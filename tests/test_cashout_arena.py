from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ban.arena_contract import (ARENA_CONTRACT_VERSION, BEHAVIORS, PROFILE_BISQ_EASY, PROFILE_BTC_FIRST, PROFILES,
                                ROLE_MAP, ArenaContractError, CashoutTrace, ConservationError, QuoteTrace,
                                check_conservation, ledger_trace_to_arena_lines, parse_arena_line, scenario_for,
                                scenario_from_dict)
from ban.sim.demo import EXTRA_ARENA_SCENARIOS, _world, golden_scenarios
from ban.sim.ledger import State

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = json.loads((FIXTURES / "cashout_demo.json").read_text(encoding="utf-8"))
ARENA_LINES = (FIXTURES / "arena" / "arena_lines.jsonl").read_text(encoding="utf-8").splitlines()


def quote_trace(**kw):
    base = dict(transitions=(("INTENT_RECEIVED", "QUOTED"), ("QUOTED", "FUNDING_SEEN"),
                             ("FUNDING_SEEN", "FUNDING_ACCEPTED")),
                net_payout_minor=5000, sender_sent_sats=13293, provider_received_sats=13293, refunded_sats=0,
                rail_credits_minor=(), provider_claimed_minor=0, funding_accepted=True)
    base.update(kw)
    return QuoteTrace(**base)


def trace(*quotes, expected="blocked", scenario="t"):
    return CashoutTrace(scenario, expected, "ILS", 2, tuple(quotes), ())


