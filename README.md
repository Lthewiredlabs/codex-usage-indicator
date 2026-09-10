# Codex Usage

A small app that shows your remaining Codex allowance in the GNOME top bar, alongside system resource indicators.

**Panel example:** `Codex 80% · 40%` (illustrative values)

The first number is the five-hour allowance remaining; the second is the weekly allowance remaining. The menu labels the actual window lengths reported by your account and shows reset times in your local timezone. Any additional usage buckets appear in the menu.

- Refreshes every minute, with **Refresh now** in the menu.
- Starts when you sign in. Search for **Codex Usage** in the application launcher to reopen it after quitting.
- Uses your existing local Codex sign-in. No API key needs to be copied into this app.
- Only reads usage. It does not start AI conversations, run model requests, buy credits, or use reset credits.
- A `~` means the numbers are from the last successful check. A `—` means unavailable. A passed reset time is never treated as proof that the allowance is full again.

The app keeps its last successful reading in memory and does not store account details or credentials. Codex itself handles its usual authentication and network connection. It works while the Codex desktop window is closed, provided the installed Codex executable and sign-in remain available.

## Install and launch

Tested on GNOME 46 with GTK 3 and an enabled AppIndicator extension. It appears in the existing panel without requiring a GNOME restart.

Requirements on Ubuntu:

- Python 3.10 or newer, using the system Python for GTK support.
- System packages `python3-gi`, `gir1.2-gtk-3.0`, and `gir1.2-ayatanaappindicator3-0.1`.
- An enabled GNOME AppIndicator extension, such as Ubuntu AppIndicators or AppIndicator and KStatusNotifierItem Support.
- Codex installed locally and signed in with a ChatGPT account. API-key-only accounts do not provide the same subscription usage windows.

Download and extract the repository's **Code → Download ZIP**, or clone it:

```sh
git clone https://github.com/Lthewiredlabs/codex-usage-indicator.git
cd codex-usage-indicator
```

From the extracted or cloned folder:

```sh
/usr/bin/python3 install.py
```

Open **Codex Usage** from the application launcher. Installation enables startup at login. Use `/usr/bin/python3 install.py --no-autostart` to install without login startup. Updates preserve the previous managed files in `~/.local/share/codex-usage-indicator-backups/`.

To run directly from source:

```sh
/usr/bin/python3 indicator.py
```

The app looks for Codex on your PATH and in the common Linux desktop install locations. When running `indicator.py` or `usage_reader.py`, `--codex /absolute/path/to/codex` selects an executable explicitly. The installer uses automatic detection. Only one indicator instance runs per user session.

## Remove

Choose **Quit Codex Usage** in the panel menu to stop it for this session. To remove the installed app and login startup:

```sh
/usr/bin/python3 ~/.local/share/codex-usage-indicator/uninstall.py
```

Removal backs up the managed application files first, preserves unrelated files, and leaves Codex and its sign-in untouched. Python bytecode caches may remain in the old application folder.

## Verification

Run these from the source folder; the installed application does not include the tests:

```sh
python3 -m unittest discover -s tests -v
/usr/bin/python3 usage_reader.py
```

The 15 tests cover remaining-percentage calculation, unknown and stale values, passed reset times, multiple buckets, protocol initialization, read-only request selection, timeout cleanup, and sanitized errors. Separate manual validation on GNOME 46 on September 10, 2026 confirmed the exported indicator label, menu, single-instance behavior, and automatic refresh.

## How it reads usage

The reader briefly starts the installed `codex app-server` over local standard input/output, initializes the protocol, and requests `account/rateLimits/read`. It prefers `rateLimitsByLimitId`, falling back to `rateLimits`. The displayed allowance is `100 - usedPercent`, limited to 0–100. This is the shared Codex account allowance, not the context window of an individual conversation.

The protocol is documented in [OpenAI's Codex App Server documentation](https://learn.chatgpt.com/docs/app-server). The GNOME panel integration uses [Ayatana AppIndicator](https://github.com/AyatanaIndicators/libayatana-appindicator). Future Codex protocol changes may require an update to the reader.
