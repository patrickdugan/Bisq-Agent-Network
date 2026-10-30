import ast
import csv
from hashlib import sha256
from pathlib import Path
import unittest

from ban.bip340 import verify, verify_hex
from ban.sim.testkeys import TestKey, _sign

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VECTORS = FIXTURES / "bip340_test_vectors.csv"
VECTORS_SHA256 = "34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d"


class Bip340VectorTests(unittest.TestCase):
    def test_vendored_csv_matches_recorded_source_hash(self):
        # Upstream bytes use CRLF; normalize in case a checkout rewrote line endings.
        data = VECTORS.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.assertEqual(sha256(data).hexdigest(), VECTORS_SHA256)
        source = (FIXTURES / "bip340_test_vectors.SOURCE").read_text(encoding="utf-8")
        self.assertIn("bitcoin/bips/200f9b26fe0a2f235a2af8b30c4be9f12f6bc9cb/bip-0340/test-vectors.csv", source)
        self.assertIn(VECTORS_SHA256, source)

    def test_every_vector_verifies_as_expected_and_signs_exactly(self):
        with VECTORS.open(newline="", encoding="ascii") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 19)
        for row in rows:
            with self.subTest(index=row["index"]):
                pubkey, msg, sig = (bytes.fromhex(row[k]) for k in ("public key", "message", "signature"))
                self.assertEqual(verify(pubkey, msg, sig), row["verification result"] == "TRUE")
                if row["secret key"]:
                    self.assertEqual(_sign(int(row["secret key"], 16), msg, bytes.fromhex(row["aux_rand"])), sig)

    def test_malformed_inputs_are_false_not_errors(self):
        key = TestKey("vector-check")
        sig = key.sign(b"\x07" * 32)
        self.assertTrue(verify_hex(key.pubkey, "07" * 32, sig))
        self.assertFalse(verify_hex(key.pubkey.upper(), "07" * 32, sig))
        self.assertFalse(verify_hex(key.pubkey, "07" * 32, sig[:-2]))
        self.assertFalse(verify_hex("zz" * 32, "07" * 32, sig))
        self.assertFalse(verify(b"\x00" * 31, b"", b"\x00" * 64))

    def test_signing_stays_in_the_simulation_package(self):
        src = Path(__file__).resolve().parents[1] / "src" / "ban"
        for path in src.glob("*.py"):
            if path.name == "cli.py":
                continue  # the CLI only dispatches to ban.sim.demo
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                    [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    self.assertFalse(name.startswith(("sim", "ban.sim")), path.name + " imports ban.sim")


if __name__ == "__main__":
    unittest.main()
