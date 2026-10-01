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

from reset_state import ResetJournal, ResetStateError
from ui_model import available_credit_id, build_view, can_reset, reset_count

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
        self.resetting = False
        self.preparing_reset = False
        self.dialog = None
        self.reset_notice = None
        self.reset_state_error = None
        self.journal = ResetJournal()
        try:
            self.pending_reset = self.journal.load()
        except (OSError, ResetStateError):
            self.pending_reset = None
            self.reset_state_error = "Saved reset could not be read. Reset is disabled."
        if self.pending_reset:
            self.reset_notice = "A previous reset is unresolved. Retry it to check the result."

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
        self.indicator.set_label(view["label"], "Codex 100% · 100% · ↻99")
        reset_enabled = not (self.refreshing or self.resetting or self.dialog or self.reset_state_error) and can_reset(
            self.snapshot, time.time(), self.error, self.pending_reset)
        reset_label = "Resetting usage…" if self.resetting else (
            "Retry previous reset…" if self.pending_reset else "Reset usage…")
        for notice in (self.reset_notice, self.reset_state_error):
            if notice:
                view["rows"].append((notice, False))
        signature = (view["rows"], self.refreshing, self.resetting, bool(self.dialog), reset_label, bool(reset_enabled))
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
        reset = Gtk.MenuItem.new_with_label(reset_label)
        reset.set_sensitive(bool(reset_enabled))
        reset.connect("activate", lambda _item: self.request_reset())
        menu.append(reset)
        refresh = Gtk.MenuItem.new_with_label("Refreshing…" if self.refreshing else "Refresh now")
        refresh.set_sensitive(not (self.refreshing or self.resetting or self.dialog))
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
        if self.refreshing or self.resetting or self.dialog or self.stopping.is_set():
            return
        self.refreshing = True
        self.last_attempt = time.monotonic()
        self.render()
        threading.Thread(target=self.fetch, name="usage-read", daemon=True).start()

    def fetch(self, attempt=None):
        command = [sys.executable, str(APP_DIR / "usage_reader.py"), "--timeout", "18"]
        if self.codex:
            command.extend(["--codex", self.codex])
        if attempt:
            command.extend(["--reset", "--idempotency-key", attempt["idempotencyKey"],
                            "--account-scope", attempt["accountScope"]])
            if attempt.get("creditId"):
                command.extend(["--credit-id", attempt["creditId"]])
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
            payload = {"status": "error", "error": (
                "Reset result unknown. Retry the previous reset to check it safely." if attempt else
                "Could not refresh usage. Check Codex sign-in and connection.")}
        finally:
            if process is not None:
                stop_process_group(process)
                process.wait()
            with self.process_lock:
                self.process = None
        if not self.stopping.is_set():
            GLib.idle_add(self.finish_reset if attempt else self.finish_fetch, payload)

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
        if self.preparing_reset:
            self.preparing_reset = False
            self.show_reset_confirmation()
        return GLib.SOURCE_REMOVE

    def request_reset(self):
        if self.dialog:
            self.dialog.present()
            return
        if self.refreshing or self.resetting or self.reset_state_error or self.stopping.is_set():
            return
        self.preparing_reset = True
        self.refresh()  # Confirm against a fresh reading, not the previous minute's data.

    def show_reset_confirmation(self):
        if not can_reset(self.snapshot, time.time(), self.error, self.pending_reset):
            self.reset_notice = "Reset unavailable. Check the available count and Codex sign-in."
            self.render()
            return
        scope = self.snapshot["accountScope"]
        credit_id = self.pending_reset.get("creditId") if self.pending_reset else available_credit_id(self.snapshot, time.time())
        if self.pending_reset:
            title = "Retry the previous usage reset?"
            description = ("This checks the same saved reset request. If it already succeeded, "
                           "another reset will not be spent. If it did not reach Codex, this will use one reset.")
            button = "Retry previous reset"
        else:
            title = "Use one usage reset?"
            count = reset_count(self.snapshot)
            description = (f"You have {count} available reset{'s' if count != 1 else ''}. "
                           "This uses one reset to restore eligible Codex usage limits. "
                           "A used reset cannot be returned.")
            button = "Use 1 reset"
        dialog = Gtk.MessageDialog(
            application=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text=title,
        )
        dialog.set_title("Codex Usage")
        dialog.format_secondary_text(description)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(button, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        self.dialog = dialog
        dialog.connect("response", lambda current, response: self.confirm_reset(current, response, scope, credit_id))
        dialog.show_all()
        dialog.present()
        self.render()

    def confirm_reset(self, dialog, response, scope, credit_id):
        # Ignore a duplicate callback; only the displayed confirmation can authorize a reset.
        if dialog is not self.dialog:
            return
        self.dialog = None
        dialog.destroy()
        if response != Gtk.ResponseType.OK or self.stopping.is_set():
            self.render()
            return
        if not can_reset(self.snapshot, time.time(), self.error, self.pending_reset) or self.snapshot["accountScope"] != scope:
            self.reset_notice = "Usage changed while confirming. Refresh and try again."
            self.render()
            return
        try:
            # Persist before sending so a restart cannot turn a retry into a second reset.
            self.pending_reset = self.journal.begin(scope, credit_id)
        except (OSError, ResetStateError):
            self.reset_state_error = "Could not save the reset request. No reset was sent."
            self.render()
            return
        self.resetting = True
        self.reset_notice = "Applying your usage reset…"
        self.render()
        threading.Thread(target=self.fetch, args=(self.pending_reset.copy(),), name="usage-reset", daemon=True).start()

    def finish_reset(self, payload):
        if self.stopping.is_set():
            return GLib.SOURCE_REMOVE
        self.resetting = False
        messages = {
            "reset": "Usage reset applied.",
            "alreadyRedeemed": "The previous reset was already applied. No additional reset was used.",
            "noCredit": "No resets are available. No reset was used.",
            "nothingToReset": "There is no eligible usage to reset. No reset was used.",
        }
        outcome = payload.get("outcome")
        if payload.get("status") == "ok" and outcome in messages:
            self.reset_notice = messages[outcome]
            try:
                self.journal.finish(self.pending_reset["idempotencyKey"])
                self.pending_reset = None
            except (OSError, ResetStateError):
                self.reset_state_error = "Reset result received, but its saved request could not be cleared."
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage.get("status") == "ok":
                self.snapshot = usage
                self.error = None
            else:
                self.snapshot = None
                self.error = "Waiting for fresh usage after the reset request."
        else:
            self.reset_notice = payload.get("error") or "Reset result unknown. Retry the previous reset to check it safely."
            # Keep the saved request on every error, including uncertain network outcomes.
            self.error = "Usage may have changed. Refreshing…"
        self.render()
        self.refresh()
        return GLib.SOURCE_REMOVE

    def close(self):
        self.stopping.set()
        if self.dialog:
            self.dialog.destroy()
            self.dialog = None
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
