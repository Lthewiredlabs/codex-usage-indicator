import json
import os
from pathlib import Path
import tempfile
import unittest

from usage_reader import UsageError, normalize, read_usage


class ReaderTests(unittest.TestCase):
    def test_multi_bucket_takes_precedence_and_strips_private_fields(self):
        result = normalize({"rateLimits": {"limitId": "legacy"}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 87, "windowDurationMins": 300, "resetsAt": 123},
                      "credits": {"balance": "123"}, "accountId": "private"}}}, 42)
        self.assertEqual(result["buckets"][0]["id"], "codex")
        self.assertEqual(result["buckets"][0]["primary"]["usedPercent"], 87)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("credits", json.dumps(result))

    def test_fallback_and_unknown_values(self):
        result = normalize({"rateLimitsByLimitId": {}, "rateLimits": {
            "primary": {"usedPercent": None, "windowDurationMins": True, "resetsAt": float("inf")}}})
        self.assertEqual(result["buckets"][0]["primary"],
                         {"usedPercent": None, "windowDurationMins": None, "resetsAt": None})
        self.assertIsNone(result["buckets"][0]["secondary"])

    def test_missing_data_and_binary(self):
        with self.assertRaises(UsageError):
            normalize({})
        self.assertEqual(read_usage("/nonexistent/codex")["status"], "error")

    def fake_codex(self, folder, body):
        script = Path(folder) / "codex"
        script.write_text("#!/usr/bin/python3\n" + body)
        script.chmod(0o700)
        return str(script)

    def test_handshake_and_only_read_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = self.fake_codex(folder, '''import sys, json
first=json.loads(sys.stdin.readline())
assert first['method']=='initialize'
print(json.dumps({'id':first['id'],'result':{}}),flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
request=json.loads(sys.stdin.readline())
assert request['method']=='account/rateLimits/read'
print(json.dumps({'method':'unrelated/notification','params':{}}),flush=True)
print(json.dumps({'id':request['id'],'result':{'rateLimits':{'limitId':'codex','primary':{'usedPercent':87}}}}),flush=True)
sys.stdin.read()
''')
            result = read_usage(binary, 2)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["buckets"][0]["primary"]["usedPercent"], 87)

    def test_timeout_reaps_child(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            binary = self.fake_codex(folder, f'''import os, time
open({str(pidfile)!r},'w').write(str(os.getpid()))
time.sleep(20)
''')
            self.assertEqual(read_usage(binary, .1)["status"], "error")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_raw_errors_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = self.fake_codex(folder, '''import sys,json
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'],'error':{'message':'SECRET account@example.com'}}),flush=True)
''')
            output = json.dumps(read_usage(binary, 1))
            self.assertNotIn("SECRET", output)
            self.assertNotIn("account@example.com", output)


if __name__ == "__main__":
    unittest.main()
