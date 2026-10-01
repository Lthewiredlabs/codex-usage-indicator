#!/usr/bin/env python3
"""Read Codex usage, or explicitly redeem one reset, through its app-server protocol."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
import uuid


RESET_OUTCOMES = frozenset(("reset", "alreadyRedeemed", "noCredit", "nothingToReset"))


class UsageError(Exception):
    pass


def find_codex(explicit=None):
    candidates = [explicit] if explicit else [
        shutil.which("codex"), "/usr/lib/chatgpt/resources/codex",
        "/usr/lib/codex/resources/codex", str(Path.home() / ".local/bin/codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise UsageError("Codex was not found. Install or open the Codex app first.")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_window(window):
    if not isinstance(window, dict):
        return None
    return {key: window[key] if number(window.get(key)) else None
            for key in ("usedPercent", "windowDurationMins", "resetsAt")}


def normalize_reset_credits(summary):
    """Keep reset metadata separate from paid credits; never infer the count from rows."""
    summary = summary if isinstance(summary, dict) else {}
    count = summary.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = None
    credits = []
    rows = summary.get("credits")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        expires = row.get("expiresAt")
        credits.append({
            "id": row["id"],
            "title": row["title"][:120] if isinstance(row.get("title"), str) else None,
            "description": row["description"][:500] if isinstance(row.get("description"), str) else None,
            "expiresAt": expires if isinstance(expires, int) and not isinstance(expires, bool) else None,
            "resetType": row.get("resetType") if row.get("resetType") in ("codexRateLimits", "unknown") else "unknown",
            "status": row.get("status") if row.get("status") in ("available", "redeeming", "redeemed", "unknown") else "unknown",
        })
    return {"availableCount": count, "credits": credits}


def normalize(result, fetched_at=None):
    if not isinstance(result, dict):
        raise UsageError("Codex returned an unsupported usage response.")
    raw = result.get("rateLimitsByLimitId")
    if not isinstance(raw, dict) or not raw:
        legacy = result.get("rateLimits")
        raw = {legacy.get("limitId") or "codex": legacy} if isinstance(legacy, dict) else {}
    buckets = []
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        identity = item.get("limitId") or key
        name = item.get("limitName") or ("Codex" if identity == "codex" else identity)
        # Paid-credit balances and account records never cross into the UI.
        buckets.append({
            "id": str(identity), "name": str(name)[:80],
            "primary": normalize_window(item.get("primary")),
            "secondary": normalize_window(item.get("secondary")),
        })
    if not buckets:
        raise UsageError("No usage windows available. Check your Codex sign-in.")
    return {"status": "ok", "fetchedAt": time.time() if fetched_at is None else fetched_at,
            "buckets": buckets, "resetCredits": normalize_reset_credits(result.get("rateLimitResetCredits")),
            "accountScope": None}


def account_scope(result):
    """Pseudonymous sign-in binding; the protocol does not expose a workspace identity."""
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return None
    email = account.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return hashlib.sha256(("chatgpt:" + email.strip().casefold()).encode()).hexdigest()


class Protocol:
    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.buffer = b""
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, request_id, method, params=None):
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.response(request_id)

    def response(self, request_id):
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("id") != request_id or "method" in message:
                    continue
                if "error" in message:
                    # Never expose the server's raw error, which may contain account details.
                    raise UsageError("Usage request failed. Check Codex sign-in and connection.")
                return message.get("result")
            wait = self.deadline - time.monotonic()
            if wait <= 0 or not self.selector.select(wait):
                raise UsageError("Usage request timed out. Check Codex and retry.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError("Codex closed the usage connection. Open Codex and retry.")
            self.buffer += chunk
            if len(self.buffer) > 1048576:
                raise UsageError("Codex returned an unsupported usage response.")


@contextmanager
def connection(codex, timeout):
    process = None
    protocol = None
    try:
        binary = find_codex(codex)
        # Inherit the helper's process group so its caller can cancel the whole tree.
        process = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(Path.home()),
        )
        protocol = Protocol(process, time.monotonic() + timeout)
        protocol.request(1, "initialize", {
            "clientInfo": {"name": "codex_usage_indicator", "title": "Codex Usage", "version": "0.2.0"},
            "capabilities": {"experimentalApi": False},
        })
        protocol.send({"method": "initialized", "params": {}})
        yield protocol
    finally:
        if protocol:
            protocol.selector.close()
        if process:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass


def read_usage(codex=None, timeout=18):
    try:
        with connection(codex, timeout) as protocol:
            usage = normalize(protocol.request(2, "account/rateLimits/read"))
            try:
                usage["accountScope"] = account_scope(protocol.request(3, "account/read", {"refreshToken": False}))
            except (UsageError, OSError, ValueError):
                # Older servers or unavailable identity must not hide valid usage.
                pass
            return usage
    except UsageError as exc:
        error = str(exc)
    except (OSError, ValueError):
        error = "Could not read usage. Check Codex sign-in and connection."
    return {"status": "error", "fetchedAt": time.time(), "buckets": [], "error": error,
            "resetCredits": {"availableCount": None, "credits": []}, "accountScope": None}


def validate_reset_attempt(idempotency_key, expected_account_scope, credit_id):
    try:
        valid_key = (isinstance(idempotency_key, str) and len(idempotency_key) == 36
                     and str(uuid.UUID(idempotency_key)) == idempotency_key.lower())
    except (ValueError, AttributeError):
        valid_key = False
    if not valid_key:
        raise UsageError("A valid saved UUID is required for a reset attempt.")
    if not isinstance(expected_account_scope, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_account_scope):
        raise UsageError("Refresh usage to verify the signed-in account before resetting.")
    if credit_id is not None and (not isinstance(credit_id, str) or not credit_id):
        raise UsageError("The selected reset credit is invalid. Refresh usage and retry.")


def consume_reset(codex=None, timeout=18, *, idempotency_key=None, credit_id=None, expected_account_scope=None):
    """Send one user-confirmed attempt. Callers must persist/reuse the supplied UUID."""
    sent = False
    answer = None
    try:
        validate_reset_attempt(idempotency_key, expected_account_scope, credit_id)
        with connection(codex, timeout) as protocol:
            scope = account_scope(protocol.request(2, "account/read", {"refreshToken": False}))
            if scope is None or scope != expected_account_scope:
                raise UsageError("The signed-in account could not be verified or has changed. No reset was requested.")
            params = {"idempotencyKey": idempotency_key}
            if credit_id is not None:
                params["creditId"] = credit_id
            # A partial pipe write may reach the server, so mark before attempting send.
            sent = True
            result = protocol.request(3, "account/rateLimitResetCredit/consume", params)
            outcome = result.get("outcome") if isinstance(result, dict) else None
            if not isinstance(outcome, str) or outcome not in RESET_OUTCOMES:
                raise UsageError("Codex returned an unsupported reset response.")
            answer = {"status": "ok", "outcome": outcome, "uncertain": False, "usage": None}
            try:
                usage = normalize(protocol.request(4, "account/rateLimits/read"))
                usage["accountScope"] = scope
                answer["usage"] = usage
            except Exception:
                # A known outcome stays known even when refreshing usage fails.
                answer["error"] = "The reset result was received, but usage could not be refreshed. Refresh usage to check current limits."
            return answer
    except UsageError as exc:
        error = str(exc)
    except Exception:
        # Keep unexpected transport/protocol failures sanitized and conservative.
        error = "Could not complete the reset request. Check Codex sign-in and connection."
    # Even a late helper-cleanup failure cannot undo a verified server outcome.
    if answer is not None:
        return answer
    if sent:
        error = "The reset outcome could not be confirmed. Retry only this same saved attempt."
    return {"status": "error", "outcome": None, "uncertain": sent, "usage": None, "error": error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Codex executable path")
    parser.add_argument("--timeout", type=float, default=18, help="Request timeout in seconds (1–20)")
    parser.add_argument("--reset", action="store_true", help="Explicitly redeem one existing reset credit")
    parser.add_argument("--idempotency-key", help="Saved UUID identifying this single reset attempt")
    parser.add_argument("--credit-id", help="Optional existing reset credit to redeem")
    parser.add_argument("--account-scope", help="Saved sign-in scope from a successful usage refresh")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 20:
        parser.error("--timeout must be between 1 and 20 seconds")
    if not args.reset and any(value is not None for value in (args.idempotency_key, args.credit_id, args.account_scope)):
        parser.error("reset arguments require --reset")
    if args.reset:
        result = consume_reset(args.codex, args.timeout, idempotency_key=args.idempotency_key,
                               credit_id=args.credit_id, expected_account_scope=args.account_scope)
    else:
        result = read_usage(args.codex, args.timeout)
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
