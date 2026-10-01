from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from usage_reader import UsageError, account_scope, connection, consume_reset, normalize, read_usage


KEY = "29e04b66-8c13-4b7b-980c-80d2c9684cda"
ACCOUNT = {"account": {"type": "chatgpt", "email": "test@example.invalid", "planType": "plus"}}
SCOPE = account_scope(ACCOUNT)
USAGE = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 87}}}


class ReaderTests(unittest.TestCase):
    def test_multi_bucket_takes_precedence_and_strips_private_fields(self):
        result = normalize({"rateLimits": {"limitId": "legacy"}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 87, "windowDurationMins": 300, "resetsAt": 123},
                      "credits": {"balance": "123"}, "accountId": "private"}}}, 42)
        self.assertEqual(result["buckets"][0]["id"], "codex")
        self.assertEqual(result["buckets"][0]["primary"]["usedPercent"], 87)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("credits", result["buckets"][0])
        self.assertNotIn("balance", json.dumps(result))
        self.assertEqual(result["resetCredits"], {"availableCount": None, "credits": []})

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

    def test_reset_count_is_authoritative_and_only_allowed_metadata_survives(self):
        result = normalize({**USAGE, "rateLimitResetCredits": {"availableCount": 7, "credits": [
            {"id": "reset-1", "title": "Earned reset", "description": "Use one reset", "expiresAt": 100,
             "resetType": "codexRateLimits", "status": "available", "grantedAt": 55,
             "accountId": "private", "billingDetails": "secret"},
        ]}})
        credits = result["resetCredits"]
        self.assertEqual(credits["availableCount"], 7)
        self.assertEqual(credits["credits"], [{"id": "reset-1", "title": "Earned reset", "description": "Use one reset",
                                              "expiresAt": 100, "resetType": "codexRateLimits", "status": "available"}])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        for rows in (None, []):
            summary = normalize({**USAGE, "rateLimitResetCredits": {"availableCount": 7, "credits": rows}})
            self.assertEqual(summary["resetCredits"], {"availableCount": 7, "credits": []})

    def test_unknown_reset_count_is_not_zero_or_derived_from_details(self):
        for count in (None, -1, True, 1.0, "2"):
            result = normalize({**USAGE, "rateLimitResetCredits": {"availableCount": count,
                                "credits": [{"id": "reset-1", "status": "available"}]}})
            self.assertIsNone(result["resetCredits"]["availableCount"])
        result = normalize({**USAGE, "rateLimitResetCredits": {"availableCount": 0}})
        self.assertEqual(result["resetCredits"]["availableCount"], 0)

    def test_malformed_reset_metadata_is_sanitized(self):
        result = normalize({**USAGE, "rateLimitResetCredits": {"availableCount": 1, "credits": [None, {},
            {"id": "good", "title": 5, "description": [], "expiresAt": True, "resetType": {}, "status": []}]}})
        self.assertEqual(result["resetCredits"]["credits"], [{"id": "good", "title": None, "description": None,
                         "expiresAt": None, "resetType": "unknown", "status": "unknown"}])

    def test_account_scope_is_a_pseudonym_and_unknown_without_chatgpt_email(self):
        self.assertEqual(len(SCOPE), 64)
        self.assertNotIn("test", SCOPE)
        self.assertEqual(account_scope({"account": {"type": "chatgpt", "email": " TEST@example.invalid "}}), SCOPE)
        for account in (None, {}, {"type": "apiKey"}, {"type": "chatgpt", "email": None}):
            self.assertIsNone(account_scope({"account": account}))

    def fake_codex(self, folder, body):
        script = Path(folder) / "codex"
        script.write_text("#!/usr/bin/python3\n" + body)
        script.chmod(0o700)
        return str(script)

    def fake_backend(self, folder, *, outcome="reset", account=ACCOUNT, usage=USAGE,
                     consume_error=False, hang_consume=False, fail_refresh=False, hang_initialize=False):
        log = Path(folder) / "requests.jsonl"
        binary = self.fake_codex(folder, f'''import sys, json, time, os
log={str(log)!r}
for line in sys.stdin:
    request=json.loads(line)
    with open(log,'a') as file:
        file.write(json.dumps(request)+'\\n')
    method=request['method']
    response={{'id':request.get('id')}}
    if method=='initialize':
        if {hang_initialize!r}: time.sleep(30)
        response['result']={{}}
    elif method=='initialized':
        continue
    elif method=='account/read':
        assert request['params']=={{'refreshToken':False}}
        response['result']={account!r}
    elif method=='account/rateLimitResetCredit/consume':
        if {hang_consume!r}: time.sleep(30)
        if {consume_error!r}: response['error']={{'message':'SECRET test@example.invalid'}}
        else: response['result']={{'outcome':{outcome!r}, 'privateData':'SECRET'}}
    elif method=='account/rateLimits/read':
        if {fail_refresh!r}: response['error']={{'message':'SECRET test@example.invalid'}}
        else: response['result']={usage!r}
    else:
        raise AssertionError(method)
    print(json.dumps(response),flush=True)
''')
        return binary, log

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
request=json.loads(sys.stdin.readline())
assert request['method']=='account/read'
print(json.dumps({'id':request['id'],'result':{'account':{'type':'chatgpt','email':'test@example.invalid'}}}),flush=True)
sys.stdin.read()
''')
            result = read_usage(binary, 2)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["buckets"][0]["primary"]["usedPercent"], 87)
            self.assertEqual(result["accountScope"], SCOPE)
            self.assertNotIn("test@example.invalid", json.dumps(result))

    def test_account_read_failure_preserves_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = self.fake_codex(folder, f'''import sys,json
