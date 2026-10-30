"""Opt-in private regtest node for the cashout simulation (spec G.5). SIMULATION ONLY.

``RegtestNode`` starts ``bitcoind -regtest`` from a local Bitcoin Core install with
a fresh throwaway datadir, RPC on 127.0.0.1 at a random port with cookie auth, and
no P2P listening or outbound connections. Before any wallet action, mining or
broadcast it refuses unless the node reports ``chain == "regtest"`` and the regtest
genesis hash. ``close()`` stops the node and deletes the datadir.

``RegtestWorld`` is a ``CashoutWorld`` whose funding is a real wallet payment to the
quote's deposit script, judged by the provider with ``CoreRpcEvidence``. Regtest
coins have no value; nothing here can reach testnet, signet or mainnet.
"""
from __future__ import annotations

import atexit
import base64
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from ..arena_contract import ARENA_CONTRACT_VERSION, ledger_trace_to_arena_lines, write_jsonl
from ..evidence import CoreRpcEvidence, EvidenceRejected, FixtureTx, FundingClaim, ReplayedOutput, WrongOutput
from ..intent import SATS_PER_BTC, price
from .demo import _NotRefused, _refused, happy_path
from .ledger import IllegalTransition, LedgerError, ReplayRejected, State
from .provider import ProviderRefused
from .recheck import RECHECK_CASES, expect as _expect
from .testkeys import TestKey
from .world import CashoutWorld, _merge

REGTEST_GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
# Reads that cannot change node, wallet or chain state. Every other method, including
# any method not listed here, runs only after the chain guard.
READ_ONLY = frozenset({"getblockchaininfo", "getblockhash", "getblockcount", "getbestblockhash", "getblockheader",
                       "getrawtransaction", "getrawmempool", "scantxoutset", "decodescript", "getnetworkinfo"})
WALLET = "ban-regtest"


class RegtestError(RuntimeError):
    pass


class NotRegtest(RegtestError):
    """The node did not prove it is regtest; the guarded call was not sent."""


class RegtestRpcError(RegtestError):
    def __init__(self, method: str, code: Any, message: Any):
        super().__init__("%s: RPC error %s: %s" % (method, code, message))
        self.code = code


def btc(sats: int) -> str:
    if type(sats) is not int or sats <= 0:
        raise ValueError("amount must be positive integer sats")
    return format(Decimal(sats).scaleb(-8), "f")


def sats(value: Any) -> int:
    amount = Decimal(value) * SATS_PER_BTC
    if amount != amount.to_integral_value():
        raise RegtestError("non-integral satoshi amount")
    return int(amount)


def outpoint(claim: FundingClaim) -> str:
    return "%s:%d" % (claim.txid, claim.vout)


