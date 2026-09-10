#!/usr/bin/env python3
"""Small GNOME/AppIndicator app for the signed-in Codex account's usage."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gio, GLib, Gtk

from ui_model import build_view

APP_ID = "io.github.lthewired.CodexUsageIndicator"
APP_DIR = Path(__file__).resolve().parent


def stop_process_group(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class UsageIndicator(Gtk.Application):
    def __init__(self, codex=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.codex = codex
        self.indicator = None
        self.snapshot = None
        self.error = None
        self.refreshing = False
        self.stopping = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.timer = None
        self.last_attempt = 0
        self.menu_signature = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        for name, callback in (("quit", self.close), ("refresh", self.refresh)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)

    def do_activate(self):
        if self.indicator is not None:
            return
        self.hold()
        self.indicator = AppIndicator.Indicator.new(
            "codex-usage", str(APP_DIR / "icons" / "codex-usage-symbolic.svg"),
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_title("Codex Usage")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.render()
        self.refresh()
        self.timer = GLib.timeout_add_seconds(5, self.tick)

    def tick(self):
        if self.stopping.is_set():
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self.last_attempt >= 60:
            self.refresh()
        self.render()
        return GLib.SOURCE_CONTINUE

    def render(self):
        if self.indicator is None or self.stopping.is_set():
            return
        view = build_view(self.snapshot, time.time(), self.error, self.refreshing)
        self.indicator.set_label(view["label"], "Codex 100% · 100%")
        signature = (view["rows"], self.refreshing)
        if self.menu_signature == signature:
            return
        self.menu_signature = signature
        menu = Gtk.Menu()
        for text, heading in view["rows"]:
            if heading and menu.get_children():
                menu.append(Gtk.SeparatorMenuItem())
            item = Gtk.MenuItem.new_with_label(text)
            item.set_sensitive(False)
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        refresh = Gtk.MenuItem.new_with_label("Refreshing…" if self.refreshing else "Refresh now")
        refresh.set_sensitive(not self.refreshing)
        refresh.connect("activate", lambda _item: self.refresh())
        menu.append(refresh)
        quit_item = Gtk.MenuItem.new_with_label("Quit Codex Usage")
        quit_item.connect("activate", lambda _item: self.close())
        menu.append(quit_item)
        menu.show_all()
        old_menu = getattr(self, "menu", None)
        self.menu = menu
        self.indicator.set_menu(menu)
        if old_menu is not None:
            old_menu.destroy()

    def refresh(self):
        if self.refreshing or self.stopping.is_set():
            return
        self.refreshing = True
        self.last_attempt = time.monotonic()
        self.render()
        threading.Thread(target=self.fetch, name="usage-read", daemon=True).start()

    def fetch(self):
        command = [sys.executable, str(APP_DIR / "usage_reader.py"), "--timeout", "18"]
        if self.codex:
            command.extend(["--codex", self.codex])
        process = None
        try:
            with self.process_lock:
                if self.stopping.is_set():
                    return
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, start_new_session=True,
                )
                self.process = process
            output, _ = process.communicate(timeout=23)
            payload = json.loads(output)
            if not isinstance(payload, dict) or payload.get("status") not in ("ok", "error"):
                raise ValueError("Unexpected usage response")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            payload = {"status": "error", "error": "Could not refresh usage. Check Codex sign-in and connection."}
        finally:
            if process is not None:
                stop_process_group(process)
                process.wait()
            with self.process_lock:
                self.process = None
        if not self.stopping.is_set():
            GLib.idle_add(self.finish_fetch, payload)

    def finish_fetch(self, payload):
        if self.stopping.is_set():
            return GLib.SOURCE_REMOVE
        self.refreshing = False
        if payload.get("status") == "ok":
            self.snapshot = payload
            self.error = None
        else:
            self.error = payload.get("error") or "Usage unavailable. Check Codex sign-in."
        self.render()
        return GLib.SOURCE_REMOVE

    def close(self):
        self.stopping.set()
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        with self.process_lock:
            stop_process_group(self.process)
        if self.indicator:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        self.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quit", action="store_true", help="Stop the running indicator")
    parser.add_argument("--codex", help="Path to the Codex executable")
    args = parser.parse_args()
    if args.quit:
        app = Gio.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.IS_LAUNCHER)
        app.register(None)
        if app.get_is_remote():
            app.activate_action("quit", None)
            Gio.DBusConnection.flush_sync(app.get_dbus_connection(), None)
        return 0
    app = UsageIndicator(args.codex)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: app.close() or False)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: app.close() or False)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
