"""Exercise component packaging in temporary homes; never activate a live service."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import install
import uninstall


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="codex-install-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.home = self.root / "home with spaces and 50%"
        self.source = self.root / "source"
        for name in install.ALL_FILES:
            self.write(self.source / name, "fixture " + name + "\n")
        for name in install.EXTENSION_FILES:
            self.write(self.source / "extension" / name, "extension fixture " + name + "\n")
        self.write(self.source / "extension/metadata.json", json.dumps({
            "uuid": install.EXTENSION_UUID, "shell-version": ["50"]}))
        self.codex = "/opt/Codex 50%/codex"
        self.addCleanup(patch.stopall)
        patch("install.__file__", str(self.source / "install.py")).start()
        self.dependencies = patch("install.check_dependencies", return_value=self.codex).start()
        self.process = patch("subprocess.run").start()
        self.output = io.StringIO()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def install(self, **kwargs):
        with redirect_stdout(self.output):
            return install.install(self.home, **kwargs)

    def uninstall(self, **kwargs):
        with redirect_stdout(self.output):
            uninstall.uninstall(self.home, **kwargs)

    def manifest(self):
        return json.loads((install.locations(self.home)[0] / install.MARKER).read_text())

    def tearDown(self):
        self.process.assert_not_called()

    def test_default_remains_indicator_only_and_needs_no_chat_files(self):
        (self.source / "chat_service.py").unlink()
        target = self.install()
        _target, desktop, autostart = install.locations(self.home)
        self.assertTrue(desktop.is_file())
        self.assertTrue(autostart.is_file())
        self.assertFalse((target / "chat_client.py").exists())
        self.assertFalse(install.chat_locations(self.home)[0].exists())
        self.assertEqual(self.manifest()["components"], ["indicator"])
        self.dependencies.assert_called_once_with(indicator=True, chat=False)
        self.assertIn('"/opt/Codex 50%%/codex"', desktop.read_text())

    def test_chat_only_installs_complete_extension_and_on_demand_service(self):
        target = self.install(chat_only=True)
        extension, service = install.chat_locations(self.home)
        for name in install.EXTENSION_FILES:
            self.assertEqual((extension / name).read_bytes(), (self.source / "extension" / name).read_bytes())
        self.assertFalse(install.locations(self.home)[1].exists())
        self.assertFalse(install.locations(self.home)[2].exists())
        self.assertFalse((target / "indicator.py").exists())
        self.assertEqual(self.manifest()["components"], ["chat"])
        self.dependencies.assert_called_once_with(indicator=False, chat=True)
        line = next(row for row in service.read_text().splitlines() if row.startswith("Exec="))
        self.assertEqual(shlex.split(line[5:]), ["/usr/bin/python3", str(target / "chat_service.py"), "--codex", self.codex])
        self.assertFalse((self.home / ".config/systemd").exists())

    def test_chat_only_preserves_existing_indicator_autostart_and_launcher(self):
        target = self.install()
        _, desktop, autostart = install.locations(self.home)
        original = (desktop.read_bytes(), autostart.read_bytes(), (target / "indicator.py").read_bytes())
        self.install(chat_only=True, autostart_enabled=False)
        self.assertEqual((desktop.read_bytes(), autostart.read_bytes(), (target / "indicator.py").read_bytes()), original)
        self.assertEqual(self.manifest()["components"], ["chat", "indicator"])

    def test_default_update_keeps_quick_chat_installed(self):
        self.install(chat_only=True)
        extension, service = install.chat_locations(self.home)
        previous = service.read_bytes()
        self.install()
        self.assertEqual(service.read_bytes(), previous)
        self.assertTrue((extension / "extension.js").is_file())
        self.assertEqual(self.manifest()["components"], ["chat", "indicator"])

    def test_no_autostart_backs_up_before_removing_existing_startup(self):
        self.install()
        autostart = install.locations(self.home)[2]
        old = autostart.read_bytes()
        self.install(autostart_enabled=False)
        self.assertFalse(autostart.exists())
        backups = list((self.home / ".local/share/codex-usage-indicator-backups").glob("*/autostart.desktop"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old)

    def test_legacy_indicator_manifest_upgrades_without_losing_component(self):
        target = self.install()
        self.write(target / install.MARKER, json.dumps({"app": install.APP_NAME, "version": "0.2.0", "files": install.FILES}))
        self.install(chat_only=True)
        self.assertEqual(self.manifest()["components"], ["chat", "indicator"])
        self.assertTrue((target / "reset_state.py").is_file())

    def test_chat_removal_preserves_indicator_reset_state_and_history(self):
        target = self.install(with_chat=True)
        state = self.home / ".local/state/codex-usage-indicator/reset-attempt.json"
        history = self.home / ".codex/sessions/conversation.jsonl"
        chat_state = self.home / ".local/state/codex-usage-indicator/quickchat/thread.json"
        self.write(state, "pending reset")
        self.write(history, "private chat fixture")
        self.write(chat_state, "saved thread fixture")
        self.uninstall(chat_only=True)
        self.assertEqual(self.manifest()["components"], ["indicator"])
        self.assertTrue((target / "indicator.py").is_file())
        self.assertTrue(install.locations(self.home)[2].is_file())
        self.assertFalse(install.chat_locations(self.home)[0].exists())
        self.assertFalse(install.chat_locations(self.home)[1].exists())
        self.assertFalse((target / "chat_service.py").exists())
        self.assertEqual(state.read_text(), "pending reset")
        self.assertEqual(history.read_text(), "private chat fixture")
        self.assertEqual(chat_state.read_text(), "saved thread fixture")

    def test_indicator_removal_keeps_chat_and_shared_helpers(self):
        target = self.install(with_chat=True)
        self.uninstall(indicator_only=True)
        self.assertEqual(self.manifest()["components"], ["chat"])
        self.assertTrue((target / "usage_reader.py").is_file())
        self.assertTrue((target / "chat_service.py").is_file())
        self.assertFalse((target / "indicator.py").exists())
        self.assertFalse(install.locations(self.home)[1].exists())
        self.assertFalse(install.locations(self.home)[2].exists())
        self.assertTrue(install.chat_locations(self.home)[1].is_file())

    def test_full_remove_preserves_unrelated_files_and_can_reinstall(self):
        target = self.install(with_chat=True)
        extension = install.chat_locations(self.home)[0]
        self.write(target / "personal-notes.txt", "keep me")
        self.write(target / "__pycache__/example.pyc", "cache fixture")
        self.write(extension / "personal-notes.txt", "keep extension notes")
        self.uninstall()
        self.assertEqual((target / "personal-notes.txt").read_text(), "keep me")
        self.assertEqual((extension / "personal-notes.txt").read_text(), "keep extension notes")
        self.assertEqual(self.manifest()["components"], [])
        self.install(with_chat=True)
        self.assertTrue((target / "indicator.py").exists())
        self.assertTrue((extension / "extension.js").exists())

    def test_clean_full_remove_removes_managed_directories(self):
        target = self.install(with_chat=True)
        self.uninstall()
        self.assertFalse(target.exists())
        self.assertFalse(install.chat_locations(self.home)[0].exists())
        self.assertTrue((self.home / ".local/share/codex-usage-indicator-backups").is_dir())

    def test_unowned_application_file_is_not_overwritten_or_removed(self):
        target = self.install()
        self.write(target / "chat_service.py", "unrelated script")
        with self.assertRaisesRegex(RuntimeError, "unrelated application file"):
            self.install(chat_only=True)
        self.uninstall()
        self.assertEqual((target / "chat_service.py").read_text(), "unrelated script")

    def test_unowned_extension_collision_stops_before_app_changes(self):
        target = self.install()
        before = (target / install.MARKER).read_bytes()
        extension = install.chat_locations(self.home)[0]
        self.write(extension / "extension.js", "not managed")
        with self.assertRaisesRegex(RuntimeError, "unrelated files"):
            self.install(with_chat=True)
        self.assertEqual((target / install.MARKER).read_bytes(), before)
        self.assertEqual((extension / "extension.js").read_text(), "not managed")

    def test_service_collision_and_symlink_are_rejected(self):
        _extension, service = install.chat_locations(self.home)
        self.write(service, "unrelated service")
        with self.assertRaisesRegex(RuntimeError, "unrelated launcher or service"):
            self.install(chat_only=True)
        self.assertEqual(service.read_text(), "unrelated service")
        service.unlink()
        outside = self.root / "outside.py"
        self.write(outside, "untouched")
        target = self.install()
        (target / "indicator.py").unlink()
        (target / "indicator.py").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.install()
        self.assertEqual(outside.read_text(), "untouched")

    def test_missing_extension_frame_is_detected_before_update(self):
        target = self.install()
        before = (target / install.MARKER).read_bytes()
        (self.source / "extension" / install.EXTENSION_ICONS[0]).unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing application file"):
            self.install(with_chat=True)
        self.assertEqual((target / install.MARKER).read_bytes(), before)
        self.assertFalse(install.chat_locations(self.home)[1].exists())

    def test_desktop_special_characters_and_newline_rejection(self):
        self.assertEqual(install.desktop_quote('path % " $ ` \\'), '"path %% \\" \\$ \\` \\\\"')
        with self.assertRaisesRegex(RuntimeError, "line breaks"):
            install.desktop_quote("bad\npath")


if __name__ == "__main__":
    unittest.main()
