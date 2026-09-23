from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from aibridge.storage import Storage  # noqa: E402


class GatewayKeyStorageTests(unittest.TestCase):
    def test_new_key_can_be_revealed_after_reopen_without_exposing_it_in_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "aibridge.db"
            secret = root / "secret.key"
            storage = Storage(database, secret)
            key, token = storage.create_key("test client")

            with closing(sqlite3.connect(database)) as db:
                cipher = db.execute("SELECT secret_cipher FROM gateway_keys WHERE id=?", (key["id"],)).fetchone()[0]
            self.assertNotIn(token, cipher)
            self.assertNotIn("token", storage.list_keys()[0])

            reopened = Storage(database, secret)
            self.assertEqual(token, reopened.get_key_token(key["id"]))
            self.assertTrue(reopened.verify_key(token))
            self.assertTrue(reopened.list_keys()[0]["revealable"])

    def test_legacy_key_stays_valid_and_can_be_saved_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "aibridge.db"
            secret = root / "secret.key"
            storage = Storage(database, secret)
            key, token = storage.create_key("legacy client")
            with closing(sqlite3.connect(database)) as db:
                db.execute("ALTER TABLE gateway_keys DROP COLUMN secret_cipher")
                db.commit()

            legacy = Storage(database, secret)
            self.assertTrue(legacy.verify_key(token))
            self.assertFalse(legacy.list_keys()[0]["revealable"])
            self.assertIsNone(legacy.get_key_token(key["id"]))
            with self.assertRaises(ValueError):
                legacy.save_existing_key_token(key["id"], "wrong token")
            self.assertIsNone(legacy.get_key_token(key["id"]))

            legacy.save_existing_key_token(key["id"], token)
            self.assertEqual(token, Storage(database, secret).get_key_token(key["id"]))