class RegtestRpc:
    """JSON-RPC client for one loopback regtest node, with the chain guard."""

    def __init__(self, url: str, cookie_path: str | Path | None, *, timeout: float = 30.0):
        self.url = url
        self.cookie_path = cookie_path
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never via a proxy

    def _raw(self, method: str, params: list | dict | None = None, *, wallet: str | None = None) -> Any:
        body = json.dumps({"jsonrpc": "1.0", "id": "ban-regtest", "method": method,
                           "params": [] if params is None else params}).encode()
        request = urllib.request.Request(self.url + ("/wallet/" + wallet if wallet else ""), data=body,
                                         headers={"Content-Type": "application/json"})
        token = Path(self.cookie_path).read_text(encoding="ascii").strip()
        request.add_header("Authorization", "Basic " + base64.b64encode(token.encode()).decode())
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                reply = json.loads(response.read(), parse_float=Decimal)
        except urllib.error.HTTPError as exc:  # Core sends JSON-RPC 1.0 errors as HTTP 404/500 with a body
            try:
                reply = json.loads(exc.read(), parse_float=Decimal)
            except ValueError:
                raise RegtestError("%s: HTTP %d from the regtest node" % (method, exc.code)) from None
        if not isinstance(reply, dict):
            raise RegtestError(method + ": malformed RPC reply")
        if reply.get("error"):
            raise RegtestRpcError(method, reply["error"].get("code"), reply["error"].get("message"))
        return reply["result"]

    def require_regtest(self) -> None:
        info = self._raw("getblockchaininfo")
        genesis = self._raw("getblockhash", [0])
        if info.get("chain") != "regtest" or genesis != REGTEST_GENESIS:
            raise NotRegtest("refusing: node reports chain=%r, genesis %s" % (info.get("chain"), genesis))

    def call(self, method: str, params: list | dict | None = None, *, wallet: str | None = None) -> Any:
        if method not in READ_ONLY:
            self.require_regtest()
        return self._raw(method, params, wallet=wallet)

    def wallet(self, method: str, params: list | dict | None = None) -> Any:
        return self.call(method, params, wallet=WALLET)

    # -- regtest wallet, mining and broadcast (all guarded)
    def setup_wallet(self, *, mature_blocks: int = 101) -> None:
        self.call("createwallet", {"wallet_name": WALLET})
        self.mine(mature_blocks)

    def mine(self, blocks: int = 1) -> list[str]:
        if blocks <= 0:
            return []
        # A fresh coinbase address per call, so a block re-mined after invalidateblock never
        # reproduces the invalidated block.
        address = self.wallet("getnewaddress", {"address_type": "bech32m"})
        return self.call("generatetoaddress", [blocks, address])

    def address(self, script_hex: str) -> str:
        decoded = self.call("decodescript", [script_hex])
        if "address" not in decoded:
            raise RegtestError("script has no regtest address")
        return decoded["address"]

    def pay(self, script_hex: str, amount_sats: int, *, fee_rate: int = 2) -> FundingClaim:
        """Broadcast a wallet payment of ``amount_sats`` to ``script_hex`` (BIP125-signalling, unconfirmed)."""
        txid = self.wallet("sendtoaddress", {"address": self.address(script_hex), "amount": btc(amount_sats),
                                             "replaceable": True, "fee_rate": fee_rate})
        return FundingClaim(txid, self.output_index(txid, script_hex))

    def bump(self, txid: str, *, fee_rate: int = 25) -> str:
        """RBF-replace a wallet payment; the replacement keeps its outputs and cuts the change."""
        return self.wallet("bumpfee", {"txid": txid, "options": {"fee_rate": fee_rate}})["txid"]

    def wallet_tx(self, txid: str) -> dict:
        return self.wallet("gettransaction", {"txid": txid, "verbose": True})

    def output_index(self, txid: str, script_hex: str) -> int:
        hits = [o["n"] for o in self.wallet_tx(txid)["decoded"]["vout"] if o["scriptPubKey"]["hex"] == script_hex]
        if len(hits) != 1:
            raise RegtestError("expected exactly one output paying the script in " + txid)
        return hits[0]

    def now(self) -> int:
        """Wall-clock seconds, never behind the tip's block time (fast regtest mining runs ahead)."""
        tip = self.call("getblockheader", [self.call("getbestblockhash")])
        return max(int(time.time()), int(tip["time"]))


