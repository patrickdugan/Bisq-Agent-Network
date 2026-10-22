"""Consent, retention, privacy and cohort metric regressions."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ban.cohort import (
    CohortError, cohort_report, enroll, list_merchants, purge_expired,
    renew_consent, update_status, withdraw,
)
from ban.cli import parser, run
from ban.ledger import record


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cohort.db"

    def tearDown(self):
        self.temp.cleanup()

    def join(self, alias="Merchant_One", methods=None, consent=True, channel="community"):
        return enroll(
            self.path, alias=alias, currency="PHP",
            payment_methods=methods if methods is not None else ["GCash", "Bank Transfer"],
            channel=channel, consent_attested=consent,
        )

    def test_empty_read_does_not_create_db(self):
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 0)
        self.assertFalse(self.path.exists())

    def test_enrollment_requires_explicit_consent(self):
        with self.assertRaises(CohortError):
            self.join(consent=False)
        self.assertFalse(self.path.exists())

    def test_local_pseudonymous_enrollment(self):
        item = self.join()
        self.assertEqual(len(item["merchant_id"]), 32)
        self.assertEqual(item["visibility"], "local_only")
        self.assertEqual(item["consent_record"], "operator_attested")
        record = list_merchants(self.path, "PHP")
        self.assertEqual(record["total"], 1)
        self.assertEqual(record["merchants"][0]["payment_methods"], ["GCash", "Bank Transfer"])
        self.assertNotIn("email", json.dumps(record))
        self.assertNotIn("address", json.dumps(record))
        self.assertEqual(cohort_report(self.path, "PHP")["first_five_merchant_target_remaining"], 4)

    def test_active_requires_availability_attestation(self):
        item = self.join()
        with self.assertRaises(CohortError):
            update_status(self.path, merchant_id=item["merchant_id"], state="active")
        result = update_status(self.path, merchant_id=item["merchant_id"], state="active",
                               availability_attested=True)
        self.assertEqual(result["state_evidence"], "operator_attested")
        self.assertEqual(cohort_report(self.path, "PHP")["active_self_reported"], 1)
        update_status(self.path, merchant_id=item["merchant_id"], state="paused")
        self.assertEqual(cohort_report(self.path, "PHP")["active_self_reported"], 0)

    def test_withdrawal_is_hard_delete_and_attested(self):
        item = self.join()
        with self.assertRaises(CohortError):
            withdraw(self.path, merchant_id=item["merchant_id"], request_attested=False)
        self.assertTrue(withdraw(self.path, merchant_id=item["merchant_id"],
                                 request_attested=True)["deleted"])
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cohort_merchants").fetchone()[0], 0)

    def test_consent_renewal_and_expiration(self):
        item = self.join()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE cohort_merchants SET consent_until=?", (past,))
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 0)
        with self.assertRaises(CohortError):
            renew_consent(self.path, merchant_id=item["merchant_id"], consent_attested=False)
        self.assertEqual(renew_consent(self.path, merchant_id=item["merchant_id"],
                                       consent_attested=True)["consent_record"], "operator_attested")
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 1)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE cohort_merchants SET consent_until=?", (past,))
        self.assertEqual(purge_expired(self.path)["expired_profiles_deleted"], 1)
        self.assertEqual(list_merchants(self.path)["total"], 0)

    def test_duplicate_alias_casefolded(self):
        self.join()
        with self.assertRaises(CohortError):
            self.join(alias="merchant_one")

    def test_payment_rail_and_alias_validation(self):
        for alias in ("Some Person", "name@example.com", "b", "💥"):
            with self.subTest(alias=alias):
                with self.assertRaises(CohortError):
                    self.join(alias=alias)
        for rails in ([], ["a"], ["GCash", "gcash"], ["GCash; DROP TABLE"],
                      ["Bank Account 12345678901"], ["A" * 50]):
            with self.subTest(rails=rails):
                with self.assertRaises(CohortError):
                    self.join(methods=rails)

    def test_ledger_only_db_migrates_without_disruption(self):
        record(self.path, "merchant_interviewed", "PHP", "community")
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 0)
        self.join()
        self.assertEqual(cohort_report(self.path, "PHP")["enrolled"], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_cli_enroll_and_report(self):
        p = parser()
        args = p.parse_args(["merchant-enroll", "--alias", "LocalSeller", "--currency", "PHP",
                             "--method", "GCash", "--channel", "direct", "--attest-consent",
                             "--db", str(self.path)])
        created = run(args)
        self.assertEqual(created["state"], "onboarded")
        summary = run(p.parse_args(["cohort", "--currency", "PHP", "--db", str(self.path)]))
        self.assertEqual(summary["enrolled"], 1)
        self.assertEqual(summary["by_acquisition_channel"]["direct"], 1)
        self.assertNotIn("LocalSeller", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
