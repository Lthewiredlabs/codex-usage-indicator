#!/usr/bin/env python3
"""Remove Codex Usage components, preserving backups, reset state, and chat history."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from install import (APP_NAME, CHAT_FILES, COMMON_FILES, EXTENSION_FILES, EXTENSION_MARKER,
                     EXTENSION_UUID, FILES, INDICATOR_FILES, MARKER, backup_managed, chat_locations,
                     atomic_write, check_owned, installed_components, locations, read_manifest, write_manifest)


def stop_component(command):
    try:
        result = subprocess.run(command, timeout=8, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            print("Could not confirm the component stopped. Log out and back in to finish stopping it.")
    except (OSError, subprocess.TimeoutExpired):
        print("Could not confirm the component stopped. Log out and back in to finish stopping it.")


def remove_files(root, names):
    for name in names:
        path = root / name
        if path.is_file():
            path.unlink()
    folders = {root}
    for name in names:
        folders.update(parent for parent in (root / name).parents if parent == root or root in parent.parents)
    for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass  # Keep unrelated files and bytecode caches; never delete trees.


def uninstall(home, *, chat_only=False, indicator_only=False):
    home = Path(home).resolve()
    target, desktop, autostart = locations(home)
    indicator, chat = not chat_only, not indicator_only
    manifest = check_owned(target, desktop, autostart, indicator=indicator, chat=chat, home=home)
    components = installed_components(manifest)
    owned = set(manifest.get("files", FILES)) if manifest else set()
    backup = backup_managed(home, target, desktop, autostart, indicator=indicator, chat=chat)
    extension, service = chat_locations(home)
    if home == Path.home():
        if chat:
            if extension.exists() and shutil.which("gnome-extensions"):
                stop_component(["gnome-extensions", "disable", EXTENSION_UUID])
            if "chat_service.py" in owned and (target / "chat_service.py").is_file():
                stop_component(["/usr/bin/python3", str(target / "chat_service.py"), "--quit"])
        if indicator and "indicator.py" in owned and (target / "indicator.py").is_file():
            stop_component(["/usr/bin/python3", str(target / "indicator.py"), "--quit"])
    if indicator:
        for path in (desktop, autostart):
            if path.is_file():
                path.unlink()
        remove_files(target, tuple(name for name in INDICATOR_FILES if name in owned))
        components.discard("indicator")
    if chat:
        if service.is_file():
            service.unlink()
        extension_manifest = read_manifest(extension, EXTENSION_MARKER)
        extension_owned = set(extension_manifest.get("files", EXTENSION_FILES)) if extension_manifest else set()
        remove_files(extension, tuple(name for name in EXTENSION_FILES if name in extension_owned))
        if extension.exists():
            if list(extension.iterdir()) == [extension / EXTENSION_MARKER]:
                remove_files(extension, (EXTENSION_MARKER,))
            elif extension_manifest:
                # Retain ownership if unrelated files prevent removing the directory.
                atomic_write(extension / EXTENSION_MARKER, json.dumps({"app": APP_NAME, "files": []}) + "\n")
        remove_files(target, tuple(name for name in CHAT_FILES if name in owned))
        components.discard("chat")
    if components:
        write_manifest(target, components)
    elif target.exists():
        remove_files(target, tuple(name for name in COMMON_FILES if name in owned))
        if list(target.iterdir()) == [target / MARKER]:
            remove_files(target, (MARKER,))
        else:
            write_manifest(target, set())
    print("Requested Codex components removed. Reset state and chat history were preserved.")
    if backup:
        print(f"Application files preserved: {backup}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chat-only", action="store_true", help="Remove Quick Chat, keeping the indicator")
    mode.add_argument("--indicator-only", action="store_true", help="Remove the indicator, keeping Quick Chat")
    parser.add_argument("--target-home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        uninstall(args.target_home, chat_only=args.chat_only, indicator_only=args.indicator_only)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Removal stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