class ArenaContractTests(unittest.TestCase):
    def test_role_map_keeps_the_provider_as_the_btc_buyer(self):
        self.assertEqual(ROLE_MAP["provider"], "buyer")
        self.assertNotEqual(ROLE_MAP["provider"], "seller")
        self.assertEqual(dict(ROLE_MAP), {"provider": "buyer", "sender": "seller", "recipient": "fiat_payee"})
        with self.assertRaises(TypeError):
            ROLE_MAP["provider"] = "seller"  # type: ignore[index]
        self.assertEqual(ARENA_CONTRACT_VERSION, "ban-arena-cashout/1")
        self.assertEqual(PROFILES, ("cashout-btc-first", "cashout-bisq-easy"))

    def test_golden_scenario_ids_for_every_behavior_and_both_profiles(self):
        golden = json.loads((FIXTURES / "arena" / "cashout_scenario_ids.json").read_text(encoding="utf-8"))
        self.assertEqual(golden["source"], "SYNTHETIC_FIXTURE")
        lines = (FIXTURES / "arena" / "cashout_scenarios.jsonl").read_text(encoding="utf-8").splitlines()
        scenarios = golden_scenarios(FIXTURE)
        self.assertEqual(len(scenarios), 2 * len(BEHAVIORS))
        self.assertEqual({(s.profile, s.scenario) for s in scenarios},
                         {(p, b) for p in PROFILES for b in BEHAVIORS})
        self.assertEqual([s.canonical() for s in scenarios], lines)
        self.assertEqual({s.profile + "/" + s.scenario: s.scenario_id() for s in scenarios}, golden["scenario_ids"])
        self.assertEqual(len(set(golden["scenario_ids"].values())), 24)
        for line, s in zip(lines, scenarios):
            self.assertEqual(scenario_from_dict(json.loads(line)), s)
        exposure = {(s.profile, s.scenario) for s in scenarios if s.expected == "exposure_observed"}
        self.assertIn((PROFILE_BTC_FIRST, "provider_withholds"), exposure)
        self.assertIn((PROFILE_BISQ_EASY, "sender_withholds"), exposure)
        self.assertNotIn((PROFILE_BISQ_EASY, "provider_withholds"), exposure)

    def test_scenario_import_is_strict(self):
        data = golden_scenarios(FIXTURE)[0].as_dict()
        for mutate in ({"extra": 1}, {"arena_supported": True}, {"amount_sats": "13293"}, {"source": "BISQ2_LOCAL"},
                       {"behavior": {"provider": "honest"}}, {"expected": "won"}):
            with self.subTest(mutate=mutate), self.assertRaises(ArenaContractError):
                scenario_from_dict(dict(data, **mutate))

    def test_non_usd_scenarios_are_flagged_unsupported(self):
        kw = dict(amount_sats=13293, net_payout_minor=5000, exponent=2, quote_expiry_height=206, min_conf=2)
        self.assertFalse(scenario_for(PROFILE_BTC_FIRST, "honest", currency="ILS", **kw).as_dict()["arena_supported"])
        self.assertTrue(scenario_for(PROFILE_BTC_FIRST, "honest", currency="USD", **kw).as_dict()["arena_supported"])
        self.assertFalse(scenario_for(PROFILE_BTC_FIRST, "honest", currency="USD",
                                      **dict(kw, net_payout_minor=60_001)).as_dict()["arena_supported"])

    # -- arena lines
    def test_parse_accepts_real_arena_lines(self):
        readme, strict, legacy = (parse_arena_line(line) for line in ARENA_LINES)
        self.assertEqual((readme.kind, readme.protocol, readme.passed, readme.attack_success),
                         ("scenario", "bisq-easy", True, True))  # passed=oracle matched; attack_success=loss shown
        self.assertEqual(readme.fields["safety_findings"], ["seller_non_delivery"])
        self.assertEqual((strict.kind, strict.fields["main_task_success"], strict.fields["side_task_success"]),
                         ("control_state", True, False))
        self.assertTrue(legacy.fields["side_task_success"])
        musig = ('{"protocol":"bisq-musig-model","scenario":"crash-after-ack","passed":true,"attack_success":false,'
                 '"recovered":true,"trials":3,"committed_events":5,"acknowledgements":2,"final_phase":"Completed",'
                 '"safety_findings":[]}')
        self.assertEqual(parse_arena_line(musig).protocol, "bisq-musig-model")

    def test_parse_rejects_unknown_missing_and_non_integer_fields(self):
        readme = json.loads(ARENA_LINES[0])
        bad = [
            dict(readme, extra=1),
            {k: v for k, v in readme.items() if k != "committed_events"},
            dict(readme, committed_events=8.0),
            dict(readme, buyer_usd_at_risk_cents="5000"),
            dict(readme, seller_btc_at_risk_sats=-1),
            dict(readme, passed=1),
            dict(readme, attack_success="true"),
            dict(readme, protocol="lightning"),
            dict(readme, safety_findings=[1]),
            dict(json.loads(ARENA_LINES[1]), kind="trade"),
            {k: v for k, v in json.loads(ARENA_LINES[1]).items() if k != "journal_valid"},
        ]
        for data in bad:
            with self.subTest(data=sorted(data)), self.assertRaises(ArenaContractError):
                parse_arena_line(json.dumps(data))
        for text in (ARENA_LINES[0].replace('"passed":true', '"passed":true,"passed":true'), "[1]", "not json",
                     ARENA_LINES[0].replace("5000", "5e3"), ARENA_LINES[0].replace("5000", "NaN")):
            with self.subTest(text=text[:40]), self.assertRaises(ArenaContractError):
                parse_arena_line(text)

    # -- traces
    def test_unmitigated_provider_withholding_is_an_attack_success_with_sender_btc_at_risk(self):
        line = json.loads(ledger_trace_to_arena_lines(trace(quote_trace(), expected="exposure_observed",
                                                            scenario="provider_withholds"))[0])
        self.assertEqual((line["passed"], line["attack_success"]), (True, True))
        self.assertEqual(line["sender_btc_at_risk_sats"], 13293)
        self.assertIn("provider_non_delivery", line["safety_findings"])
        # The same trace under a "blocked" oracle does not pass: passed tracks the oracle only.
        blocked = json.loads(ledger_trace_to_arena_lines(trace(quote_trace(), expected="blocked"))[0])
        self.assertEqual((blocked["passed"], blocked["attack_success"]), (False, True))
        # Refunded or delivered: no exposure.
        for q in (quote_trace(refunded_sats=13293), quote_trace(rail_credits_minor=(5000,))):
            self.assertEqual(json.loads(ledger_trace_to_arena_lines(trace(q))[0])["sender_btc_at_risk_sats"], 0)

    def test_simulated_provider_withholding_and_claim_without_credit(self):
        with tempfile.TemporaryDirectory() as temp:
            withheld = EXTRA_ARENA_SCENARIOS["provider_withholds"](_world(FIXTURE, Path(temp), "w"))
            line = json.loads(ledger_trace_to_arena_lines(withheld)[0])
            self.assertEqual((line["passed"], line["attack_success"]), (True, True))
            self.assertGreater(line["sender_btc_at_risk_sats"], 0)
            w = _world(FIXTURE, Path(temp), "c")
            claimed = EXTRA_ARENA_SCENARIOS["claim_without_credit"](w)
            self.assertNotIn(State.RECEIPTED.value, {to for q in claimed.quotes for _, to in q.transitions})
            self.assertEqual([r.state for r in w.provider.ledger.all_rows()], [State.PAYOUT_SUBMITTED])
            line = json.loads(ledger_trace_to_arena_lines(claimed)[0])
            self.assertEqual(line["recipient_credited_minor"], 0)  # P's claim never credits R

    def test_conservation(self):
        check_conservation(trace(quote_trace(rail_credits_minor=(5000,), provider_claimed_minor=5000,
                                             transitions=quote_trace().transitions + (("X", "RECEIPTED"),))))
        broken = [
            quote_trace(provider_received_sats=13000),                     # sats lost between S and P
            quote_trace(refunded_sats=20000),                              # refund more than received
            quote_trace(provider_claimed_minor=5000,                       # receipt on a claim alone
                        transitions=quote_trace().transitions + (("X", "RECEIPTED"),)),
            quote_trace(funding_accepted=False, rail_credits_minor=(5000,)),  # credit without funding
            quote_trace(sender_sent_sats=-1, provider_received_sats=-1),
        ]
        for q in broken:
            with self.subTest(q=q), self.assertRaises((ConservationError, ArenaContractError)):
                check_conservation(trace(q))
        with self.assertRaises(ArenaContractError):
            ledger_trace_to_arena_lines(replace(trace(quote_trace()), profile=PROFILE_BISQ_EASY))


if __name__ == "__main__":
    unittest.main()
