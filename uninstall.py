#!/usr/bin/env python3
"""Remove the current user's Codex Usage installation, preserving a backup."""

import argparse
from pathlib import Path
import subprocess

from install import FILES, MARKER, backup_managed, check_owned, locations


def uninstall(home):
    target, desktop, autostart = locations(home)
    check_owned(target, desktop, autostart)
    backup = backup_managed(home, target, desktop, autostart)
    if target.exists() and home == Path.home():
        subprocess.run(["/usr/bin/python3", str(target / "indicator.py"), "--quit"],
                       timeout=5, check=False)
    for path in (desktop, autostart, *(target / name for name in (*FILES, MARKER))):
        if path.is_file() or path.is_symlink():
            path.unlink()
    for folder in (target / "icons", target):
        try:
            folder.rmdir()
        except OSError:
            pass  # Keep any files not owned by this application.
    print("Codex Usage removed; automatic startup is disabled.")
    if backup:
        print(f"Application files preserved: {backup}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    uninstall(args.target_home.resolve())


if __name__ == "__main__":
    main()
