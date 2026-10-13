#!/usr/bin/env python3
"""Session D-Bus service for Codex Quick Chat. Starting it never sends a prompt."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import threading
import time
from urllib.parse import quote

from chat_client import ChatClient, ChatError, ChatStopped, MAX_INPUT, ThreadStore, bounded_messages
from ui_model import build_view

BUS_NAME = "org.codex.QuickChat"
OBJECT_PATH = "/org/codex/QuickChat"
INTERFACE = "org.codex.QuickChat"
INTROSPECTION = '''<node><interface name="org.codex.QuickChat">
<method name="GetState"><arg type="s" direction="out" name="state"/></method>
<method name="Send"><arg type="s" direction="in" name="message"/></method>
<method name="Stop"/><method name="NewChat"/><method name="OpenChat"/><method name="RefreshUsage"/>
<method name="Quit"/>
<signal name="Changed"><arg type="s" name="state"/></signal>
</interface></node>'''


class ChatController:
    """UI-independent controller. Protocol and usage work stay off the main loop."""
    def __init__(self, store, codex=None, on_changed=None, client_factory=None, usage_reader=None, opener=None):
        self.store, self.codex = store, codex
        self.on_changed = on_changed or (lambda value: None)
        self.client_factory = client_factory or (lambda: ChatClient(store, codex))
        self.usage_reader = usage_reader or self._read_usage
        self.opener = opener or (lambda uri: None)
        self.lock = threading.RLock()
        self.worker = None
        self.usage_worker = None
        self.usage_process = None
        self.client = None
        self.cancel = threading.Event()
        self.closed = False
        self.generation = 0
        self.scope = None
        self.usage_snapshot = None
        self.usage_error = None
        self.state = {"status": "idle", "thread_id": None, "messages": [], "error": None,
                      "usage_label": "Codex …", "usage_stale": True}

    def get_state(self):
        with self.lock:
            view = build_view(self.usage_snapshot, time.time(), error=self.usage_error,
                              refreshing=self.usage_worker is not None)
            self.state["usage_label"], self.state["usage_stale"] = view["label"], view["stale"]
            return json.dumps(self.state, ensure_ascii=False, allow_nan=False)

    def publish(self):
        if not self.closed:
            self.on_changed(self.get_state())

    def _assert_available(self):
        if self.closed:
            raise ChatError("Quick Chat is closing.")
        if self.worker is not None:
            raise ChatError("Quick Chat is busy. Wait for the reply or press Stop.")

    def start(self):
        """Load persisted history without resuming a turn or sending a message."""
        with self.lock:
            self._assert_available()
            try:
                saved = self.store.load()
            except ChatError as error:
                self.state.update(status="error", error=str(error))
                saved = None
            if saved:
                self.scope = saved["account_scope"]
                self.cancel = threading.Event()
                self._start_worker(None, saved)
        self.refresh_usage()
        self.publish()

    def _start_worker(self, text, saved):
        generation = self.generation
        cancel = self.cancel
        worker = threading.Thread(target=self._run, args=(text, saved, generation, cancel), name="codex-quickchat", daemon=True)
        self.worker = worker
        worker.start()

    def send(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT:
            raise ChatError(f"Enter a message of 1–{MAX_INPUT} characters.")
        with self.lock:
            self._assert_available()
            saved = self.store.load()
            self.scope = saved["account_scope"] if saved else None
            self.cancel = threading.Event()
            self.state.update(status="thinking", error=None)
            self.state["messages"] = bounded_messages(self.state["messages"] + [{"role": "user", "text": text}])
            self._start_worker(text, saved)
        self.publish()

    def _run(self, text, saved, generation, cancel):
        client = None

        def update(values):
            with self.lock:
                if generation != self.generation or self.closed:
                    return
                self.state["thread_id"] = values.get("thread_id")
                self.state["messages"] = bounded_messages(values.get("messages", []))
                try:
                    stored = self.store.load()
                    self.scope = stored["account_scope"] if stored else None
                except ChatError:
                    pass
            self.publish()

        try:
            client = self.client_factory()
            with self.lock:
                self.client = client
            result = client.restore(saved, cancel) if text is None else client.send(text, saved, update, cancel)
            update(result)
            with self.lock:
                if generation == self.generation:
                    self.state.update(status="idle", error=None)
        except ChatStopped:
            with self.lock:
                if generation == self.generation:
                    self.state.update(status="idle", error=None)
        except ChatError as error:
            with self.lock:
                if generation == self.generation:
                    self.state.update(status="error", error=str(error))
                    if "sign-in changed" in str(error):
                        self.state.update(thread_id=None, messages=[])
        except Exception:
            with self.lock:
                if generation == self.generation:
                    self.state.update(status="error", error="Quick Chat could not finish. Open Codex to check before sending again.")
        finally:
            if client is not None:
                client.close()
            with self.lock:
                self.client = None
                self.worker = None
            self.publish()
            if text is not None and not self.closed:
                self.refresh_usage()

    def stop(self):
        with self.lock:
            if self.worker is not None:
                self.cancel.set()
        # Remain thinking until interruption/cleanup has actually completed.

    def new_chat(self):
        with self.lock:
            self._assert_available()
            self.store.clear()
            self.generation += 1
            self.scope = None
            self.state.update(status="idle", thread_id=None, messages=[], error=None)
        self.publish()

    def open_chat(self):
        with self.lock:
            thread_id = self.state["thread_id"]
        if not thread_id:
            raise ChatError("Send a message first to create a chat.")
        try:
            self.opener("codex://threads/" + quote(thread_id, safe=""))
        except Exception:
            raise ChatError("Could not open Codex. Open the app from your launcher.") from None

    def refresh_usage(self):
        with self.lock:
            if self.closed or self.usage_worker is not None:
                return
            worker = threading.Thread(target=self._refresh_usage, name="codex-quickchat-usage", daemon=True)
            self.usage_worker = worker
            worker.start()

    @staticmethod
    def _terminate_group(process):
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)

    def _read_usage(self):
        # Run the existing reader in its own group so shutdown also stops its
        # app-server child. This still uses usage_reader.read_usage; it simply
        # gives the service a cancellable boundary around the synchronous API.
        command = [sys.executable, "-c",
                   "import runpy,signal,sys; "
                   "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                   "runpy.run_path(sys.argv.pop(1), run_name='__main__')",
                   str(Path(__file__).with_name("usage_reader.py"))]
        if self.codex:
            command.extend(("--codex", self.codex))
        with self.lock:
            if self.closed:
                return {"status": "error"}
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            self.usage_process = process
        try:
            output, _ = process.communicate(timeout=22)
            if len(output) > 1024 * 1024:
                return {"status": "error"}
            result = json.loads(output)
            return result if isinstance(result, dict) else {"status": "error"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"status": "error"}
        finally:
            self._terminate_group(process)
            with self.lock:
                if self.usage_process is process:
                    self.usage_process = None

    def _refresh_usage(self):
        try:
            result = self.usage_reader()
            with self.lock:
                if result.get("status") == "ok":
                    self.usage_snapshot, self.usage_error = result, None
                    scope = result.get("accountScope")
                    if scope and self.scope and scope != self.scope:
                        # Drop old-account text immediately; do not migrate its thread.
                        self.cancel.set()
                        self.generation += 1
                        self.state.update(status="error", thread_id=None, messages=[],
                            error="The Codex sign-in changed. Choose New chat to keep conversations separate.")
                else:
                    self.usage_error = "Usage unavailable"
        except Exception:
            with self.lock:
                self.usage_error = "Usage unavailable"
        finally:
            with self.lock:
                self.usage_worker = None
            self.publish()

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
            worker = self.worker
            usage_worker, usage_process = self.usage_worker, self.usage_process
        self._terminate_group(usage_process)
        if usage_worker is not None and usage_worker is not threading.current_thread():
            usage_worker.join(timeout=2)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=4.5)
        # The client normally responds to cancel within 3s. Kill/reap on shutdown
        # even if startup or a broken peer did not follow the protocol.
        with self.lock:
            client = self.client
        if client is not None:
            client.close()


def quit_existing():
    """Uninstall helper: never activates an absent D-Bus service."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NO_AUTO_START, 1000, None)
        if owner.unpack()[0]:
            bus.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, "Quit", None, None, Gio.DBusCallFlags.NO_AUTO_START, 6000, None)
        return 0
    except GLib.Error:
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Codex executable (also supports a fake server in tests)")
    parser.add_argument("--state-dir", help="Private quick-chat state directory")
    parser.add_argument("--quit", action="store_true", help="Stop an existing service without starting one")
    args = parser.parse_args(argv)
    if args.quit:
        return quit_existing()
    from gi.repository import Gio, GLib
    try:
        store = ThreadStore(args.state_dir)
    except Exception:
        return 1
    loop = GLib.MainLoop()
    service = {"bus": None, "registered": 0}

    def emit(payload):
        def deliver():
            if service["bus"] is not None:
                try:
                    service["bus"].emit_signal(None, OBJECT_PATH, INTERFACE, "Changed", GLib.Variant("(s)", (payload,)))
                except GLib.Error:
                    pass
            return GLib.SOURCE_REMOVE
        GLib.idle_add(deliver)

    controller = ChatController(store, args.codex, on_changed=emit,
                               opener=lambda uri: Gio.AppInfo.launch_default_for_uri(uri, None))

    def shutdown():
        controller.close()
        loop.quit()
        return GLib.SOURCE_REMOVE

    def call(bus, sender, path, interface, method, parameters, invocation):
        try:
            if method == "GetState":
                invocation.return_value(GLib.Variant("(s)", (controller.get_state(),)))
                return
            if method == "Send":
                controller.send(parameters.unpack()[0])
            elif method == "Stop":
                controller.stop()
            elif method == "NewChat":
                controller.new_chat()
            elif method == "OpenChat":
                controller.open_chat()
            elif method == "RefreshUsage":
                controller.refresh_usage()
            elif method == "Quit":
                invocation.return_value(None)
                GLib.idle_add(shutdown)
                return
            else:
                raise ChatError("Unknown Quick Chat action.")
            invocation.return_value(None)
        except ChatError as error:
            invocation.return_dbus_error(INTERFACE + ".Error", str(error))
        except Exception:
            invocation.return_dbus_error(INTERFACE + ".Error", "Quick Chat could not complete that action.")

    def bus_acquired(bus, name):
        service["bus"] = bus
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        service["registered"] = bus.register_object(OBJECT_PATH, node.interfaces[0], call, None, None)

    def name_acquired(bus, name):
        controller.start()

    def name_lost(bus, name):
        shutdown()

    owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                             bus_acquired, name_acquired, name_lost)
    timer = GLib.timeout_add_seconds(60, lambda: (controller.refresh_usage(), GLib.SOURCE_CONTINUE)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown)
    try:
        loop.run()
    finally:
        controller.close()
        GLib.source_remove(timer)
        if service["registered"] and service["bus"]:
            service["bus"].unregister_object(service["registered"])
        Gio.bus_unown_name(owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
