"""Funding evidence for a provider's own deposits (spec B.3 T4/T5, E, F.2).

The provider learns it was paid from its own node (``own_core_v1``). A field
such as ``"verified": true`` or ``"proof_verified": true`` is never authority:
third-party proof evidence is admitted only through a configured, pinned
verifier, and this slice configures none.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import urllib.request
from urllib.parse import urlsplit

from . import canon
from .intent import EvidencePolicyV1, FundingAcceptanceV1, ProviderQuoteV1, SATS_PER_BTC

_TRUST_FLAGS = ("verified", "proof_verified", "valid", "is_valid")


class EvidenceRejected(ValueError):
    def __init__(self, message: str, *, outpoint: str | None = None, block_height: int | None = None):
        super().__init__(message)
        self.outpoint = outpoint
        self.block_height = block_height


class WrongOutput(EvidenceRejected):
    pass


class ReplayedOutput(EvidenceRejected):
    """Funding confirmed below ``quote_created_height`` (invariant I7)."""


class LateFunding(EvidenceRejected):
    pass


class StaleChain(EvidenceRejected):
    pass


class InsufficientConfirmations(EvidenceRejected):
    pass


class UntrustedAssertion(EvidenceRejected):
    pass


class PolicyRejected(EvidenceRejected):
    pass


class VerifierUnavailable(EvidenceRejected):
    pass


@dataclass(frozen=True)
class FundingClaim:
    """An outpoint someone says pays the quote. Optional: P can scan by script."""
    txid: str
    vout: int


@dataclass(frozen=True)
class Observation:
    """What the provider's node shows for one output."""
    txid: str
    vout: int
    value: int
    script_hex: str
    block_hash: str
    block_height: int
    block_time: int
    on_active_chain: bool
    node_tip_height: int
    node_tip_hash: str
    evidence_tip_height: int

    @property
    def outpoint(self) -> str:
        return self.txid + ":" + str(self.vout)


class FundingEvidence(Protocol):
    def accept(self, quote: ProviderQuoteV1, *, now: int, claim: FundingClaim | None = None) -> FundingAcceptanceV1: ...


def _reject_trust_flags(obj: Any) -> None:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if key in _TRUST_FLAGS:
                raise UntrustedAssertion("a '%s' field is not verification authority" % key)
            _reject_trust_flags(value)
    elif isinstance(obj, list):
        for item in obj:
            _reject_trust_flags(item)


def _check_policy(policy: EvidencePolicyV1, quote: ProviderQuoteV1, profile: str) -> dict:
    if policy.digest() != quote.evidence_policy_digest:
        raise PolicyRejected("quote names a different evidence policy")
    if policy.network != quote.network:
        raise PolicyRejected("evidence policy is for another network")
    params = policy.profiles.get(profile)
    if params is None:
        raise PolicyRejected("evidence profile not accepted by policy: " + profile)
    if params["approval"]["state"] != "approved" and policy.mode == "production":
        raise PolicyRejected("candidate keys are not accepted in production mode")
    return params


def judge(quote: ProviderQuoteV1, policy: EvidencePolicyV1, obs: Observation) -> FundingAcceptanceV1:
    """Own-node acceptance predicate shared by fixture and Core evidence."""
    params = _check_policy(policy, quote, "own_core_v1")
    if params.get("chain") != quote.network:
        raise PolicyRejected("own_core_v1 chain does not match the quote network")
    where = {"outpoint": obs.outpoint, "block_height": obs.block_height}
    if sha256(bytes.fromhex(obs.script_hex)).hexdigest() != quote.deposit_script_sha256:
        raise WrongOutput("output does not pay the quoted deposit script", **where)
    if obs.value < quote.sats_required:
        raise WrongOutput("output value below sats_required", **where)
    if not obs.on_active_chain:
        raise StaleChain("funding block is not on the node's active chain", **where)
    if obs.node_tip_height - obs.evidence_tip_height > quote.max_tip_lag:
        raise StaleChain("evidence tip lags the node tip by more than max_tip_lag", **where)
    if obs.block_height < quote.quote_created_height:
        raise ReplayedOutput("funding confirmed below quote_created_height", **where)
    if obs.block_height > quote.quote_expiry_height or obs.block_time > quote.quote_expiry_unix:
        raise LateFunding("funding confirmed after the quote expired", **where)
    confirmations = obs.node_tip_height - obs.block_height + 1
    if confirmations < max(quote.min_conf, int(params.get("min_conf", 1))):
        raise InsufficientConfirmations("node shows %d confirmations" % confirmations, **where)
    evidence = canon.dumps({"txid": obs.txid, "vout": obs.vout, "value": obs.value, "script": obs.script_hex,
                            "block_hash": obs.block_hash, "block_height": obs.block_height,
                            "tip_height": obs.node_tip_height, "tip_hash": obs.node_tip_hash})
    return FundingAcceptanceV1(quote.network, quote.quote_id(), obs.txid, obs.vout, obs.value,
                               quote.deposit_script_sha256, obs.block_hash, obs.block_height, confirmations,
                               obs.node_tip_hash, "own_core_v1", sha256(evidence.encode("utf-8")).hexdigest())


