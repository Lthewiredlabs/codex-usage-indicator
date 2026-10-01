import unittest

from ui_model import (available_credit_id, build_view, can_reset, percent, remaining,
                      reset_count, reset_text, window_name)


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.primary = {"usedPercent": 87, "windowDurationMins": 300, "resetsAt": 4600}
        self.secondary = {"usedPercent": 75, "windowDurationMins": 10080, "resetsAt": 90100}
        self.snapshot = {"status": "ok", "fetchedAt": 990, "buckets": [
            {"id": "codex", "name": "Codex", "primary": self.primary, "secondary": self.secondary}]}

    def test_reference_is_remaining_not_used(self):
        self.assertEqual(build_view(self.snapshot, self.now)["label"], "Codex 13% · 25% · ↻?")

    def test_missing_is_not_zero(self):
        self.assertIsNone(remaining(None, self.now))
        self.assertIsNone(remaining({"usedPercent": None}, self.now))
        self.assertIsNone(remaining({"usedPercent": True}, self.now))
        self.assertEqual(percent(None), "—")
        self.assertEqual(percent(0), "0%")

    def test_reset_does_not_invent_full_quota(self):
        self.primary["resetsAt"] = self.now
        self.assertEqual(build_view(self.snapshot, self.now)["label"], "Codex — · 25% · ↻?")
        self.assertIn("waiting for fresh usage", reset_text(self.primary, self.now))

    def test_stale_and_failed_requests_mark_last_known(self):
        self.assertEqual(build_view(self.snapshot, 1200)["label"], "Codex ~13% · 25% · ↻?")
        view = build_view(self.snapshot, self.now, error="Offline")
        self.assertEqual(view["label"], "Codex ~13% · 25% · ↻?")
        self.assertIn(("Offline", False), view["rows"])

    def test_prefer_codex_bucket_and_expose_other_buckets(self):
        self.snapshot["buckets"].insert(0, {"id": "other", "name": "Other quota", "primary": None})
        view = build_view(self.snapshot, self.now)
        self.assertEqual(view["label"], "Codex 13% · 25% · ↻?")
        self.assertIn(("Other quota", True), view["rows"])

    def test_no_codex_bucket_keeps_identity(self):
        self.snapshot["buckets"][0]["id"] = "other"
        self.snapshot["buckets"][0]["name"] = "Other quota"
        self.assertTrue(build_view(self.snapshot, self.now)["label"].startswith("Other quota "))

    def test_window_labels_follow_provider(self):
        self.assertEqual(window_name(self.primary, "Unknown"), "5-hour")
        self.assertEqual(window_name(self.secondary, "Unknown"), "Weekly")
        self.assertEqual(window_name({"windowDurationMins": 15}, "Unknown"), "15-minute")

    def test_nonfinite_and_fractional_values(self):
        self.assertIsNone(remaining({"usedPercent": float("nan")}, self.now))
        self.assertEqual(percent(0.2), "<1%")
        self.assertEqual(remaining({"usedPercent": 110}, self.now), 0)

    def test_no_data_states(self):
        self.assertEqual(build_view(None, self.now)["label"], "Codex — · ↻?")
        self.assertEqual(build_view(None, self.now, refreshing=True)["label"], "Codex … · ↻?")

    def test_reset_count_zero_unknown_and_stale_are_distinct(self):
        for value in (None, True, -1, 1.2, "2"):
            self.snapshot["resetCredits"] = {"availableCount": value}
            self.assertIsNone(reset_count(self.snapshot))
        self.snapshot["resetCredits"] = {"availableCount": 0}
        self.assertTrue(build_view(self.snapshot, self.now)["label"].endswith("↻0"))
        self.snapshot["resetCredits"] = {"availableCount": 2}
        self.assertTrue(build_view(self.snapshot, self.now)["label"].endswith("↻2"))
        self.assertTrue(build_view(self.snapshot, self.now, error="Offline")["label"].endswith("↻~2"))

    def test_reset_requires_fresh_count_and_verified_signin(self):
        self.snapshot["resetCredits"] = {"availableCount": 2}
        self.assertFalse(can_reset(self.snapshot, self.now))
        self.snapshot["accountScope"] = "a" * 64
        self.assertTrue(can_reset(self.snapshot, self.now))
        self.assertFalse(can_reset(self.snapshot, self.now, "Offline"))
        self.assertFalse(can_reset(self.snapshot, 1200))
        self.snapshot["resetCredits"]["availableCount"] = 0
        self.assertFalse(can_reset(self.snapshot, self.now))
        self.assertTrue(can_reset(self.snapshot, self.now, pending={"accountScope": "a" * 64}))
        self.assertFalse(can_reset(self.snapshot, self.now, pending={"accountScope": "b" * 64}))

    def test_credit_selection_skips_expired_and_uses_earliest_expiry(self):
        self.snapshot["resetCredits"] = {"availableCount": 4, "credits": [
            {"id": "expired", "status": "available", "expiresAt": 10},
            {"id": "forever", "status": "available", "expiresAt": None},
            {"id": "later", "status": "available", "expiresAt": 3000},
            {"id": "soon", "status": "available", "expiresAt": 2000},
            {"id": "used", "status": "redeemed", "expiresAt": 1500},
        ]}
        self.assertEqual(available_credit_id(self.snapshot, self.now), "soon")
        self.snapshot["resetCredits"]["credits"] = None
        self.assertIsNone(available_credit_id(self.snapshot, self.now))


if __name__ == "__main__":
    unittest.main()
