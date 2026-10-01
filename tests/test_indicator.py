"""Exercise reset confirmation and callbacks without a window or real Codex calls."""

import time
import unittest
from unittest.mock import Mock, patch

try:
    import indicator
except (ImportError, ValueError) as error:
    raise unittest.SkipTest("GTK/AppIndicator dependencies are required for UI callback tests") from error


class ResetInteractionTests(unittest.TestCase):
    def setUp(self):
        self.journal = Mock()
        self.journal.load.return_value = None
        self.attempt = {"idempotencyKey": "ed606708-e2e8-4f4c-b74a-38ad6819c98b", "accountScope": "a" * 64}
        self.journal.begin.return_value = self.attempt.copy()
        with patch.object(indicator, "ResetJournal", return_value=self.journal):
            self.app = indicator.UsageIndicator()
        self.app.snapshot = {"status": "ok", "fetchedAt": time.time(), "buckets": [],
                             "accountScope": "a" * 64, "resetCredits": {"availableCount": 2}}
        self.app.refresh = Mock()

    def test_opening_action_only_refreshes(self):
        self.app.request_reset()
        self.app.refresh.assert_called_once_with()
        self.assertTrue(self.app.preparing_reset)
        self.journal.begin.assert_not_called()

    def test_confirmation_defaults_to_cancel_and_cancel_never_starts_request(self):
        with patch.object(indicator.Gtk, "MessageDialog") as factory, patch.object(indicator.threading, "Thread") as worker:
            self.app.show_reset_confirmation()
            dialog = factory.return_value
            dialog.set_default_response.assert_called_once_with(indicator.Gtk.ResponseType.CANCEL)
            self.app.confirm_reset(dialog, indicator.Gtk.ResponseType.CANCEL, "a" * 64, None)
            self.journal.begin.assert_not_called()
            worker.assert_not_called()

    def test_confirm_saves_attempt_before_sending_and_ignores_double_response(self):
        dialog = Mock()
        self.app.dialog = dialog
        events = []
        self.journal.begin.side_effect = lambda *_args: events.append("saved") or self.attempt.copy()
        with patch.object(indicator.threading, "Thread") as worker:
            worker.return_value.start.side_effect = lambda: events.append("started")
            self.app.confirm_reset(dialog, indicator.Gtk.ResponseType.OK, "a" * 64, None)
            self.app.confirm_reset(dialog, indicator.Gtk.ResponseType.OK, "a" * 64, None)
            self.assertEqual(events, ["saved", "started"])
            self.assertTrue(self.app.resetting)
            self.assertEqual(worker.call_args.kwargs["args"], (self.attempt,))

    def test_stale_confirmation_and_failed_save_never_send(self):
        for stale in (True, False):
            with self.subTest(stale=stale), patch.object(indicator.threading, "Thread") as worker:
                dialog = Mock()
                self.app.dialog = dialog
                self.app.snapshot["fetchedAt"] = time.time() - (200 if stale else 0)
                self.journal.begin.side_effect = indicator.ResetStateError("disk failure")
                self.app.confirm_reset(dialog, indicator.Gtk.ResponseType.OK, "a" * 64, None)
                worker.assert_not_called()

    def test_uncertain_result_keeps_pending_attempt(self):
        self.app.pending_reset = self.attempt.copy()
        self.app.resetting = True
        self.app.finish_reset({"status": "error", "uncertain": True})
        self.assertEqual(self.app.pending_reset, self.attempt)
        self.journal.finish.assert_not_called()
        self.assertFalse(self.app.resetting)
        self.app.refresh.assert_called_once_with()

    def test_known_success_with_failed_usage_read_clears_attempt_without_inventing_usage(self):
        self.app.pending_reset = self.attempt.copy()
        self.app.finish_reset({"status": "ok", "outcome": "reset", "usage": None})
        self.journal.finish.assert_called_once_with(self.attempt["idempotencyKey"])
        self.assertIsNone(self.app.pending_reset)
        self.assertIsNone(self.app.snapshot)
        self.assertEqual(self.app.reset_notice, "Usage reset applied.")
        self.app.refresh.assert_called_once_with()

    def test_nocredit_and_nothingtoresets_are_definitive_without_success_claim(self):
        for outcome in ("noCredit", "nothingToReset"):
            with self.subTest(outcome=outcome):
                self.app.pending_reset = self.attempt.copy()
                self.app.finish_reset({"status": "ok", "outcome": outcome})
                self.assertIsNone(self.app.pending_reset)
                self.assertIn("No reset was used", self.app.reset_notice)

    def test_failed_journal_load_disables_reset(self):
        self.journal.load.side_effect = indicator.ResetStateError("invalid state")
        with patch.object(indicator, "ResetJournal", return_value=self.journal):
            app = indicator.UsageIndicator()
        app.refresh = Mock()
        app.request_reset()
        app.refresh.assert_not_called()
        self.assertIsNotNone(app.reset_state_error)


if __name__ == "__main__":
    unittest.main()