def admit_third_party(policy: EvidencePolicyV1, quote: ProviderQuoteV1, evidence: Mapping[str, Any], *,
                      verifiers: Mapping[str, Callable[[Mapping[str, Any]], FundingAcceptanceV1]] | None = None
                      ) -> FundingAcceptanceV1:
    """Admit SP1/Nova proof evidence only through a configured pinned verifier."""
    verifiers = verifiers or {}
    profile = evidence.get("profile") if isinstance(evidence, Mapping) else None
    if not isinstance(profile, str):
        raise PolicyRejected("evidence names no profile")
    if profile not in verifiers:
        _reject_trust_flags(evidence)
    if profile == "cashu_bitvm_claim_v1":
        raise PolicyRejected("cashu_bitvm_claim_v1 is reserved and unverified")
    params = _check_policy(policy, quote, profile)
    if profile == "sp1_btc_payment_v1":
        pinned = ("pay_vkey", "node_vkey", "leaf_vkey", "segment_vkey", "groth16_vk_sha256", "verifier_sha256")
        program = evidence.get("program") or {}
        if any(not params.get(k) for k in pinned) or any(program.get(k) != params[k] for k in pinned):
            raise PolicyRejected("program words or verifier are not pinned by the policy")
    elif profile == "nova_spv_payment_v1":
        if (not params.get("vk_sha256") or evidence.get("vk_sha256") != params["vk_sha256"]
                or evidence.get("checkpoint") not in params.get("checkpoints", [])):
            raise PolicyRejected("Nova key or checkpoint is not pinned by the policy")
    verifier = verifiers.get(profile)
    if verifier is None:
        raise VerifierUnavailable("no pinned verifier configured for " + profile)
    return verifier(evidence)


# ------------------------------------------------------------------ fixtures

def synthetic_block_hash(height: int, branch: str = "main") -> str:
    return sha256(("SYNTHETIC-BLOCK:" + branch + ":" + str(height)).encode("ascii")).hexdigest()


@dataclass
class FixtureTx:
    txid: str
    block_hash: str
    block_height: int
    outputs: list[tuple[int, str]]  # (value sats, script hex)


class FixtureChain:
    """A synthetic node view: active-chain hashes by height plus confirmed txs."""

    def __init__(self, network: str, tip_height: int, *, genesis_time: int = 1_700_000_000):
        self.network = network
        self.tip_height = tip_height
        self.genesis_time = genesis_time
        self.txs: dict[str, FixtureTx] = {}

    def block_hash(self, height: int) -> str:
        return synthetic_block_hash(height)

    def block_time(self, height: int) -> int:
        return self.genesis_time + 600 * height

    def add_tx(self, outputs: list[tuple[int, str]], height: int, *, branch: str = "main", tag: str = "") -> str:
        for value, script in outputs:
            if type(value) is not int or value <= 0 or not isinstance(script, str):
                raise ValueError("fixture outputs are (positive sats, script hex)")
        txid = sha256(("SYNTHETIC-TX:%s:%d:%s:%s" % (branch, height, tag, outputs)).encode("utf-8")).hexdigest()
        self.txs[txid] = FixtureTx(txid, synthetic_block_hash(height, branch), height, list(outputs))
        return txid

    def mine(self, blocks: int = 1) -> None:
        self.tip_height += blocks


class FixtureEvidence:
    """``FundingEvidence`` over a ``FixtureChain`` (tests and demo only)."""

    def __init__(self, chain: FixtureChain, policy: EvidencePolicyV1, *, evidence_tip_height: int | None = None):
        self.chain = chain
        self.policy = policy
        self.evidence_tip_height = evidence_tip_height

    def _observe(self, tx: FixtureTx, vout: int) -> Observation:
        value, script = tx.outputs[vout]
        tip = self.chain.tip_height
        return Observation(tx.txid, vout, value, script, tx.block_hash, tx.block_height,
                           self.chain.block_time(tx.block_height),
                           tx.block_height <= tip and self.chain.block_hash(tx.block_height) == tx.block_hash,
                           tip, self.chain.block_hash(tip),
                           tip if self.evidence_tip_height is None else self.evidence_tip_height)

    def accept(self, quote: ProviderQuoteV1, *, now: int, claim: FundingClaim | None = None) -> FundingAcceptanceV1:
        if claim is not None:
            tx = self.chain.txs.get(claim.txid)
            if tx is None:
                raise WrongOutput("node does not know the claimed transaction")
            if not 0 <= claim.vout < len(tx.outputs):
                raise WrongOutput("claimed vout does not exist")
            return judge(quote, self.policy, self._observe(tx, claim.vout))
        paying = sorted(((tx.block_height, tx.txid, i) for tx in self.chain.txs.values()
                         for i, (_, script) in enumerate(tx.outputs) if script == quote.deposit_script))
        if not paying:
            raise WrongOutput("no output pays the quoted deposit script")
        _, txid, vout = paying[0]
        return judge(quote, self.policy, self._observe(self.chain.txs[txid], vout))


