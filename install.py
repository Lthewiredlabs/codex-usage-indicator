#!/usr/bin/env python3
"""Install Codex Usage and optional GNOME Quick Chat for the current user."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from usage_reader import UsageError, find_codex

APP_NAME = "codex-usage-indicator"
VERSION = "0.3.0"
MARKER = ".codex-usage-managed.json"
COMMON_FILES = ("usage_reader.py", "ui_model.py", "install.py", "uninstall.py", "README.md")
INDICATOR_FILES = ("indicator.py", "reset_state.py", "icons/codex-usage-symbolic.svg")
CHAT_FILES = ("chat_client.py", "chat_service.py")
FILES = COMMON_FILES + INDICATOR_FILES  # Original indicator installation scope.
ALL_FILES = FILES + CHAT_FILES
EXTENSION_UUID = "codex-quick-chat@lthewiredlabs"
EXTENSION_ICONS = tuple("icons/bit-" + state + "-symbolic.svg" for state in
                        ("idle", "blink", "thinking-left", "thinking-right", "happy", "error"))
EXTENSION_FILES = ("metadata.json", "extension.js", "stateModel.js", "stylesheet.css") + EXTENSION_ICONS
EXTENSION_MARKER = ".codex-quick-chat-managed.json"
DESKTOP_MARKER = "X-Codex-Usage-Managed=true"
DBUS_NAME = "org.codex.QuickChat"


def locations(home):
    return (home / ".local/share" / APP_NAME,
            home / ".local/share/applications" / (APP_NAME + ".desktop"),
            home / ".config/autostart" / (APP_NAME + ".desktop"))


def chat_locations(home):
    return (home / ".local/share/gnome-shell/extensions" / EXTENSION_UUID,
            home / ".local/share/dbus-1/services" / (DBUS_NAME + ".service"))


def checked_value(value):
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value:
        raise RuntimeError("Installation paths must not contain line breaks or NUL characters.")
    return value


def desktop_quote(value):
    # Desktop Exec syntax is not a shell. Percent escapes field-code expansion.
    value = checked_value(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def read_manifest(target, marker=MARKER):
    if target.is_symlink():
        raise RuntimeError(f"The install folder is a symbolic link: {target}")
    if not target.exists():
        return None
    path = target / marker
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"The install folder contains unrelated files: {target}")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("app") != APP_NAME:
        raise RuntimeError(f"The install folder contains unrelated files: {target}")
    for key in ("files", "components"):
        if key in manifest and (not isinstance(manifest[key], list) or
                                not all(isinstance(item, str) for item in manifest[key])):
            raise RuntimeError(f"The install record is invalid: {path}")
    return manifest


def installed_components(manifest):
    if manifest is None:
        return set()
    # Installations before 0.3.0 had only the indicator and no components field.
    return set(manifest.get("components", ["indicator"])) & {"indicator", "chat"}


def check_paths(root, names):
    for name in names:
        path = root / name
        for parent in (path, *path.parents):
            if parent == root.parent:
                break
            if parent.is_symlink():
                raise RuntimeError(f"A managed path is a symbolic link: {parent}")
        if path.exists() and not path.is_file():
            raise RuntimeError(f"A managed file is not a regular file: {path}")


def check_entry(path):
    if path.is_symlink() or (path.exists() and
            (not path.is_file() or DESKTOP_MARKER not in path.read_text())):
        raise RuntimeError(f"An unrelated launcher or service exists at {path}; leaving it unchanged.")


def check_owned(target, desktop, autostart, *, indicator=True, chat=False, home=None):
    manifest = read_manifest(target)
    check_paths(target, (*ALL_FILES, MARKER))
    if indicator:
        for path in (desktop, autostart):
            check_entry(path)
    if chat:
        extension, service = chat_locations(home)
        read_manifest(extension, EXTENSION_MARKER)
        check_paths(extension, (*EXTENSION_FILES, EXTENSION_MARKER))
        check_entry(service)
    return manifest


def backup_managed(home, target, desktop, autostart, *, indicator=True, chat=True):
    """Preserve only this application's known files, never state or credentials."""
    manifest = read_manifest(target)
    owned = set(manifest.get("files", FILES)) if manifest else set()
    names = tuple(name for name in ALL_FILES if name in owned) + ((MARKER,) if manifest else ())
    existing = [(target / name, Path("app") / name) for name in names]
    if indicator:
        existing.extend([(desktop, Path("launcher.desktop")), (autostart, Path("autostart.desktop"))])
    extension, service = chat_locations(home)
    if chat and (extension / EXTENSION_MARKER).is_file():
        extension_manifest = read_manifest(extension, EXTENSION_MARKER)
        extension_owned = set(extension_manifest.get("files", EXTENSION_FILES))
        existing.extend((extension / name, Path("extension") / name)
                        for name in (*EXTENSION_FILES, EXTENSION_MARKER)
                        if name in extension_owned or name == EXTENSION_MARKER)
    if chat:
        existing.append((service, Path("quick-chat.service")))
    existing = [(source, relative) for source, relative in existing
                if source.is_file() and not source.is_symlink()
                and (relative.parts[0] in ("app", "extension") or DESKTOP_MARKER in source.read_text())]
    if not existing:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = home / ".local/share" / (APP_NAME + "-backups") / stamp
    for source, relative in existing:
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    return destination


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".codex-install-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content if isinstance(content, bytes) else content.encode())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def check_dependencies(*, indicator, chat):
    import gi
    if indicator:
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
    if chat:
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
    return find_codex()


