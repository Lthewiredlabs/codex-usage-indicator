import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

from chat_client import (ChatClient, ChatError, ChatStopped, DISABLED_FEATURES, MAX_INPUT,
                         ThreadStore, bounded_messages, history_messages, reject_server_request)
from usage_reader import account_scope

ACCOUNT = {"account": {"type": "chatgpt", "email": "quickchat@example.invalid"}}
SCOPE = account_scope(ACCOUNT)
HISTORY = [{"id": "old-turn", "items": [
    {"type": "userMessage", "content": [{"type": "text", "text": "Earlier question"}]},
    {"type": "commandExecution", "aggregatedOutput": "PRIVATE TOOL OUTPUT"},
    {"type": "agentMessage", "text": "Earlier answer"}]}]


def fake_codex(folder, **options):
    log = Path(folder) / "fake-requests.jsonl"
    config = {"features": {name: False for name in DISABLED_FEATURES}, "approval_policy": "never",
              "sandbox_mode": "read-only", "web_search": "disabled", "project_doc_max_bytes": 0,
              "mcp_servers": {}}
    if options.get("unsafe"):
        config["features"]["shell_tool"] = True
    script = Path(folder) / "fake-codex"
    script.write_text('#!/usr/bin/python3\n' + f'''
import json,os,sys,time
options={options!r}
config={config!r}
account={ACCOUNT!r}
history={HISTORY!r}
log={str(log)!r}
def record(value):
    with open(log,'a') as out:out.write(json.dumps(value)+'\\n')
def send(value):print(json.dumps(value),flush=True)
def event(method,params):send({{'method':method,'params':params}})
def result(req,value):send({{'id':req['id'],'result':value}})
record({{'pid':os.getpid(),'argv':sys.argv[1:]}})
if options.get('mcp'):
    disabled='mcp_servers.test.enabled=false' in sys.argv
    config['mcp_servers']={{'test':{{'enabled':False if disabled else True}}}}
if options.get('other_account'):account['account']['email']='other@example.invalid'
for line in sys.stdin:
    req=json.loads(line);record(req)
    method=req.get('method')
    if method=='initialize':result(req,{{}})
    elif method=='initialized':continue
    elif method=='config/read':result(req,{{'config':config}})
    elif method=='account/read':result(req,account)
    elif method=='account/rateLimits/read':result(req,{{'rateLimits':{{'limitId':'codex','primary':{{'usedPercent':25}}}}}})
    elif method in ('thread/start','thread/resume','thread/read'):
        result(req,{{'thread':{{'id':'test-thread','turns':history if method!='thread/start' else [],
            'status':{{'type':'active' if options.get('active') else 'idle'}}}}}})
    elif method=='turn/start':
        if options.get('delay_id'):time.sleep(.15)
        event('turn/started',{{'threadId':'test-thread','turn':{{'id':'turn-1','status':'inProgress'}}}})
        if not options.get('early'):result(req,{{'turn':{{'id':'turn-1','status':'inProgress'}}}})
        if options.get('approvals'):
            for i,m in enumerate(('item/commandExecution/requestApproval','item/fileChange/requestApproval',
                'item/permissions/requestApproval','item/tool/requestUserInput','item/tool/call',
                'mcpServer/elicitation/request','unknown/privilegedAction')):
                send({{'id':500+i,'method':m,'params':{{}}}})
                reply=json.loads(sys.stdin.readline());record(reply)
        event('item/agentMessage/delta',{{'threadId':'unrelated','turnId':'turn-1','itemId':'bad','delta':'PRIVATE WRONG CHAT'}})
        event('item/agentMessage/delta',{{'threadId':'test-thread','turnId':'turn-1','itemId':'answer','delta':'Hel'}})
        if options.get('disconnect'):sys.exit(0)
        if options.get('hang') or options.get('delay_id'):continue
        event('item/agentMessage/delta',{{'threadId':'test-thread','turnId':'turn-1','itemId':'answer','delta':'lo'}})
        event('item/completed',{{'threadId':'test-thread','turnId':'turn-1','item':{{'id':'answer','type':'agentMessage','text':'Hello!'}}}})
        event('turn/completed',{{'threadId':'test-thread','turn':{{'id':'turn-1','status':'failed' if options.get('failed') else 'completed',
            'error':{{'message':'SECRET account token'}}}}}})
        if options.get('early'):result(req,{{'turn':{{'id':'turn-1','status':'inProgress'}}}})
    elif method=='turn/interrupt':
        result(req,{{}})
        event('turn/completed',{{'threadId':'test-thread','turn':{{'id':'turn-1','status':'interrupted'}}}})
    else:raise RuntimeError('unexpected method')
''')
    script.chmod(0o700)
    return str(script), log


class ChatClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = ThreadStore(self.folder / "state")

    def tearDown(self):
        self.temp.cleanup()

    def client(self, **options):
        binary, log = fake_codex(self.folder, **options)
        return ChatClient(self.store, binary, request_timeout=1, turn_timeout=1), log

    def methods(self, log):
        return [row.get("method") for row in map(json.loads, log.read_text().splitlines()) if "method" in row]

    def test_streaming_final_item_is_authoritative_and_state_is_private(self):
        client, log = self.client()
        updates = []
        result = client.send("Hello", None, updates.append)
        self.assertEqual(result["messages"], [{"role": "user", "text": "Hello"}, {"role": "assistant", "text": "Hello!"}])
        self.assertGreaterEqual(len(updates), 2)
        self.assertNotIn("PRIVATE", json.dumps(updates))
        self.assertEqual(self.methods(log).count("turn/start"), 1)
        self.assertEqual(self.store.load(), {"thread_id": "test-thread", "account_scope": SCOPE})
        self.assertNotIn("Hello", self.store.path.read_text())
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.workspace.stat().st_mode), 0o700)
        starts = [r for r in map(json.loads, log.read_text().splitlines()) if r.get("method") == "thread/start"]
        self.assertEqual(starts[0]["params"]["sandbox"], "read-only")
        self.assertEqual(starts[0]["params"]["cwd"], str(self.store.workspace))

    def test_events_before_turn_start_response_are_retained(self):
        client, _ = self.client(early=True)
        result = client.send("Hello", None, lambda _: None)
        self.assertEqual(result["messages"][-1]["text"], "Hello!")

    def test_restore_reads_history_without_model_request(self):
        self.store.save("test-thread", SCOPE)
        client, log = self.client()
        restored = client.restore(self.store.load())
        self.assertEqual(restored["messages"], [{"role": "user", "text": "Earlier question"}, {"role": "assistant", "text": "Earlier answer"}])
        self.assertNotIn("turn/start", self.methods(log))
        self.assertNotIn("thread/resume", self.methods(log))

    def test_subsequent_message_resumes_only_saved_thread(self):
        self.store.save("test-thread", SCOPE)
        client, log = self.client()
        result = client.send("Follow up", self.store.load(), lambda _: None)
        self.assertEqual(result["messages"][0]["text"], "Earlier question")
        self.assertIn("thread/resume", self.methods(log))
        self.assertNotIn("thread/start", self.methods(log))

    def test_account_mismatch_never_reads_or_sends_old_chat(self):
        self.store.save("test-thread", SCOPE)
        for method in ("restore", "send"):
            client, log = self.client(other_account=True)
            with self.assertRaisesRegex(ChatError, "sign-in changed"):
                if method == "restore":client.restore(self.store.load())
                else:client.send("Hello", self.store.load(), lambda _: None)
            self.assertFalse(any(m.startswith("thread/") or m == "turn/start" for m in self.methods(log)))

    def test_configured_mcp_server_is_explicitly_disabled_before_any_thread(self):
        client, log = self.client(mcp=True)
        client.send("Hello", None, lambda _: None)
        rows = list(map(json.loads, log.read_text().splitlines()))
        launches = [r for r in rows if "pid" in r]
        self.assertEqual(len(launches), 2)
        self.assertIn("mcp_servers.test.enabled=false", launches[1]["argv"])
        self.assertEqual(self.methods(log).count("config/read"), 2)
        self.assertEqual(self.methods(log).count("turn/start"), 1)

    def test_unverified_settings_fail_closed_before_thread_creation(self):
        client, log = self.client(unsafe=True)
        with self.assertRaisesRegex(ChatError, "restricted settings"):
            client.send("Hello", None, lambda _: None)
        self.assertNotIn("thread/start", self.methods(log))
        self.assertNotIn("turn/start", self.methods(log))

    def test_server_requests_are_denied_without_executing_tools(self):
        client, log = self.client(approvals=True)
        client.send("Hello", None, lambda _: None)
        replies = [r for r in map(json.loads, log.read_text().splitlines()) if isinstance(r.get("id"), int) and r["id"] >= 500]
        self.assertEqual(replies[1]["result"]["decision"], "cancel")
        self.assertEqual(replies[2]["result"]["permissions"], {})
        self.assertFalse(replies[4]["result"]["success"])
        self.assertEqual(replies[5]["result"]["action"], "decline")
        self.assertEqual(replies[6]["error"]["code"], -32601)

    def test_stop_before_turn_id_arrives_still_sends_exactly_one_interrupt(self):
        client, log = self.client(delay_id=True)
        cancel = threading.Event()
        caught = []
        def run():
            try:client.send("Hello", None, lambda _: None, cancel)
            except Exception as error:caught.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 1
        while (not log.exists() or '"method": "turn/start"' not in log.read_text()) and time.monotonic() < deadline:
            time.sleep(.005)
        cancel.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(caught[0], ChatStopped)
        self.assertEqual(self.methods(log).count("turn/interrupt"), 1)
        for row in map(json.loads, log.read_text().splitlines()):
            if "pid" in row:
                with self.assertRaises(ProcessLookupError):os.kill(row["pid"], 0)

    def test_cancel_before_connect_never_starts_server(self):
        client, log = self.client()
        cancel = threading.Event();cancel.set()
        with self.assertRaises(ChatStopped):client.send("Hello", None, lambda _: None, cancel)
        self.assertFalse(log.exists())

    def test_disconnect_does_not_retry_and_never_exposes_raw_errors(self):
        for options in ({"disconnect": True}, {"failed": True}):
            client, log = self.client(**options)
            log.unlink(missing_ok=True)
            with self.assertRaises(ChatError) as caught:
                client.send("Hello", None, lambda _: None)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(self.methods(log).count("turn/start"), 1)

    def test_active_thread_is_not_steered_by_accident(self):
        self.store.save("test-thread", SCOPE)
        client, log = self.client(active=True)
        with self.assertRaisesRegex(ChatError, "already working"):
            client.send("Hello", self.store.load(), lambda _: None)
        self.assertNotIn("turn/start", self.methods(log))

    def test_input_and_history_are_bounded(self):
        client, log = self.client()
        for text in ("", " ", "x" * (MAX_INPUT + 1)):
            with self.assertRaises(ChatError):client.send(text, None, lambda _: None)
        self.assertFalse(log.exists())
        result = bounded_messages([{"role": "assistant", "text": "x" * 20000}] * 100)
        self.assertLessEqual(sum(len(r["text"]) for r in result), 48000)
        self.assertLessEqual(len(result), 30)

    def test_corrupt_or_symlink_state_never_chooses_another_thread(self):
        self.store.path.write_text('{"thread_id":"../wrong"}')
        with self.assertRaises(ChatError):self.store.load()
        self.store.clear()
        external = self.folder / "external";external.write_text("private")
        self.store.path.symlink_to(external)
        with self.assertRaises(ChatError):self.store.load()
        self.assertEqual(external.read_text(), "private")

    def test_legacy_approval_denials_match_protocol(self):
        for method in ("applyPatchApproval", "execCommandApproval"):
            self.assertEqual(reject_server_request({"id": 5, "method": method}), {"id": 5, "result": {"decision": "abort"}})


if __name__ == "__main__":
    unittest.main()