def load_fixture_observation(obj: Mapping[str, Any]) -> Observation:
    """Parse a fixture observation, refusing self-asserted trust flags."""
    _reject_trust_flags(obj)
    fields = Observation.__dataclass_fields__
    if set(obj) != set(fields):
        raise WrongOutput("fixture observation has unexpected fields")
    return Observation(**{k: obj[k] for k in fields})


# ------------------------------------------------------------------ Core RPC

class CoreRpcEvidence:
    """Optional own-node evidence from a user-run, loopback-only regtest node.

    Uses ``scantxoutset`` by deposit script, so no txindex is needed. Off unless
    ``BAN_REGTEST_RPC_URL`` is set; auth comes from a cookie file path.
    """

    def __init__(self, url: str, policy: EvidencePolicyV1, *, cookie_path: str | None = None, timeout: float = 10.0):
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if parts.scheme != "http" or not loopback or parts.username or parts.password or parts.query \
                or parts.fragment or parts.port is None:
            raise ValueError("Core RPC must be a credential-free http loopback URL with a port")
        if policy.network != "regtest":
            raise ValueError("CoreRpcEvidence is regtest-only in this slice")
        self.url = url
        self.policy = policy
        self.cookie_path = cookie_path
        self.timeout = timeout

    def _call(self, method: str, params: list) -> Any:
        body = json.dumps({"jsonrpc": "1.0", "id": "ban", "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        if self.cookie_path:
            token = Path(self.cookie_path).read_text(encoding="ascii").strip()
            request.add_header("Authorization", "Basic " + base64.b64encode(token.encode()).decode())
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            reply = json.loads(response.read(), parse_float=Decimal)
        if reply.get("error"):
            raise EvidenceRejected("Core RPC error: " + str(reply["error"].get("message")))
        return reply["result"]

    def accept(self, quote: ProviderQuoteV1, *, now: int, claim: FundingClaim | None = None) -> FundingAcceptanceV1:
        scan = self._call("scantxoutset", ["start", ["raw(" + quote.deposit_script + ")"]])
        info = self._call("getblockchaininfo", [])
        if info.get("chain") != "regtest":
            raise PolicyRejected("node is not on regtest")
        if scan.get("bestblock") != info.get("bestblockhash"):
            raise StaleChain("node tip moved during the UTXO scan; retry")
        unspents = sorted(scan.get("unspents", []), key=lambda u: (u["height"], u["txid"], u["vout"]))
        if claim is not None:
            unspents = [u for u in unspents if (u["txid"], u["vout"]) == (claim.txid, claim.vout)]
        if not unspents:
            raise WrongOutput("node shows no unspent output paying the deposit script")
        u = unspents[0]
        sats = Decimal(u["amount"]) * SATS_PER_BTC
        if sats != sats.to_integral_value():
            raise WrongOutput("non-integral satoshi amount")
        tip, height, vout = int(info["blocks"]), int(u["height"]), int(u["vout"])
        block_hash = self._call("getblockhash", [height])
        # Prove inclusion in the active-chain block at that height: a reorg between the scan
        # and these reads would otherwise pair the output with another chain's block.
        try:
            tx = self._call("getrawtransaction", [u["txid"], True, block_hash])
        except EvidenceRejected as exc:
            raise StaleChain("funding tx is not in the active-chain block at its height") from exc
        outputs = tx.get("vout", [])
        if not 0 <= vout < len(outputs) or outputs[vout].get("scriptPubKey", {}).get("hex") != quote.deposit_script \
                or Decimal(outputs[vout].get("value")) * SATS_PER_BTC != sats:
            raise StaleChain("block contents disagree with the UTXO scan")
        header = self._call("getblockheader", [block_hash])
        if header.get("confirmations") != tip - height + 1 or self._call("getbestblockhash", []) != info["bestblockhash"]:
            raise StaleChain("node tip moved while reading the funding block; retry")
        obs = Observation(u["txid"], vout, int(sats), quote.deposit_script, block_hash, height,
                          int(header["time"]), True, tip, info["bestblockhash"], tip)
        return judge(quote, self.policy, obs)
