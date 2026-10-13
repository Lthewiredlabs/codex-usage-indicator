import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_client import ChatError, ChatStopped, ThreadStore
from chat_service import ChatController
from test_chat_client import SCOPE, fake_codex

USAGE = {"status": "ok", "fetchedAt": time.time(), "buckets": [
    {"id": "codex", "name": "Codex", "primary": {"usedPercent": 25}, "secondary": {"usedPercent": 40}}],
    "resetCredits": {"availableCount": 1}, "accountScope": SCOPE}


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("background operation did not finish")
        time.sleep(.005)


class FakeClient:
    def __init__(self, store, blocking=False, error=None):
        self.store, self.blocking, self.error = store, blocking, error
        self.calls = []
        self.closed = False

    def restore(self, saved, cancel):
        self.calls.append(("restore", saved))
        return {"thread_id": saved["thread_id"], "messages": [{"role": "assistant", "text": "Previous answer"}]}

    def send(self, text, saved, update, cancel):
        self.calls.append(("send", text))
        self.store.save("test-thread", SCOPE)
        result = {"thread_id": "test-thread", "messages": [{"role": "user", "text": text}, {"role": "assistant", "text": "Answer"}]}
        update(result)
        if self.blocking:
            cancel.wait(2)
            raise ChatStopped()
        if self.error:
            raise self.error
        return result

    def close(self):
        self.closed = True


class ChatServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = ThreadStore(self.folder / "state")
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:controller.close()
        self.temp.cleanup()

    def controller(self, **kwargs):
        controller = ChatController(self.store, usage_reader=lambda: USAGE, **kwargs)
        self.controllers.append(controller)
        return controller

    def state(self, controller):
        return json.loads(controller.get_state())

    def test_startup_without_saved_chat_never_creates_or_sends(self):
        client = FakeClient(self.store)
        controller = self.controller(client_factory=lambda: client)
        controller.start()
        wait_until(lambda: controller.usage_worker is None)
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.state(controller)["thread_id"])
        self.assertIn("75%", self.state(controller)["usage_label"])
        self.assertFalse(self.state(controller)["usage_stale"])

    def test_startup_restores_chat_and_open_uses_correct_deep_link(self):
        self.store.save("test-thread", SCOPE)
        client, opened = FakeClient(self.store), []
        controller = self.controller(client_factory=lambda: client, opener=opened.append)
        controller.start()
        wait_until(lambda: controller.worker is None)
        self.assertEqual(client.calls[0][0], "restore")
        self.assertEqual(self.state(controller)["messages"][0]["text"], "Previous answer")
        controller.open_chat()
        self.assertEqual(opened, ["codex://threads/test-thread"])
        self.assertFalse(any(call[0] == "send" for call in client.calls))

    def test_send_streams_then_stop_cleans_up_and_new_chat_never_sends(self):
        client = FakeClient(self.store, blocking=True)
        updates = []
        controller = self.controller(client_factory=lambda: client, on_changed=lambda value: updates.append(json.loads(value)))
        controller.send("Hello")
        wait_until(lambda: bool(client.calls))
        self.assertEqual(self.state(controller)["status"], "thinking")
        with self.assertRaises(ChatError):controller.send("Duplicate")
        controller.stop()
        wait_until(lambda: controller.worker is None)
        self.assertEqual(self.state(controller)["status"], "idle")
        self.assertTrue(client.closed)
        self.assertEqual(client.calls, [("send", "Hello")])
        controller.new_chat()
        self.assertEqual(self.state(controller)["messages"], [])
        self.assertIsNone(self.store.load())
        self.assertEqual(client.calls, [("send", "Hello")])

    def test_unknown_worker_exception_is_sanitized_and_busy_flag_cleared(self):
        controller = self.controller(client_factory=lambda: (_ for _ in ()).throw(RuntimeError("SECRET")))
        controller.send("Hello")
        wait_until(lambda: controller.worker is None)
        self.assertEqual(self.state(controller)["status"], "error")
        self.assertNotIn("SECRET", controller.get_state())
        controller.new_chat()

    def test_failed_usage_preserves_last_snapshot_as_stale(self):
        controller = self.controller()
        controller.refresh_usage()
        wait_until(lambda: controller.usage_worker is None)
        controller.usage_reader = lambda: {"status": "error", "error": "SECRET raw server"}
        controller.refresh_usage()
        wait_until(lambda: controller.usage_worker is None)
        self.assertTrue(self.state(controller)["usage_stale"])
        self.assertIn("75%", self.state(controller)["usage_label"])
        self.assertNotIn("SECRET", controller.get_state())

    def test_account_change_hides_previous_history_and_cancels_active_reply(self):
        client = FakeClient(self.store, blocking=True)
        controller = self.controller(client_factory=lambda: client)
        controller.send("Hello")
        wait_until(lambda: controller.scope == SCOPE)
        controller.usage_reader = lambda: {**USAGE, "accountScope": "a" * 64}
        controller.refresh_usage()
        wait_until(lambda: controller.usage_worker is None and controller.worker is None)
        state = self.state(controller)
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["messages"], [])
        self.assertIsNone(state["thread_id"])
        self.assertIn("sign-in changed", state["error"])

    def test_close_stops_chat_worker(self):
        client = FakeClient(self.store, blocking=True)
        controller = self.controller(client_factory=lambda: client)
        controller.send("Hello")
        wait_until(lambda: bool(client.calls))
        controller.close()
        self.assertIsNone(controller.worker)
        self.assertTrue(client.closed)

    def test_close_terminates_and_reaps_usage_reader_and_its_app_server(self):
        pidfile = self.folder / "usage-child-pid"
        binary = self.folder / "hanging-codex"
        binary.write_text('#!/usr/bin/python3\nimport os,time\n' + f'open({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(30)\n')
        binary.chmod(0o700)
        controller = ChatController(self.store, codex=str(binary))
        self.controllers.append(controller)
        controller.refresh_usage()
        wait_until(pidfile.exists)
        helper_pid = controller.usage_process.pid
        child_pid = int(pidfile.read_text())
        start = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNone(controller.usage_worker)
        self.assertIsNone(controller.usage_process)
        for pid in (helper_pid, child_pid):
            with self.assertRaises(ProcessLookupError):os.kill(pid, 0)

    def test_invalid_inputs_and_open_without_thread_do_not_start_workers(self):
        controller = self.controller()
        for text in ("", " ", "x" * 4001):
            with self.assertRaises(ChatError):controller.send(text)
        with self.assertRaises(ChatError):controller.open_chat()
        self.assertIsNone(controller.worker)

    @unittest.skipUnless(Path('/usr/bin/dbus-run-session').exists(), 'private D-Bus session unavailable')
    def test_real_dbus_contract_with_fake_codex_and_quit_does_not_activate(self):
        binary, log = fake_codex(self.folder)
        service = str(Path(__file__).resolve().parents[1] / 'chat_service.py')
        driver = self.folder / 'dbus-driver.py'
        driver.write_text(f'''import json,subprocess,sys,time
from gi.repository import Gio,GLib
bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
name='org.codex.QuickChat'
def call(method,params=None):
 return bus.call_sync(name,'/org/codex/QuickChat',name,method,params,None,Gio.DBusCallFlags.NO_AUTO_START,2000,None)
def state():return json.loads(call('GetState').unpack()[0])
assert subprocess.run([sys.executable,{service!r},'--quit']).returncode==0
proc=subprocess.Popen([sys.executable,{service!r},'--codex',{binary!r},'--state-dir',{str(self.folder / 'dbus-state')!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 deadline=time.monotonic()+4
 while True:
  try:state();break
  except GLib.Error:
   if time.monotonic()>deadline:raise
   time.sleep(.02)
 assert state()['messages']==[]
 call('Send',GLib.Variant('(s)',('Hello',)))
 deadline=time.monotonic()+4
 while state()['status']=='thinking':
  assert time.monotonic()<deadline
  time.sleep(.02)
 assert state()['messages'][-1]['text']=='Hello!',state()
 call('NewChat')
 assert state()['thread_id'] is None
 assert subprocess.run([sys.executable,{service!r},'--quit']).returncode==0
 proc.wait(timeout=5)
 assert proc.returncode==0
finally:
 if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
''')
        result = subprocess.run(['dbus-run-session', sys.executable, str(driver)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        methods = [r.get('method') for r in map(json.loads, log.read_text().splitlines())]
        self.assertEqual(methods.count('turn/start'), 1)


if __name__ == '__main__':
    unittest.main()
