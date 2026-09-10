import unittest

from ui_model import build_view, percent, remaining, reset_text, window_name


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.primary = {"usedPercent": 87, "windowDurationMins": 300, "resetsAt": 4600}
        self.secondary = {"usedPercent": 75, "windowDurationMins": 10080, "resetsAt": 90100}
        self.snapshot = {"status": "ok", "fetchedAt": 990, "buckets": [
            {"id": "codex", "name": "Codex", "primary": self.primary, "secondary": self.secondary}]}

    def test_reference_is_remaining_not_used(self):
        self.assertEqual(build_view(self.snapshot, self.now)["label"], "Codex 13% · 25%")

    def test_missing_is_not_zero(self):
        self.assertIsNone(remaining(None, self.now))
        self.assertIsNone(remaining({"usedPercent": None}, self.now))
        self.assertIsNone(remaining({"usedPercent": True}, self.now))
        self.assertEqual(percent(None), "—")
        self.assertEqual(percent(0), "0%")

    def test_reset_does_not_invent_full_quota(self):
        self.primary["resetsAt"] = self.now
        self.assertEqual(build_view(self.snapshot, self.now)["label"], "Codex — · 25%")
        self.assertIn("waiting for fresh usage", reset_text(self.primary, self.now))

    def test_stale_and_failed_requests_mark_last_known(self):
        self.assertEqual(build_view(self.snapshot, 1200)["label"], "Codex ~13% · 25%")
        view = build_view(self.snapshot, self.now, error="Offline")
        self.assertEqual(view["label"], "Codex ~13% · 25%")
        self.assertIn(("Offline", False), view["rows"])

    def test_prefer_codex_bucket_and_expose_other_buckets(self):
        self.snapshot["buckets"].insert(0, {"id": "other", "name": "Other quota", "primary": None})
        view = build_view(self.snapshot, self.now)
        self.assertEqual(view["label"], "Codex 13% · 25%")
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
        self.assertEqual(build_view(None, self.now)["label"], "Codex —")
        self.assertEqual(build_view(None, self.now, refreshing=True)["label"], "Codex …")


if __name__ == "__main__":
    unittest.main()
