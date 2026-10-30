import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from ban import cli
from ban.sim.demo import ATTACKS, run_demo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cashout_demo.json"


class CashoutDemoTests(unittest.TestCase):
    def test_cli_demo_exits_zero_and_prints_every_refused_attack(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["ban", "cashout-demo", "--fixture", str(FIXTURE)]), \
                contextlib.redirect_stdout(out):
            cli.main()
        result = json.loads(out.getvalue())
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(set(fixture["attacks"]), set(ATTACKS))
        self.assertTrue(result["all_attacks_refused"])
        self.assertEqual([a["attack"] for a in result["attacks"]], fixture["attacks"])
        self.assertTrue(all(a["refused"] and a["by"] for a in result["attacks"]))
        steps = [s["step"] for s in result["transitions"]]
        self.assertEqual(steps, ["T0a", "T0b", "T0c", "T1", "select", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"])
        select = result["transitions"][4]
        self.assertEqual(select["excluded"]["maker_sells_btc"], 2)
        self.assertTrue(all(o.startswith("synthetic-ils-buy") for o in select["selected"]))
        self.assertEqual(result["transitions"][-1]["result"], "RECONCILED")
        self.assertNotIn(fixture["alias"]["destination"], out.getvalue())

    def test_demo_refuses_non_synthetic_fixtures(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["source"] = "BISQ2_LOCAL"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                run_demo(path)


if __name__ == "__main__":
    unittest.main()
