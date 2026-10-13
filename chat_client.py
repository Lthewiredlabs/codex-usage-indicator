"""Bounded, text-only Codex app-server client for the GNOME quick chat.

No API keys, prompt logs, automatic message retries, or reset-credit calls.
Codex itself owns conversation history; our private file holds only its ID and a
pseudonymous sign-in binding. The binding distinguishes emails, not workspaces.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time

from usage_reader import account_scope, find_codex

MAX_INPUT = 4000
MAX_MESSAGE = 16000
MAX_MESSAGES = 30
MAX_TOTAL_TEXT = 48000
MAX_WIRE_BYTES = 4 * 1024 * 1024
THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ACCOUNT_SCOPE = re.compile(r"[0-9a-f]{64}")
CHAT_INSTRUCTIONS = (
    "You are Codex Quick Chat, a small conversational assistant in the GNOME clock menu. "
    "Answer briefly in plain text unless the user asks for detail. This client only supports conversation. "
    "Do not execute commands, use tools, browse, edit files, install software, or take external actions. "
    "If a request needs those capabilities, explain that the user should open the full Codex app. "
    "Do not claim to have performed actions or checked live information."
)
# These are process-local overrides, never writes to the user's Codex config.
# Supported keys are documented in OpenAI's configuration reference; feature
# names are checked against the installed CLI when releasing this client.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
    "image_generation", "view_image", "multi_agent", "multi_agent_v2", "hooks",
    "memories", "code_mode", "code_mode_host", "code_mode_only", "skill_search",
    "skill_mcp_dependency_install", "goals", "sleep_tool", "workspace_dependencies",
)


class ChatError(Exception):
    """A message written by this client and safe to show to the user."""


class ChatStopped(Exception):
    pass


def bounded_messages(messages):
    result = []
    for row in messages[-MAX_MESSAGES:]:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        text = row.get("text")
        if isinstance(text, str) and text:
            result.append({"role": row["role"], "text": text[:MAX_MESSAGE]})
    while len(result) > 1 and sum(len(row["text"]) for row in result) > MAX_TOTAL_TEXT:
        result.pop(0)
    return result


def history_messages(thread):
    """Extract text only; omit tool output, paths, metadata, and reasoning."""
    result = []
    turns = thread.get("turns", []) if isinstance(thread, dict) else []
    for turn in turns if isinstance(turns, list) else []:
        items = turn.get("items", []) if isinstance(turn, dict) else []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                result.append({"role": "assistant", "text": item["text"]})
            elif item.get("type") == "userMessage":
                content = item.get("content", [])
                text = "\n".join(part["text"] for part in content if isinstance(part, dict)
                                 and part.get("type") == "text" and isinstance(part.get("text"), str)) if isinstance(content, list) else ""
                result.append({"role": "user", "text": text})
    return bounded_messages(result)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ChatError("Quick Chat's private folder is unavailable.")
    path.chmod(0o700)
    return path


class ThreadStore:
    def __init__(self, directory=None):
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.directory = private_directory(directory or base / "codex-usage-indicator/quickchat")
        self.workspace = private_directory(self.directory / "workspace")
        self.path = self.directory / "thread.json"
        self.instructions_path = self.directory / "instructions.txt"
        self._atomic(self.instructions_path, CHAT_INSTRUCTIONS)

    def _atomic(self, path, text):
        fd, temp = tempfile.mkstemp(prefix=".quickchat-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise ChatError("Saved chat could not be read. Choose New chat to start again.") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096:
                raise ValueError("invalid state")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                value = json.load(stream)
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("thread_id"), str) or not THREAD_ID.fullmatch(value["thread_id"])
                    or not isinstance(value.get("account_scope"), str) or not ACCOUNT_SCOPE.fullmatch(value["account_scope"])):
                raise ValueError("invalid state")
            return {"thread_id": value["thread_id"], "account_scope": value["account_scope"]}
        except (OSError, ValueError, UnicodeError):
            raise ChatError("Saved chat could not be read. Choose New chat to start again.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def save(self, thread_id, scope):
        if not THREAD_ID.fullmatch(thread_id or "") or not ACCOUNT_SCOPE.fullmatch(scope or ""):
            raise ChatError("Codex returned an invalid chat identity.")
        try:
            self._atomic(self.path, json.dumps({"version": 1, "thread_id": thread_id, "account_scope": scope}))
        except OSError:
            raise ChatError("The chat could not be saved. No new message was sent.") from None

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ChatError("The saved chat could not be cleared.") from None


def server_command(codex, store, disabled_servers=()):
    overrides = [
        'approval_policy="never"', 'sandbox_mode="read-only"',
        'model_provider="openai"', 'forced_login_method="chatgpt"',
        'web_search="disabled"', 'mcp_servers={}', 'notify=[]',
        'project_doc_max_bytes=0', 'project_doc_fallback_filenames=[]',
        'developer_instructions=' + json.dumps(CHAT_INSTRUCTIONS),
        'model_instructions_file=' + json.dumps(str(store.instructions_path)),
        'memories.generate_memories=false',
    ] + ['features.' + feature + '=false' for feature in DISABLED_FEATURES]
    for name in disabled_servers:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ChatError("Quick Chat cannot safely disable a configured tool. Open the full Codex app instead.")
        overrides.extend(("mcp_servers." + name + ".enabled=false",
                          "mcp_servers." + name + ".required=false"))
    command = [find_codex(codex), "app-server", "--listen", "stdio://"]
    for override in overrides:
        command.extend(("-c", override))
    return command


def reject_server_request(message):
    """Protocol-correct denials for known requests, fail closed for new methods."""
    method, request_id = message.get("method"), message.get("id")
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        result = {"decision": "cancel"}
    elif method in ("execCommandApproval", "applyPatchApproval"):
        result = {"decision": "abort"}
    elif method == "item/permissions/requestApproval":
        result = {"permissions": {}, "scope": "turn"}
    elif method == "item/tool/requestUserInput":
        result = {"answers": {}}
    elif method == "item/tool/call":
        result = {"success": False, "contentItems": [{"type": "inputText", "text": "Tools are unavailable in Quick Chat."}]}
    elif method == "mcpServer/elicitation/request":
        result = {"action": "decline"}
    else:
        return {"id": request_id, "error": {"code": -32601, "message": "This client does not support that request."}}
    return {"id": request_id, "result": result}


class ChatClient:
    """One background operation per instance; all writes happen on its worker."""
    def __init__(self, store, codex=None, request_timeout=20, turn_timeout=180):
        self.store, self.codex = store, codex
        self.request_timeout, self.turn_timeout = request_timeout, turn_timeout
        self.process = None
        self.selector = None
        self.buffer = b""
        self.sequence = 0
        self.cancel = threading.Event()
        self.thread_id = None
        self.turn_id = None
        self.interrupt_sent_at = None
        self.interrupt_sent = False
        self.turn_sent = False
        self.on_notification = lambda method, params: None

    def close(self):
        process, self.process = self.process, None
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    @contextmanager
    def connected(self, cancel=None):
        self.cancel = cancel if cancel is not None else threading.Event()
        try:
            if self.cancel.is_set():
                raise ChatStopped()
            disabled_servers = ()
            for attempt in range(2):
                self.buffer = b""
                self.process = subprocess.Popen(server_command(self.codex, self.store, disabled_servers),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    cwd=self.store.workspace, start_new_session=True)
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
                self.request("initialize", {"clientInfo": {"name": "codex_quick_chat", "title": "Codex Quick Chat", "version": "0.3.0"},
                                             "capabilities": {"experimentalApi": False}})
                self.write({"method": "initialized", "params": {}})
                effective = self.request("config/read", {"includeLayers": False})
                config = effective.get("config", {}) if isinstance(effective, dict) else {}
                features = config.get("features", {})
                if (not isinstance(features, dict) or any(features.get(name) is not False for name in DISABLED_FEATURES)
                        or config.get("approval_policy") != "never" or config.get("sandbox_mode") != "read-only"
                        or config.get("web_search") != "disabled" or config.get("project_doc_max_bytes") != 0):
                    raise ChatError("Quick Chat could not verify its restricted settings. Open the full Codex app instead.")
                servers = config.get("mcp_servers") or {}
                if not isinstance(servers, dict):
                    raise ChatError("Quick Chat could not verify that tools are disabled.")
                enabled = tuple(name for name, value in servers.items()
                                if not isinstance(value, dict) or value.get("enabled") is not False)
                if not enabled:
                    break
                if attempt:
                    raise ChatError("Quick Chat could not disable configured tools. Open the full Codex app instead.")
                disabled_servers = enabled
                self.close()
            yield self
        except (ChatError, ChatStopped):
            raise
        except Exception:
            raise ChatError("Codex disconnected. Open the chat to check before sending again." if self.turn_sent
                            else "Could not connect to Codex. Check your sign-in in the Codex app.") from None
        finally:
            self.close()

    def write(self, message):
        self.process.stdin.write((json.dumps(message, allow_nan=False) + "\n").encode())
        self.process.stdin.flush()

    def _cancel_check(self):
        if not self.cancel.is_set():
            return
        if not self.turn_sent:
            raise ChatStopped()
        if self.interrupt_sent_at is None:
            self.interrupt_sent_at = time.monotonic()
        if self.thread_id and self.turn_id and not self.interrupt_sent:
            self.sequence += 1
            self.write({"id": self.sequence, "method": "turn/interrupt", "params": {"threadId": self.thread_id, "turnId": self.turn_id}})
            self.interrupt_sent = True
        if time.monotonic() - self.interrupt_sent_at >= 3:
            raise ChatStopped()

    def read_message(self, deadline):
        while True:
            self._cancel_check()
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ChatError("Codex returned an unreadable reply. Open the full chat to continue.") from None
                if isinstance(message, dict):
                    return message
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChatError("Codex took too long to reply. Open the chat to check before sending again.")
            if not self.selector.select(min(.1, remaining)):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                if self.cancel.is_set():
                    raise ChatStopped()
                raise ChatError("Codex disconnected. Open the chat to check before sending again.")
            self.buffer += chunk
            if len(self.buffer) > MAX_WIRE_BYTES:
                raise ChatError("This chat is too large for Quick Chat. Open it in Codex or choose New chat.")

    def handle(self, message):
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            self.write(reject_server_request(message))
            return
        params = message.get("params")
        if isinstance(params, dict):
            if method == "turn/started" and params.get("threadId") == self.thread_id:
                turn = params.get("turn") or {}
                if isinstance(turn.get("id"), str):
                    self.turn_id = turn["id"]
            self.on_notification(method, params)

    def request(self, method, params=None):
        self._cancel_check()
        self.sequence += 1
        request_id = self.sequence
        if method == "turn/start":
            self.turn_sent = True
        self.write({"id": request_id, "method": method, **({"params": params} if params is not None else {})})
        deadline = time.monotonic() + self.request_timeout
        while True:
            message = self.read_message(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise ChatError("Codex could not complete the request. Check the full app, or choose New chat if this chat cannot resume.")
                return message.get("result")
            self.handle(message)

    def verified_scope(self, saved):
        scope = account_scope(self.request("account/read", {"refreshToken": False}))
        if scope is None:
            raise ChatError("Sign in to Codex with ChatGPT before using Quick Chat.")
        if saved and saved["account_scope"] != scope:
            raise ChatError("The Codex sign-in changed. Choose New chat to keep conversations separate.")
        return scope

    def restore(self, saved, cancel=None):
        if not saved:
            return {"thread_id": None, "messages": []}
        with self.connected(cancel):
            self.verified_scope(saved)
            result = self.request("thread/read", {"threadId": saved["thread_id"], "includeTurns": True})
            thread = result.get("thread") if isinstance(result, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != saved["thread_id"]:
                raise ChatError("Saved chat is unavailable. Choose New chat to start again.")
            return {"thread_id": saved["thread_id"], "messages": history_messages(thread)}

    def send(self, text, saved, on_update, cancel=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT:
            raise ChatError(f"Enter a message of 1–{MAX_INPUT} characters.")
        with self.connected(cancel):
            scope = self.verified_scope(saved)
            params = {"cwd": str(self.store.workspace), "approvalPolicy": "never", "sandbox": "read-only",
                      "baseInstructions": CHAT_INSTRUCTIONS, "developerInstructions": CHAT_INSTRUCTIONS,
                      "modelProvider": "openai"}
            if saved:
                params["threadId"] = saved["thread_id"]
            result = self.request("thread/resume" if saved else "thread/start", params)
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
                raise ChatError("Codex could not open a chat.")
            if saved and thread_id != saved["thread_id"]:
                raise ChatError("Codex returned a different chat. No message was sent.")
            if (thread.get("status") or {}).get("type") == "active":
                raise ChatError("This chat is already working in Codex. Wait for it to finish before sending.")
            self.thread_id = thread_id
            # Save before inference, so a crash cannot silently lose the submitted task.
            self.store.save(thread_id, scope)
            history = history_messages(thread)
            history.append({"role": "user", "text": text})
            assistant_items = {}
            finished = None
            last_emit = 0

            def emit(force=False):
                nonlocal last_emit
                while len(assistant_items) > MAX_MESSAGES or (len(assistant_items) > 1 and sum(map(len, assistant_items.values())) > MAX_TOTAL_TEXT):
                    assistant_items.pop(next(iter(assistant_items)))
                now = time.monotonic()
                if force or now - last_emit >= .1:
                    on_update({"thread_id": thread_id, "messages": bounded_messages(history + [
                        {"role": "assistant", "text": value} for value in assistant_items.values()])})
                    last_emit = now

            def notification(method, event):
                nonlocal finished
                if event.get("threadId") != thread_id:
                    return
                event_turn = event.get("turnId")
                if self.turn_id and event_turn and event_turn != self.turn_id:
                    return
                if method == "item/agentMessage/delta":
                    item_id, delta = event.get("itemId"), event.get("delta")
                    if isinstance(item_id, str) and isinstance(delta, str):
                        assistant_items[item_id] = (assistant_items.get(item_id, "") + delta)[:MAX_MESSAGE]
                        emit()
                elif method == "item/completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        assistant_items[str(item.get("id", "reply"))] = item["text"][:MAX_MESSAGE]
                        emit()
                elif method == "turn/completed":
                    turn = event.get("turn") or {}
                    if self.turn_id and turn.get("id") != self.turn_id:
                        return
                    finished = turn.get("status")
                    emit(True)

            self.on_notification = notification
            emit(True)
            self._cancel_check()
            response = self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}})
            turn = response.get("turn") if isinstance(response, dict) else None
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise ChatError("The message status is uncertain. Open the chat before sending it again.")
            self.turn_id = turn["id"]
            deadline = time.monotonic() + self.turn_timeout
            while finished is None:
                self.handle(self.read_message(deadline))
            emit(True)
            if finished == "interrupted" or self.cancel.is_set():
                raise ChatStopped()
            if finished != "completed":
                raise ChatError("Codex could not finish the reply. Check your usage or open the full chat.")
            return {"thread_id": thread_id, "messages": bounded_messages(history + [
                {"role": "assistant", "text": value} for value in assistant_items.values()])}