for line in sys.stdin:
    request=json.loads(line)
    method=request['method']
    if method=='initialized':continue
    if method=='account/read':break
    result={USAGE!r} if method=='account/rateLimits/read' else {{}}
    print(json.dumps({{'id':request['id'],'result':result}}),flush=True)
''')
            result = read_usage(binary, 1)
            self.assertEqual(result["status"], "ok")
            self.assertIsNone(result["accountScope"])

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

    def test_each_consume_outcome_uses_exact_key_once_and_fetches_actual_usage(self):
        for outcome in ("reset", "alreadyRedeemed", "noCredit", "nothingToReset"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                usage = {**USAGE, "rateLimitResetCredits": {"availableCount": 2, "credits": None}}
                binary, log = self.fake_backend(folder, outcome=outcome, usage=usage)
                result = consume_reset(binary, 2, idempotency_key=KEY, credit_id="chosen-credit", expected_account_scope=SCOPE)
                self.assertEqual((result["status"], result["outcome"], result["uncertain"]), ("ok", outcome, False))
                self.assertEqual(result["usage"]["buckets"][0]["primary"]["usedPercent"], 87)
                self.assertEqual(result["usage"]["resetCredits"]["availableCount"], 2)
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual([row["method"] for row in requests], ["initialize", "initialized", "account/read",
                                 "account/rateLimitResetCredit/consume", "account/rateLimits/read"])
                self.assertEqual(requests[3]["params"], {"idempotencyKey": KEY, "creditId": "chosen-credit"})
                self.assertNotIn("SECRET", json.dumps(result))

    def test_backend_can_select_credit_when_details_are_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            binary, log = self.fake_backend(folder)
            result = consume_reset(binary, 2, idempotency_key=KEY, expected_account_scope=SCOPE)
            self.assertEqual(result["outcome"], "reset")
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(requests[3]["params"], {"idempotencyKey": KEY})

    def test_known_success_survives_failed_usage_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            binary, _ = self.fake_backend(folder, fail_refresh=True)
            result = consume_reset(binary, 2, idempotency_key=KEY, expected_account_scope=SCOPE)
            self.assertEqual((result["status"], result["outcome"], result["uncertain"]), ("ok", "reset", False))
            self.assertIsNone(result["usage"])
            self.assertIn("could not be refreshed", result["error"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_known_success_survives_unexpected_refresh_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            binary, _ = self.fake_backend(folder)
            with patch("usage_reader.normalize", side_effect=RuntimeError("SECRET")):
                result = consume_reset(binary, 2, idempotency_key=KEY, expected_account_scope=SCOPE)
            self.assertEqual((result["status"], result["outcome"], result["uncertain"]), ("ok", "reset", False))
            self.assertIsNone(result["usage"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_known_success_survives_late_helper_cleanup_failure(self):
        @contextmanager
        def failed_cleanup(codex, timeout):
            with connection(codex, timeout) as protocol:
                yield protocol
            raise RuntimeError("SECRET cleanup failure")

        with tempfile.TemporaryDirectory() as folder:
            binary, _ = self.fake_backend(folder)
            with patch("usage_reader.connection", side_effect=failed_cleanup):
                result = consume_reset(binary, 2, idempotency_key=KEY, expected_account_scope=SCOPE)
            self.assertEqual((result["status"], result["outcome"], result["uncertain"]), ("ok", "reset", False))
            self.assertEqual(result["usage"]["status"], "ok")
            self.assertNotIn("SECRET", json.dumps(result))

    def test_account_change_or_unknown_identity_prevents_consume(self):
        for account in ({"account": {"type": "chatgpt", "email": "different@example.invalid"}}, {"account": None}):
            with self.subTest(account=account), tempfile.TemporaryDirectory() as folder:
                binary, log = self.fake_backend(folder, account=account)
                result = consume_reset(binary, 2, idempotency_key=KEY, expected_account_scope=SCOPE)
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["uncertain"])
                self.assertNotIn("account/rateLimitResetCredit/consume", log.read_text())

    def test_invalid_attempt_never_starts_codex(self):
        attempts = ({"idempotency_key": None}, {"idempotency_key": "not-a-uuid"},
                    {"expected_account_scope": None}, {"expected_account_scope": "bad"}, {"credit_id": ""})
        for override in attempts:
            with self.subTest(override=override), patch("usage_reader.find_codex") as finder:
                kwargs = {"idempotency_key": KEY, "expected_account_scope": SCOPE, **override}
                result = consume_reset(**kwargs)
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["uncertain"])
                finder.assert_not_called()

    def test_pre_send_timeout_is_known_not_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            binary, log = self.fake_backend(folder, hang_initialize=True)
            result = consume_reset(binary, .1, idempotency_key=KEY, expected_account_scope=SCOPE)
            self.assertFalse(result["uncertain"])
            self.assertNotIn("account/rateLimitResetCredit/consume", log.read_text())

    def test_post_send_failures_are_uncertain_and_never_retried(self):
        failures = ({"consume_error": True}, {"hang_consume": True}, {"outcome": "unexpected"}, {"outcome": []})
        for options in failures:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                binary, log = self.fake_backend(folder, **options)
                result = consume_reset(binary, .2, idempotency_key=KEY, expected_account_scope=SCOPE)
                self.assertEqual(result["status"], "error")
                self.assertTrue(result["uncertain"])
                self.assertIsNone(result["outcome"])
                self.assertEqual(log.read_text().count('"method": "account/rateLimitResetCredit/consume"'), 1)
                self.assertNotIn("SECRET", json.dumps(result))
                self.assertNotIn("test@example.invalid", json.dumps(result))

    def test_cli_default_is_read_only_and_reset_needs_explicit_saved_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            binary, log = self.fake_backend(folder)
            reader = str(Path(__file__).resolve().parents[1] / "usage_reader.py")
            read = subprocess.run([sys.executable, reader, "--codex", binary], capture_output=True, text=True)
            self.assertEqual(read.returncode, 0)
            self.assertNotIn("account/rateLimitResetCredit/consume", log.read_text())
            invalid = subprocess.run([sys.executable, reader, "--codex", binary, "--reset"], capture_output=True, text=True)
            self.assertEqual(invalid.returncode, 1)
            self.assertFalse(json.loads(invalid.stdout)["uncertain"])
            reset = subprocess.run([sys.executable, reader, "--codex", binary, "--reset", "--idempotency-key", KEY,
                                    "--account-scope", SCOPE], capture_output=True, text=True)
            self.assertEqual(reset.returncode, 0)
            self.assertEqual(json.loads(reset.stdout)["outcome"], "reset")
            self.assertEqual(log.read_text().count('"method": "account/rateLimitResetCredit/consume"'), 1)


if __name__ == "__main__":
    unittest.main()
