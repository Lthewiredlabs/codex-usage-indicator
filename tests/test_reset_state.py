import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from reset_state import ResetJournal, ResetStateError


class ResetJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state/reset-attempt.json"
        self.journal = ResetJournal(self.path)
        self.scope = "a" * 64

    def test_persists_and_reuses_existing_attempt(self):
        self.assertIsNone(self.journal.load())
        attempt = self.journal.begin(self.scope, "credit-one")
        self.assertEqual(str(uuid.UUID(attempt["idempotencyKey"])), attempt["idempotencyKey"])
        self.assertEqual(attempt, json.loads(self.path.read_text()))
        reopened = ResetJournal(self.path)
        self.assertEqual(reopened.load(), attempt)
        self.assertEqual(reopened.begin(self.scope, "credit-two"), attempt)

    def test_mismatched_account_does_not_overwrite(self):
        attempt = self.journal.begin(self.scope)
        with self.assertRaises(ResetStateError):
            self.journal.begin("b" * 64)
        self.assertEqual(self.journal.load(), attempt)

    def test_corrupted_state_is_not_overwritten_or_removed(self):
        self.path.parent.mkdir()
        for value in ["{", "null", "[]", json.dumps({
                "idempotencyKey": "not-a-uuid", "accountScope": self.scope}),
                json.dumps({"idempotencyKey": str(uuid.uuid4()), "accountScope": self.scope,
                            "token": "must-not-be-accepted"})]:
            with self.subTest(value=value):
                self.path.write_text(value)
                for operation in [self.journal.load, lambda: self.journal.begin(self.scope),
                                  lambda: self.journal.finish(str(uuid.uuid4()))]:
                    with self.assertRaises(ResetStateError):
                        operation()
                    self.assertEqual(self.path.read_text(), value)

    def test_invalid_account_is_refused_without_state(self):
        for scope in [None, "", "account@example.com", "a" * 63, "A" * 64]:
            with self.subTest(scope=scope), self.assertRaises(ResetStateError):
                self.journal.begin(scope)
        self.assertFalse(self.path.exists())

    def test_state_directory_and_files_are_private(self):
        self.journal.begin(self.scope)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o644)
        self.journal.load()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_finish_allows_new_key_and_leaves_no_history(self):
        first = self.journal.begin(self.scope)
        self.journal.finish(first["idempotencyKey"])
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.path.exists())
        self.journal.finish(first["idempotencyKey"])
        second = self.journal.begin(self.scope)
        self.assertNotEqual(first["idempotencyKey"], second["idempotencyKey"])
        self.assertEqual(set(second), {"accountScope", "idempotencyKey"})

    def test_late_finish_cannot_remove_a_newer_attempt(self):
        original = self.journal.begin(self.scope)
        late_caller = ResetJournal(self.path)
        late_attempt = late_caller.begin(self.scope)
        self.assertEqual(late_attempt, original)
        self.journal.finish(original["idempotencyKey"])
        newer = ResetJournal(self.path).begin(self.scope)
        self.assertNotEqual(newer["idempotencyKey"], original["idempotencyKey"])
        with self.assertRaises(ResetStateError):
            late_caller.finish(late_attempt["idempotencyKey"])
        self.assertEqual(self.journal.load(), newer)

    def test_failed_sync_does_not_return_an_attempt(self):
        with patch("reset_state.os.fsync", side_effect=OSError("disk failed")):
            with self.assertRaises(ResetStateError):
                self.journal.begin(self.scope)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".reset-attempt-*")), [])

    def test_interruption_after_replacement_reuses_the_saved_key(self):
        with patch.object(self.journal, "_sync_directory", side_effect=OSError("disk failed")):
            with self.assertRaises(ResetStateError):
                self.journal.begin(self.scope)
        saved = json.loads(self.path.read_text())
        with patch.object(self.journal, "_sync_directory", wraps=self.journal._sync_directory) as sync:
            self.assertEqual(self.journal.begin(self.scope), saved)
            sync.assert_called_once_with()

    def test_concurrent_callers_share_one_attempt(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            attempts = list(executor.map(
                lambda _: ResetJournal(self.path).begin(self.scope), range(8)))
        self.assertEqual(len({attempt["idempotencyKey"] for attempt in attempts}), 1)

    def test_default_path_respects_only_absolute_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.folder.name}):
            self.assertEqual(ResetJournal().path,
                             Path(self.folder.name) / "codex-usage-indicator/reset-attempt.json")
        for setting in ["relative", ""]:
            with patch.dict(os.environ, {"XDG_STATE_HOME": setting}):
                self.assertEqual(ResetJournal().path, Path.home() /
                                 ".local/state/codex-usage-indicator/reset-attempt.json")

    def test_symlink_state_is_not_followed(self):
        self.path.parent.mkdir()
        target = Path(self.folder.name) / "other.json"
        target.write_text("keep this")
        self.path.symlink_to(target)
        with self.assertRaises(ResetStateError):
            self.journal.begin(self.scope)
        self.assertEqual(target.read_text(), "keep this")


if __name__ == "__main__":
    unittest.main()
