#!/usr/bin/env python3
"""Install Codex Usage for the current user; no administrator privileges needed."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

from usage_reader import UsageError, find_codex

APP_NAME = "codex-usage-indicator"
MARKER = ".codex-usage-managed.json"
FILES = ("indicator.py", "usage_reader.py", "ui_model.py", "reset_state.py", "install.py", "uninstall.py",
         "README.md", "icons/codex-usage-symbolic.svg")
DESKTOP_MARKER = "X-Codex-Usage-Managed=true"


def locations(home):
    return (home / ".local/share" / APP_NAME,
            home / ".local/share/applications" / (APP_NAME + ".desktop"),
            home / ".config/autostart" / (APP_NAME + ".desktop"))


def desktop_quote(value):
    # Desktop Exec syntax is not a shell. Percent must escape field-code expansion.
    value = str(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def check_owned(target, desktop, autostart):
    if target.is_symlink():
        raise RuntimeError("The install folder is a symbolic link; leaving it unchanged.")
    if target.exists():
        marker = target / MARKER
        if not marker.is_file() or json.loads(marker.read_text()).get("app") != APP_NAME:
            raise RuntimeError("The install folder already contains unrelated files; leaving it unchanged.")
    for path in (desktop, autostart):
        if path.is_symlink() or (path.exists() and DESKTOP_MARKER not in path.read_text()):
            raise RuntimeError(f"An unrelated launcher exists at {path}; leaving it unchanged.")


def backup_managed(home, target, desktop, autostart):
    existing = [(target / name, Path("app") / name) for name in (*FILES, MARKER)]
    existing.extend([(desktop, Path("launcher.desktop")), (autostart, Path("autostart.desktop"))])
    existing = [(source, relative) for source, relative in existing if source.is_file()]
    if not existing:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = home / ".local/share" / (APP_NAME + "-backups") / stamp
    for source, relative in existing:
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    return destination


def install(home, autostart_enabled=True):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    codex = find_codex()
    source = Path(__file__).resolve().parent
    for name in FILES:
        if not (source / name).is_file():
            raise RuntimeError(f"Missing application file: {name}")
    target, desktop, autostart = locations(home)
    check_owned(target, desktop, autostart)
    backup = backup_managed(home, target, desktop, autostart)
    for name in FILES:
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        if source / name != output:
            staging = output.with_name(output.name + ".new")
            shutil.copy2(source / name, staging)
            staging.replace(output)
    (target / MARKER).write_text(json.dumps({"app": APP_NAME, "version": "0.2.0", "files": FILES}) + "\n")
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
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(entry)
    if autostart_enabled:
        autostart.parent.mkdir(parents=True, exist_ok=True)
        autostart.write_text(entry + "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=10\n")
    elif autostart.exists():
        autostart.unlink()  # Already verified as ours and backed up above.
    if home == Path.home() and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Installed: {target}")
    print(f"Launcher: {desktop}")
    print("Start at login: " + ("enabled" if autostart_enabled else "disabled"))
    if backup:
        print(f"Previous files preserved: {backup}")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--target-home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        install(args.target_home.resolve(), not args.no_autostart)
    except (ImportError, OSError, ValueError, RuntimeError, UsageError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
