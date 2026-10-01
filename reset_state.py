"""Persist one pending reset attempt so interrupted requests reuse their key."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid


class ResetStateError(Exception):
    pass


def _valid_scope(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate(attempt):
    if not isinstance(attempt, dict) or set(attempt) not in (
            {"idempotencyKey", "accountScope"},
            {"idempotencyKey", "accountScope", "creditId"}):
        raise ResetStateError("The saved reset attempt is invalid. Reset remains blocked.")
    key = attempt["idempotencyKey"]
    try:
        valid_key = isinstance(key, str) and str(uuid.UUID(key)) == key
    except (ValueError, AttributeError):
        valid_key = False
    credit = attempt.get("creditId")
    if (not valid_key or not _valid_scope(attempt["accountScope"])
            or ("creditId" in attempt and (
                not isinstance(credit, str) or not credit.strip()
                or len(credit) > 256 or any(ord(char) < 32 for char in credit)))):
        raise ResetStateError("The saved reset attempt is invalid. Reset remains blocked.")
    return attempt


class ResetJournal:
    def __init__(self, path: Path | None = None):
        if path is None:
            configured = os.environ.get("XDG_STATE_HOME", "")
            base = (Path(configured) if configured and Path(configured).is_absolute()
                    else Path.home() / ".local/state")
            path = base / "codex-usage-indicator/reset-attempt.json"
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        """Serialize local processes as well as saving atomically on disk."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            lock_path = self.path.with_name(self.path.name + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
        except OSError as error:
            raise ResetStateError("Unable to safely save the reset attempt. Reset remains blocked.") from error

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ResetStateError("The saved reset attempt is invalid. Reset remains blocked.")
            os.fchmod(source.fileno(), 0o600)
            try:
                attempt = json.load(source)
            except (ValueError, UnicodeError) as error:
                raise ResetStateError("The saved reset attempt is unreadable. Reset remains blocked.") from error
        return _validate(attempt)

    def load(self):
        with self._locked():
            return self._load()

    def _sync_directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def begin(self, account_scope: str, credit_id: str | None = None):
        """Durably record a key before the caller starts the reset request."""
        if not _valid_scope(account_scope):
            raise ResetStateError("Codex account identity is unavailable. Reset remains blocked.")
        with self._locked():
            existing = self._load()
            if existing is not None:
                if existing["accountScope"] != account_scope:
                    raise ResetStateError("A reset is pending for a different Codex account.")
                # A previous process may have stopped after rename but before syncing.
                self._sync_directory()
                return existing
            attempt = {"idempotencyKey": str(uuid.uuid4()), "accountScope": account_scope}
            if credit_id is not None:
                attempt["creditId"] = credit_id
            _validate(attempt)
            fd, name = tempfile.mkstemp(prefix=".reset-attempt-", dir=self.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as destination:
                    os.fchmod(destination.fileno(), 0o600)
                    json.dump(attempt, destination, sort_keys=True)
                    destination.write("\n")
                    destination.flush()
                    os.fsync(destination.fileno())
                os.replace(temporary, self.path)
                self._sync_directory()
            finally:
                temporary.unlink(missing_ok=True)
            return attempt

    def finish(self, expected_key: str):
        """Clear pending state only after the caller has a definitive result."""
        with self._locked():
            current = self._load()
            if current is not None:
                if current["idempotencyKey"] != expected_key:
                    raise ResetStateError("A different reset attempt is pending. Its saved state was preserved.")
                self.path.unlink()
                self._sync_directory()
