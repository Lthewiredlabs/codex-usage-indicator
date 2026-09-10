#!/usr/bin/env python3
"""Read Codex usage through its app-server protocol, without creating a turn."""

import argparse
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


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
        # Only quota fields cross into the UI; no account identifiers or credit records.
        buckets.append({
            "id": str(identity), "name": str(name)[:80],
            "primary": normalize_window(item.get("primary")),
            "secondary": normalize_window(item.get("secondary")),
        })
    if not buckets:
        raise UsageError("No usage windows available. Check your Codex sign-in.")
    return {"status": "ok", "fetchedAt": time.time() if fetched_at is None else fetched_at,
            "buckets": buckets}


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
                raise UsageError("Usage refresh timed out. It will retry in one minute.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError("Codex closed the usage connection. Open Codex and retry.")
            self.buffer += chunk
            if len(self.buffer) > 1048576:
                raise UsageError("Codex returned an unsupported usage response.")


def read_usage(codex=None, timeout=18):
    process = None
    protocol = None
    try:
        binary = find_codex(codex)
        process = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(Path.home()),
        )
        protocol = Protocol(process, time.monotonic() + timeout)
        protocol.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_usage_indicator", "title": "Codex Usage", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False},
        }})
        protocol.response(1)
        protocol.send({"method": "initialized", "params": {}})
        protocol.send({"id": 2, "method": "account/rateLimits/read"})
        return normalize(protocol.response(2))
    except UsageError as exc:
        return {"status": "error", "fetchedAt": time.time(), "buckets": [], "error": str(exc)}
    except (OSError, ValueError):
        return {"status": "error", "fetchedAt": time.time(), "buckets": [],
                "error": "Could not read usage. Check Codex sign-in and connection."}
    finally:
        if protocol:
            protocol.selector.close()
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Codex executable path")
    parser.add_argument("--timeout", type=float, default=18, help="Read timeout in seconds (1–20)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 20:
        parser.error("--timeout must be between 1 and 20 seconds")
    result = read_usage(args.codex, args.timeout)
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