def write_manifest(target, components):
    names = list(COMMON_FILES) if components else []
    if "indicator" in components:
        names.extend(INDICATOR_FILES)
    if "chat" in components:
        names.extend(CHAT_FILES)
    atomic_write(target / MARKER, json.dumps({"app": APP_NAME, "version": VERSION,
                 "components": sorted(components), "files": names}) + "\n")


def install(home, autostart_enabled=True, *, with_chat=False, chat_only=False):
    home = Path(home).resolve()
    checked_value(home)
    indicator, chat = not chat_only, with_chat or chat_only
    codex = check_dependencies(indicator=indicator, chat=chat)
    checked_value(codex)
    source = Path(__file__).resolve().parent
    names = COMMON_FILES + (INDICATOR_FILES if indicator else ()) + (CHAT_FILES if chat else ())
    extension_sources = {name: source / "extension" / name for name in EXTENSION_FILES} if chat else {}
    for path in [*(source / name for name in names), *extension_sources.values()]:
        if not path.is_file():
            raise RuntimeError(f"Missing application file: {path.relative_to(source)}")
    if chat:
        metadata = json.loads((source / "extension/metadata.json").read_text())
        if metadata.get("uuid") != EXTENSION_UUID or metadata.get("shell-version") != ["50"]:
            raise RuntimeError("Quick Chat extension metadata must target its UUID and GNOME 50.")
    target, desktop, autostart = locations(home)
    manifest = check_owned(target, desktop, autostart, indicator=indicator, chat=chat, home=home)
    components = installed_components(manifest)
    owned = set(manifest.get("files", FILES)) if manifest else set()
    for name in names:
        if (target / name).exists() and name not in owned:
            raise RuntimeError(f"An unrelated application file exists: {target / name}")
    if chat:
        extension, _service = chat_locations(home)
        extension_manifest = read_manifest(extension, EXTENSION_MARKER)
        extension_owned = set(extension_manifest.get("files", EXTENSION_FILES)) if extension_manifest else set()
        for name in EXTENSION_FILES:
            if (extension / name).exists() and name not in extension_owned:
                raise RuntimeError(f"An unrelated extension file exists: {extension / name}")
    backup = backup_managed(home, target, desktop, autostart, indicator=indicator, chat=chat)
    for name in names:
        atomic_write(target / name, (source / name).read_bytes())
    if indicator:
        components.add("indicator")
        command = " ".join(desktop_quote(part) for part in
                           ("/usr/bin/python3", target / "indicator.py", "--codex", codex))
        entry = f"""[Desktop Entry]
Type=Application
Version=1.0
Name=Codex Usage
Comment=Remaining Codex usage in your top bar
Exec={command}
Icon={target / 'icons/codex-usage-symbolic.svg'}
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
{DESKTOP_MARKER}
"""
        atomic_write(desktop, entry)
        if autostart_enabled:
            atomic_write(autostart, entry + "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=10\n")
        elif autostart.exists():
            autostart.unlink()
    if chat:
        components.add("chat")
        extension, service = chat_locations(home)
        for name, path in extension_sources.items():
            atomic_write(extension / name, path.read_bytes())
        atomic_write(extension / EXTENSION_MARKER, json.dumps({"app": APP_NAME, "version": VERSION,
                     "files": EXTENSION_FILES}) + "\n")
        # D-Bus parses quoted arguments without running a shell. Percent is
        # literal here, unlike the field-code escaping used by desktop Exec.
        command = " ".join(shlex.quote(checked_value(part)) for part in
                           ("/usr/bin/python3", target / "chat_service.py", "--codex", codex))
        atomic_write(service, f"[D-BUS Service]\nName={DBUS_NAME}\nExec={command}\n{DESKTOP_MARKER}\n")
    write_manifest(target, components)
    if indicator and home == Path.home() and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Installed: {target}")
    if indicator:
        print(f"Launcher: {desktop}")
        print("Indicator starts at login: " + ("enabled" if autostart_enabled else "disabled"))
    if chat:
        print("Quick Chat installed. Log out and back in, then enable Codex Quick Chat in Extensions.")
        print("The helper starts on demand through your session D-Bus; no system service is installed.")
    if backup:
        print(f"Previous files preserved: {backup}")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--with-chat", action="store_true", help="Install the indicator and GNOME 50 Quick Chat")
    mode.add_argument("--chat-only", action="store_true", help="Install only Quick Chat; leave any indicator unchanged")
    parser.add_argument("--no-autostart", action="store_true", help="Disable login startup for the indicator being installed")
    parser.add_argument("--target-home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        install(args.target_home, not args.no_autostart, with_chat=args.with_chat, chat_only=args.chat_only)
    except (ImportError, OSError, ValueError, RuntimeError, UsageError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