def find_bitcoind(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        if not Path(explicit).is_file():
            raise RegtestError("bitcoind not found at " + str(explicit))
        return Path(explicit)
    candidates = [os.getenv("BAN_BITCOIND"), shutil.which("bitcoind")]
    if os.name == "nt":
        candidates.append(str(Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Bitcoin" / "daemon" / "bitcoind.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RegtestError("bitcoind not found: set BAN_BITCOIND or put bitcoind on PATH")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RegtestNode(RegtestRpc):
    """A private ``bitcoind -regtest`` in a fresh datadir under ``root``; deleted on ``close()``."""

    def __init__(self, bitcoind: str | os.PathLike | None = None, *, root: str | os.PathLike | None = None,
                 startup_timeout: float = 120.0):
        super().__init__("", None)
        self.bitcoind = find_bitcoind(bitcoind)
        self.root = Path(root or os.getenv("BAN_REGTEST_ROOT") or tempfile.gettempdir())
        self.startup_timeout = startup_timeout
        self.datadir: Path | None = None
        self._made: Path | None = None  # close() deletes only the datadir start() made
        self.datadir_removed = False
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.version = ""

    def command(self) -> list[str]:
        return [str(self.bitcoind), "-regtest", "-datadir=" + str(self.datadir), "-server=1",
                "-rpcbind=127.0.0.1", "-rpcallowip=127.0.0.1", "-rpcport=%d" % self.port,
                "-listen=0", "-connect=0", "-dnsseed=0", "-fixedseeds=0", "-discover=0",
                "-fallbackfee=0.0002", "-printtoconsole=0"]

    def start(self) -> "RegtestNode":
        if self.process is not None or self._made is not None:
            raise RegtestError("a RegtestNode starts once")
        self.root.mkdir(parents=True, exist_ok=True)
        self.datadir = self._made = Path(tempfile.mkdtemp(prefix="ban-regtest-", dir=self.root))
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.cookie_path = self.datadir / "regtest" / ".cookie"
        atexit.register(self.close)
        try:
            self.process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + self.startup_timeout
            while True:
                if self.process.poll() is not None:
                    raise RegtestError("bitcoind exited with %s: %s" % (self.process.returncode, self.log_tail()))
                if self.cookie_path.exists():
                    try:
                        self.require_regtest()
                        break
                    except NotRegtest:
                        raise
                    except (RegtestError, OSError):
                        pass  # RPC not listening yet, or warming up (-28)
                if time.monotonic() > deadline:
                    raise RegtestError("bitcoind did not answer within %.0f s: %s" % (self.startup_timeout,
                                                                                      self.log_tail()))
                time.sleep(0.2)
            self.version = self._raw("getnetworkinfo")["subversion"]
        except BaseException:
            self.close()
            raise
        return self

    def log_tail(self, lines: int = 15) -> str:
        log = self.datadir / "regtest" / "debug.log" if self.datadir else None
        if log is None or not log.exists():
            return "(no debug.log)"
        return " | ".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def close(self) -> None:
        atexit.unregister(self.close)
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                self.call("stop")  # guarded like every other state change
            except (RegtestError, OSError):
                process.terminate()  # our own child either way
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
        if self._made is not None and self._made.exists():
            for _ in range(20):  # Windows releases file handles shortly after exit
                shutil.rmtree(self._made, ignore_errors=True)
                if not self._made.exists():
                    break
                time.sleep(0.25)
        self.datadir_removed = self._made is not None and not self._made.exists()

    def __enter__(self) -> "RegtestNode":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------- the world

class RegtestChain:
    """``FixtureChain``-shaped view of the node, so world and demo code read real heights."""

    network = "regtest"

    def __init__(self, node: RegtestRpc):
        self.node = node
        self.funding: list[str] = []  # txids this world broadcast

    @property
    def tip_height(self) -> int:
        return int(self.node.call("getblockcount"))

    def block_hash(self, height: int) -> str:
        return self.node.call("getblockhash", [height])

    def mine(self, blocks: int = 1) -> None:
        self.node.mine(blocks)

    @property
    def txs(self) -> dict[str, FixtureTx]:
        """This world's funding txs as the active chain shows them now: unconfirmed,
        replaced and reorged-out transactions are absent."""
        out = {}
        for txid in self.funding:
            tx = self.node.wallet_tx(txid)
            if tx.get("confirmations", 0) < 1:
                continue
            outputs = [(sats(o["value"]), o["scriptPubKey"]["hex"]) for o in tx["decoded"]["vout"]]
            out[txid] = FixtureTx(txid, tx["blockhash"], int(tx["blockheight"]), outputs)
        return out


class RegtestWorld(CashoutWorld):
    """``CashoutWorld`` on the private regtest node. ``now`` follows the node's clock,
    because the provider compares real block times with the quote's expiry."""

    chain_label = "regtest node"

    def __init__(self, workdir: str | Path, node: RegtestRpc, config: dict | None = None):
        self.node = node
        cfg = dict(config or {})
        if cfg.get("network", "regtest") != "regtest":
            raise ValueError("RegtestWorld is regtest-only")
        cfg["now"] = node.now()
        Path(workdir).mkdir(parents=True, exist_ok=True)
        super().__init__(workdir, cfg)

    def make_chain(self) -> RegtestChain:
        return RegtestChain(self.node)

    def funding_evidence(self) -> CoreRpcEvidence:
        return CoreRpcEvidence(self.node.url, self.policy, cookie_path=str(self.node.cookie_path))

    def fund(self, quote, *, value: int | None = None, script: str | None = None, height: int | None = None,
             branch: str = "main", vout: int = 0, tag: str = "", mine: bool = True) -> FundingClaim:
        if height is not None or branch != "main" or vout:
            raise ValueError("regtest funding is a real payment: its height, branch and vout come from the chain")
        claim = self.node.pay(quote.deposit_script if script is None else script,
                              quote.sats_required if value is None else value)
        self.chain.funding.append(claim.txid)
        if mine:
            self.node.mine(1)
        return claim

    def advance(self, *, blocks: int = 0, seconds: int = 0) -> None:
        self.node.mine(blocks)
        self.now += seconds

    def orphan_funding(self, claim: FundingClaim) -> None:
        """``invalidateblock`` the funding block: the transaction goes back to the mempool."""
        self.node.call("invalidateblock", [self.node.wallet_tx(claim.txid)["blockhash"]])

    def reconfirm_funding(self, claim: FundingClaim) -> None:
        """Mine ``min_conf`` blocks; the first re-includes the transaction in a new block."""
        self.node.mine(self.cfg["provider"]["min_conf"])


def regtest_world(node: RegtestRpc, fx: dict, root: Path, name: str, *, provider_label: str | None = None) -> RegtestWorld:
    """A world in ``root/name``. Deposit keys derive from the provider label and the quote
    count, so worlds sharing one node need distinct labels to get distinct scripts."""
    config = fx if provider_label is None else _merge(fx, {"keys": {"provider": provider_label}})
    world = RegtestWorld(Path(root) / name, node, config)
    world.enroll()
    return world


# ---------------------------------------------------------------- the cases

def _accepted_at_min_conf(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    claim = w.fund(quote)
    at_one = w.provider.on_funding(qid, now=w.now)
    w.advance(blocks=quote.min_conf - 1)
    accepted = w.provider.on_funding(qid, now=w.now)
    acceptance = w.provider.evidence.accept(quote, now=w.now, claim=claim)
    paid = w.provider.pay(qid, now=w.now)
    _expect((at_one, accepted) == (State.FUNDING_SEEN, State.FUNDING_ACCEPTED),
            "funding was %s at 1 conf and %s at min_conf" % (at_one.value, accepted.value))
    return {"outpoint": outpoint(claim), "at_1_conf": at_one.value, "at_min_conf": accepted.value,
            "value": acceptance.value, "sats_required": quote.sats_required,
            "confirmations": acceptance.confirmations, "min_conf": quote.min_conf,
            "evidence_profile": acceptance.evidence_profile, "after_pay": paid.value,
            "rail_calls": len(w.rail_mock.submit_calls)}


def _underpay(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    claim = w.fund(quote, value=quote.sats_required - 1)
    w.advance(blocks=quote.min_conf - 1)
    refusal = _refused(WrongOutput, lambda: w.provider.on_funding(quote.quote_id(), now=w.now))
    row = w.provider.ledger.get(quote.quote_id())
    _expect(row.state is State.QUOTED and row.outpoint is None, "underpayment moved the quote to " + row.state.value)
    return {"outpoint": outpoint(claim), "paid_sats": quote.sats_required - 1, "sats_required": quote.sats_required,
            "refusal": refusal, "state": row.state.value, "underpay_policy": quote.underpay_policy,
            "rail_calls": len(w.rail_mock.submit_calls)}


def _overpay(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    paid_sats = quote.sats_required + 25_000
    claim = w.fund(quote, value=paid_sats)
    w.advance(blocks=quote.min_conf - 1)
    state = w.provider.on_funding(qid, now=w.now)
    acceptance = w.provider.evidence.accept(quote, now=w.now, claim=claim)
    after = w.provider.pay(qid, now=w.now)
    amounts = [call[2] for call in w.rail_mock.submit_calls]
    _expect(state is State.FUNDING_ACCEPTED and acceptance.value == paid_sats and amounts == [quote.net_payout_minor],
            "overpayment: %s, value %d, rail amounts %s" % (state.value, acceptance.value, amounts))
    return {"outpoint": outpoint(claim), "paid_sats": paid_sats, "sats_required": quote.sats_required,
            "state": state.value, "accepted_value": acceptance.value, "overpay_policy": quote.overpay_policy,
            "after_pay": after.value, "rail_amounts_minor": amounts, "net_payout_minor": quote.net_payout_minor}


def _late_funding(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    w.advance(blocks=quote.quote_expiry_height - w.chain.tip_height)  # tip at the expiry height
    claim = w.fund(quote)  # confirms one block above it
    w.advance(blocks=quote.min_conf - 1)
    state = w.provider.on_funding(qid, now=w.now)
    row = w.provider.ledger.get(qid)
    refusal = _refused((ProviderRefused, IllegalTransition), lambda: w.provider.authorize(qid, now=w.now))
    _expect(state is State.LATE_FUNDING and row.funding_height == quote.quote_expiry_height + 1
            and w.provider.ledger.authorization(qid) is None and not w.rail_mock.submit_calls,
            "late funding ended in " + state.value)
    _, edge, _ = w.quote()  # the boundary: funding in the block at the expiry height is in time
    w.advance(blocks=edge.quote_expiry_height - 1 - w.chain.tip_height)
    w.fund(edge)
    w.advance(blocks=edge.min_conf - 1)
    edge_state = w.provider.on_funding(edge.quote_id(), now=w.now)
    edge_row = w.provider.ledger.get(edge.quote_id())
    _expect(edge_state is State.FUNDING_ACCEPTED and edge_row.funding_height == edge.quote_expiry_height,
            "funding at the expiry height ended in " + edge_state.value)
    return {"outpoint": outpoint(claim), "quote_expiry_height": quote.quote_expiry_height,
            "funding_height": row.funding_height, "state": state.value, "authorize": refusal,
            "rail_calls": len(w.rail_mock.submit_calls), "at_expiry_height": edge_state.value}


def _replayed_output(w: RegtestWorld) -> dict:
    n = w.provider.ledger.quote_count()
    script = "5120" + TestKey("%s/deposit/%d" % (w.provider.key.label, n)).pubkey  # the next quote's script
    i, p = w.cfg["intent"], w.cfg["provider"]
    amount = price(i["amount_mode"], i["amount"], p["rate_minor_per_btc"], p["fee_bps"]).sats_required
    claim = w.node.pay(script, amount)
    w.chain.funding.append(claim.txid)
    w.advance(blocks=2)  # the pre-funding confirms below the next quote's created height
    funding_height = int(w.node.wallet_tx(claim.txid)["blockheight"])
    _, quote, _ = w.quote()
    _expect(quote.deposit_script == script, "the provider's deposit script derivation changed")
    refusal = _refused(ReplayedOutput, lambda: w.provider.on_funding(quote.quote_id(), now=w.now))
    row = w.provider.ledger.get(quote.quote_id())
    _expect(row.state is State.QUOTED and row.outpoint is None, "a pre-funded output moved the quote")
    return {"outpoint": outpoint(claim), "funding_height": funding_height,
            "quote_created_height": quote.quote_created_height, "refusal": refusal, "state": row.state.value}


def _reorg_below_min_conf(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    claim = w.fund(quote)
    w.advance(blocks=quote.min_conf - 1)
    accepted = w.provider.on_funding(qid, now=w.now)
    tip = w.node.call("getbestblockhash")
    w.node.call("invalidateblock", [tip])  # the confirming block leaves the active chain
    confirmations = int(w.node.wallet_tx(claim.txid)["confirmations"])
    after = w.provider.authorize(qid, now=w.now)
    unauthorized = w.provider.ledger.authorization(qid) is None and not w.rail_mock.submit_calls
    w.node.call("reconsiderblock", [tip])
    again = w.provider.on_funding(qid, now=w.now)
    paid = w.provider.pay(qid, now=w.now)
    _expect(accepted is State.FUNDING_ACCEPTED and after is State.FUNDING_SEEN and unauthorized
            and again is State.FUNDING_ACCEPTED and len(w.rail_mock.submit_calls) == 1,
            "reorg below min_conf: %s -> %s -> %s" % (accepted.value, after.value, again.value))
    return {"outpoint": outpoint(claim), "accepted": accepted.value, "invalidated": tip,
            "confirmations_after_reorg": confirmations, "authorize_after_reorg": after.value,
            "authorized_while_reorged": not unauthorized, "after_reconsiderblock": again.value,
            "after_pay": paid.value, "rail_calls": len(w.rail_mock.submit_calls),
            "history": ["%s->%s" % edge for edge in w.provider.ledger.history(qid)]}


def _reorg_orphans_funding_block(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    claim = w.fund(quote)
    funding_block = w.node.wallet_tx(claim.txid)["blockhash"]
    w.advance(blocks=quote.min_conf - 1)
    accepted = w.provider.on_funding(qid, now=w.now)
    w.node.call("invalidateblock", [funding_block])
    in_mempool = claim.txid in w.node.call("getrawmempool")
    after = w.provider.authorize(qid, now=w.now)
    unauthorized = w.provider.ledger.authorization(qid) is None and not w.rail_mock.submit_calls
    w.advance(blocks=quote.min_conf)  # the same transaction confirms again, in a new block
    new_block = w.node.wallet_tx(claim.txid)["blockhash"]
    again = w.provider.on_funding(qid, now=w.now)
    paid = w.provider.pay(qid, now=w.now)
    _expect(accepted is State.FUNDING_ACCEPTED and in_mempool and after is State.FUNDING_SEEN and unauthorized
            and new_block != funding_block and again is State.FUNDING_ACCEPTED
            and len(w.rail_mock.submit_calls) == 1,
            "orphaned funding block: %s -> %s -> %s" % (accepted.value, after.value, again.value))
    return {"outpoint": outpoint(claim), "accepted": accepted.value, "invalidated": funding_block,
            "back_in_mempool": in_mempool, "authorize_after_reorg": after.value,
            "authorized_while_reorged": not unauthorized, "reconfirmed_in": new_block,
            "after_reconfirm": again.value, "after_pay": paid.value, "rail_calls": len(w.rail_mock.submit_calls)}


def _rbf_replacement(w: RegtestWorld) -> dict:
    _, quote, _ = w.quote()
    qid = quote.quote_id()
    original = w.fund(quote, mine=False)
    # scantxoutset reads the UTXO set: an unconfirmed payment is invisible, so nothing is bound yet.
    unconfirmed = _refused(WrongOutput, lambda: w.provider.on_funding(qid, now=w.now))
    bumped = w.node.bump(original.txid)
    replacement = FundingClaim(bumped, w.node.output_index(bumped, quote.deposit_script))
    w.chain.funding.append(replacement.txid)
    w.advance(blocks=quote.min_conf)
    states = [w.provider.on_funding(qid, now=w.now) for _ in range(3)]
    unspents = w.node.call("scantxoutset", ["start", ["raw(%s)" % quote.deposit_script]])["unspents"]
    stale = _refused(WrongOutput, lambda: w.provider.evidence.accept(quote, now=w.now, claim=original))
    for _ in range(3):
        w.provider.pay(qid, now=w.now)
    row = w.provider.ledger.get(qid)
    acceptances = sum(1 for _, to in w.provider.ledger.history(qid) if to == State.FUNDING_ACCEPTED.value)
    original_confirmations = int(w.node.wallet_tx(original.txid)["confirmations"])
    _expect(replacement.txid != original.txid and states == [State.FUNDING_ACCEPTED] * 3 and acceptances == 1
            and row.outpoint == outpoint(replacement) and len(unspents) == 1 and original_confirmations < 1
            and len(w.rail_mock.submit_calls) == 1,
            "RBF: %d acceptances, outpoint %s, %d rail calls" % (acceptances, row.outpoint,
                                                                 len(w.rail_mock.submit_calls)))
    return {"original": outpoint(original), "replacement": outpoint(replacement), "while_unconfirmed": unconfirmed,
            "states": [s.value for s in states], "acceptances": acceptances, "bound_outpoint": row.outpoint,
            "unspents_paying_script": len(unspents), "original_confirmations": original_confirmations,
            "original_claim": stale, "rail_calls": len(w.rail_mock.submit_calls)}


def _outpoint_for_two_quotes(w: RegtestWorld) -> dict:
    _, first, _ = w.quote()
    claim = w.fund(first)
    w.advance(blocks=first.min_conf - 1)
    accepted = w.provider.on_funding(first.quote_id(), now=w.now)
    _, second, _ = w.quote()
    by_evidence = _refused(WrongOutput, lambda: w.provider.on_funding(second.quote_id(), now=w.now, claim=claim))
    by_ledger = _refused(ReplayRejected, lambda: w.provider.ledger.transition(
        second.quote_id(), State.FUNDING_SEEN, now=w.now, outpoint=outpoint(claim),
        funding_height=second.quote_created_height))
    row_a, row_b = w.provider.ledger.get(first.quote_id()), w.provider.ledger.get(second.quote_id())
    _expect(accepted is State.FUNDING_ACCEPTED and row_a.outpoint == outpoint(claim)
            and row_b.state is State.QUOTED and row_b.outpoint is None,
            "the outpoint moved the second quote to " + row_b.state.value)
    return {"outpoint": outpoint(claim), "first": accepted.value, "second_by_evidence": by_evidence,
            "second_by_ledger": by_ledger, "second_state": row_b.state.value}


REGTEST_CASES: dict[str, Callable[[RegtestWorld], dict]] = {
    "accepted_at_min_conf": _accepted_at_min_conf,
    "underpay": _underpay,
    "overpay": _overpay,
    "late_funding": _late_funding,
    "replayed_output": _replayed_output,
    "reorg_below_min_conf": _reorg_below_min_conf,
    "reorg_orphans_funding_block": _reorg_orphans_funding_block,
    "rbf_replacement": _rbf_replacement,
    "outpoint_for_two_quotes": _outpoint_for_two_quotes,
    **RECHECK_CASES,  # I9: the same cases also run on the fixture chain in normal CI
}


def case_world(node: RegtestRpc, fx: dict, root: Path, name: str) -> RegtestWorld:
    # Short, so Windows paths stay under MAX_PATH and deposit key labels under 100 characters.
    short = sha256(name.encode("utf-8")).hexdigest()[:10]
    return regtest_world(node, fx, root, "case-" + short, provider_label="sim/provider-p1/regtest/" + short)


def run_case(node: RegtestRpc, fx: dict, root: Path, name: str) -> dict:
    w = case_world(node, fx, root, name)
    try:
        return {"case": name, "held": True, "observed": REGTEST_CASES[name](w)}
    except (_NotRefused, EvidenceRejected, ProviderRefused, LedgerError) as exc:
        return {"case": name, "held": False, "observed": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- the demo

def run_regtest_demo(fixture: str | Path, *, bitcoind: str | None = None, root: str | None = None,
                     arena_out: str | Path = "out/arena_cashout_regtest.jsonl") -> dict:
    """``ban cashout-demo --regtest``: the B.3 flow and the funding cases on a private
    throwaway regtest node, which is stopped and deleted before this returns."""
    fx = json.loads(Path(fixture).read_text(encoding="utf-8"))
    if not isinstance(fx, dict) or fx.get("source") != "SYNTHETIC_FIXTURE" or fx.get("network") != "regtest":
        raise ValueError("cashout-demo --regtest accepts a regtest SYNTHETIC_FIXTURE only")
    node = RegtestNode(bitcoind, root=root)
    with node, tempfile.TemporaryDirectory(prefix="ban-regtest-worlds-", ignore_cleanup_errors=True) as tmp:
        node.setup_wallet()
        worlds = Path(tmp)
        flow, honest = happy_path(fx, worlds, make_world=lambda fx, r, name: regtest_world(node, fx, r, name))
        cases = [run_case(node, fx, worlds, name) for name in REGTEST_CASES]
        tip = node.call("getblockcount")
    lines = ledger_trace_to_arena_lines(honest)
    write_jsonl(lines, arena_out)
    scored = [json.loads(line) for line in lines]
    held = sum(1 for c in cases if c["held"])
    return {
        "source": "SYNTHETIC_FIXTURE",
        "simulation_only": True,
        "chain": {"network": "regtest", "bitcoind": node.version, "genesis": REGTEST_GENESIS,
                  "rpc": "127.0.0.1, cookie auth, random port", "evidence": "CoreRpcEvidence (own_core_v1)",
                  "final_tip_height": tip, "datadir": str(node.datadir), "datadir_removed": node.datadir_removed},
        "transitions": flow,
        "cases": cases,
        "cases_held": held,
        "cases_total": len(cases),
        "all_cases_held": held == len(cases),
        "arena": {"path": str(arena_out), "contract": ARENA_CONTRACT_VERSION, "lines": len(lines),
                  "all_passed": all(s["passed"] for s in scored),
                  "attack_success": [s["scenario"] for s in scored if s["attack_success"]]},
        "warning": "Regtest only: a private throwaway node whose coins have no value, synthetic keys and a mock "
                   "rail. Jawwal Pay capabilities are unverified; real payouts stay DENY in ban.policy.",
    }
