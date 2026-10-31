"""End-to-end offline CLI smoke using BAN's actual signed synthetic fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class CorridorCliSmokeTests(unittest.TestCase):
    def test_25_usd_synthetic_corridor_cli(self):
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run(
            [sys.executable, "-m", "ban.sim.corridor_cli", "--fixture",
             "fixtures/cashout_demo.json", "--usd", "25.00",
             "--synthetic-ils-per-usd", "3.50"],
            cwd=root, env={**os.environ, "PYTHONPATH": "src"}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(report["status"], "SIMULATION_PASS")
        self.assertEqual(report["net_payout_minor"], 8750)
        self.assertGreater(report["indicative_offer_count"], 0)
        self.assertFalse(report["payment_execution_enabled"])
        self.assertFalse(report["live_rail_access_verified"])
        self.assertEqual(report["source"], "SYNTHETIC_FIXTURE")
        self.assertNotIn("SYNTHETIC-JAWWAL-WALLET-0001", p.stdout)
        self.assertNotIn("recipient_commitment", report)


if __name__ == "__main__":
    unittest.main()
