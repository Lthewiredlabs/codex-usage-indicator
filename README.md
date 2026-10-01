# Codex Usage

A small app that shows your remaining Codex allowance in the GNOME top bar, alongside system resource indicators.

**Panel example:** `Codex 80% · 40% · ↻2` (illustrative values)

The first number is the five-hour allowance remaining; the second is the weekly allowance remaining. `↻2` means two usage resets are available. The menu labels the actual window lengths reported by your account and shows reset times in your local timezone. Any additional usage buckets appear in the menu.

- Refreshes every minute, with **Refresh now** in the menu.
- Starts when you sign in. Search for **Codex Usage** in the application launcher to reopen it after quitting.
- Uses your existing local Codex sign-in. No API key needs to be copied into this app.
- Normal refreshes only read account information. They do not start AI conversations, run model requests, or buy credits.
- **Reset usage…** checks availability and asks for confirmation before using one existing reset. Cancel is the default.
- A `~` means the numbers are from the last successful check. A `—` means unavailable. A passed reset time is never treated as proof that the allowance is full again.
- `↻0` means no resets are available; `↻?` means the count is unknown; `↻~2` means two were available at the last successful check. The reset button is disabled while data is stale, unavailable, or another operation is in progress.

The app keeps usage readings in memory and never stores credentials. If you confirm a reset, it saves a small private record containing a request UUID, a hash of the signed-in email, and the selected reset credit ID when available. This lets an interrupted reset be retried without spending another credit. Codex itself handles authentication and network access. The indicator works while the Codex desktop window is closed, provided the installed Codex executable and sign-in remain available.

## Use a reset

1. Open the indicator menu and check **Available resets**.
2. Select **Reset usage…**. The app checks your account again.
3. Choose **Use 1 reset** to confirm, or **Cancel** to keep it.

The server decides which usage limits are eligible for the reset. The app reads the updated usage after the request; it never assumes the percentages returned to 100%.

If the connection drops before a result is confirmed, use **Retry previous reset…**. The saved request is reused, even after restarting the app. A reset that already succeeded will not spend a second credit. Normal refreshes never retry a reset automatically. The same sign-in is required for retrying; the available account interface identifies sign-ins by email and does not distinguish workspaces sharing an email.

Pending reset state lives at `~/.local/state/codex-usage-indicator/reset-attempt.json` (or beneath an absolute `XDG_STATE_HOME`). The file is private to your user. Corrupt or unreadable pending state disables resets instead of generating a new request. Do not delete a pending record to work around an uncertain result; retry the saved request first.

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

Removal backs up the managed application files first, preserves unrelated files, and leaves Codex and its sign-in untouched. Python bytecode caches may remain in the old application folder. Pending reset state is preserved so reinstalling cannot accidentally repeat an unresolved reset with a new request.

## Verification

Run these from the source folder; the installed application does not include the tests:

```sh
python3 -m unittest discover -s tests -v
/usr/bin/python3 usage_reader.py
```

Tests cover remaining percentages, unknown and stale counts, reset confirmation and cancellation, durable retry keys, account changes, reset outcomes, timeout cleanup, and sanitized errors. Reset requests use simulated Codex backends; testing does not spend real credits. UI callback tests require the system GTK/AppIndicator dependencies and are skipped when unavailable. Separate manual validation on GNOME 46 on September 10, 2026 confirmed the original indicator label, menu, single-instance behavior, and automatic refresh.

## How it reads usage

The reader briefly starts the installed `codex app-server` over local standard input/output, initializes the protocol, and requests `account/rateLimits/read` and `account/read`. It prefers `rateLimitsByLimitId`, falling back to `rateLimits`. The displayed allowance is `100 - usedPercent`, limited to 0–100. This is the shared Codex account allowance, not the context window of an individual conversation.

The reset count comes from `rateLimitResetCredits.availableCount`, which is authoritative even if detailed credit rows are unavailable or incomplete. A confirmed reset calls `account/rateLimitResetCredit/consume` with the saved idempotency key. When credit details are available, the app selects an available credit with the earliest known expiry. Server outcomes distinguish a newly applied reset, an already-applied attempt, no available credit, and nothing eligible to reset.

The protocol is documented in [OpenAI's Codex App Server documentation](https://learn.chatgpt.com/docs/app-server). The GNOME panel integration uses [Ayatana AppIndicator](https://github.com/AyatanaIndicators/libayatana-appindicator). Future Codex protocol changes may require an update to the reader.
