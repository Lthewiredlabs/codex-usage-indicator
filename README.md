# Codex Usage

A small app that shows your remaining Codex allowance in the GNOME top bar, alongside system resource indicators. An optional **Codex Quick Chat** extension adds an animated mascot and a chat card inside the clock's notification panel. Install either component or both from this repository.

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

## Quick Chat

Click the **clock** to find the Codex chat card above your notifications, with your remaining usage, recent messages, and a message box. **Bit**, the terminal-shaped mascot, blinks while idle, glances while thinking, and changes expression when a reply arrives or something needs attention. Animations pause when the clock menu is closed and respect GNOME's **Enable Animations** setting.

- **Send** sends the text you entered to Codex and consumes your Codex allowance. Opening the menu and refreshing usage do not send model prompts.
- Replies stream into the menu. **Stop** requests cancellation of the current reply; work already completed may still count toward usage.
- The extension uses its own dedicated Codex conversation. **New chat** starts a separate conversation; it does not delete previous Codex history.
- **Open in Codex** asks the desktop app to open that conversation. Opening the exact conversation depends on the installed desktop app's support for the link; this integration is not a guarantee that every Codex version will navigate successfully.
- Quick Chat has no usage-reset control. Reset confirmation and retry remain in the optional usage indicator.
- It uses your existing local Codex sign-in. No API key is required, and no credentials are copied into the extension.

This first Quick Chat release supports **GNOME Shell 50 only**. The original AppIndicator app remains a separate component and keeps its existing behavior.

Codex owns the conversation history. The helper does not keep a separate transcript or prompt log. Its private state lives at `~/.local/state/codex-usage-indicator/quickchat/` (or beneath an absolute `XDG_STATE_HOME`). This contains the saved thread ID and a hash of the signed-in email, fixed app instructions, and an isolated working directory. Uninstalling preserves this state and Codex's history.

## Use a reset

1. Open the indicator menu and check **Available resets**.
2. Select **Reset usage…**. The app checks your account again.
3. Choose **Use 1 reset** to confirm, or **Cancel** to keep it.

The server decides which usage limits are eligible for the reset. The app reads the updated usage after the request; it never assumes the percentages returned to 100%.

If the connection drops before a result is confirmed, use **Retry previous reset…**. The saved request is reused, even after restarting the app. A reset that already succeeded will not spend a second credit. Normal refreshes never retry a reset automatically. The same sign-in is required for retrying; the available account interface identifies sign-ins by email and does not distinguish workspaces sharing an email.

Pending reset state lives at `~/.local/state/codex-usage-indicator/reset-attempt.json` (or beneath an absolute `XDG_STATE_HOME`). The file is private to your user. Corrupt or unreadable pending state disables resets instead of generating a new request. Do not delete a pending record to work around an uncertain result; retry the saved request first.

## Install and launch

The original indicator was tested on GNOME 46 with GTK 3 and an enabled AppIndicator extension. It appears in the existing panel without requiring a GNOME restart. Quick Chat uses a native GNOME Shell extension and requires GNOME Shell 50.

Requirements on Ubuntu:

- Python 3.10 or newer, using the system Python for GTK support.
- System package `python3-gi` with GLib/Gio bindings. The indicator also needs `gir1.2-gtk-3.0` and `gir1.2-ayatanaappindicator3-0.1`.
- For the indicator: an enabled GNOME AppIndicator extension, such as Ubuntu AppIndicators or AppIndicator and KStatusNotifierItem Support.
- For Quick Chat: GNOME Shell 50 and the Extensions application (or the `gnome-extensions` command).
- Codex installed locally and signed in with a ChatGPT account. API-key-only accounts do not provide the same subscription usage windows.

Download and extract the repository's **Code → Download ZIP**, or clone it:

```sh
git clone https://github.com/Lthewiredlabs/codex-usage-indicator.git
cd codex-usage-indicator
```

From the extracted or cloned folder, choose the components you want:

| Components | Install command |
| --- | --- |
| Usage indicator | `/usr/bin/python3 install.py` |
| Usage indicator and Quick Chat | `/usr/bin/python3 install.py --with-chat` |
| Quick Chat only | `/usr/bin/python3 install.py --chat-only` |

Installation is for your user only and does not need administrator privileges. `--chat-only` leaves an existing indicator, its launcher, and its startup setting unchanged. To replace an existing indicator with Quick Chat, install Quick Chat and then explicitly remove the indicator using the removal command below.

For the indicator, open **Codex Usage** from the application launcher. Installation enables startup at login. Add `--no-autostart` when installing the indicator to disable its login startup; this flag has no effect with `--chat-only`.

For Quick Chat, **log out and back in**, then open **Extensions** and enable **Codex Quick Chat**. You can also enable it from a terminal after signing back in:

```sh
gnome-extensions enable codex-quick-chat@lthewiredlabs
```

The installer does not enable extensions or restart GNOME. On Wayland, signing out and back in lets GNOME discover newly installed extension files. Reinstalling uses the same commands; sign out and back in after a Quick Chat update so the extension and helper both reload. Updates preserve the previous managed files in `~/.local/share/codex-usage-indicator-backups/`.

Quick Chat's helper starts on demand through your session D-Bus when the enabled extension requests it. It may remain running for usage refreshes after the menu closes. No system service, background model conversation, or timed message sending is installed. The installed files are:

- Shared helpers: `~/.local/share/codex-usage-indicator/`
- GNOME extension: `~/.local/share/gnome-shell/extensions/codex-quick-chat@lthewiredlabs/`
- Session activation file: `~/.local/share/dbus-1/services/org.codex.QuickChat.service`

To run directly from source:

```sh
/usr/bin/python3 indicator.py
```

The app looks for Codex on your PATH and in the common Linux desktop install locations. When running `indicator.py`, `usage_reader.py`, or `chat_service.py`, `--codex /absolute/path/to/codex` selects an executable explicitly. The installer uses automatic detection. Only one indicator instance and one Quick Chat helper run per user session.

## Remove

Choose **Quit Codex Usage** in the indicator menu to stop it for this session. Turn off **Codex Quick Chat** in Extensions to remove its card from the clock panel. To remove both installed components and login startup:

```sh
/usr/bin/python3 ~/.local/share/codex-usage-indicator/uninstall.py
```

To remove only one component, keep the other and its shared helpers:

```sh
/usr/bin/python3 ~/.local/share/codex-usage-indicator/uninstall.py --chat-only
/usr/bin/python3 ~/.local/share/codex-usage-indicator/uninstall.py --indicator-only
```

Run only the command for the component you want to remove. Removal disables the selected extension or indicator, stops its own helper when reachable, and backs up managed files first. It preserves unrelated files, Codex and its sign-in, chat history, and pending reset state. Python bytecode caches and a small ownership marker may remain so a later reinstall remains safe. Preserving a pending reset prevents an unresolved request from being repeated with a new request ID.

## Verification

Run these from the source folder; the installed application does not include the tests:

```sh
python3 -m unittest discover -s tests -v
gjs -m extension/tests/test-state.js
/usr/bin/python3 usage_reader.py
```

Tests cover remaining percentages, unknown and stale counts, reset confirmation and cancellation, durable retry keys, account changes, reset outcomes, timeout cleanup, and sanitized errors. Installer tests use temporary home directories to verify independent component installs, preserved startup settings, upgrades from the original release, selective removal, backups, paths containing spaces and percent signs, and unrelated-file protection. They do not activate desktop services or modify your real installation. Reset and chat protocol tests use simulated backends; testing does not send real prompts or spend real credits. UI callback tests require the system GTK/AppIndicator dependencies and are skipped when unavailable. Separate manual validation on GNOME 46 on September 10, 2026 confirmed the original indicator label, menu, single-instance behavior, and automatic refresh.

Quick Chat was also checked in an isolated GNOME Shell 50.1 session: 30 checks cover typing, sending, stopping, scrolling, animation, reduced motion, reconnecting, and restoring the native notification layout after disable/re-enable. The harness uses a fake local chat service and does not send prompts:

```sh
mkdir -p dist
gnome-extensions pack --force --out-dir=dist --extra-source=stateModel.js --extra-source=icons extension
dbus-run-session -- gnome-shell-test-tool --headless --extension dist/codex-quick-chat@lthewiredlabs.shell-extension.zip "$PWD/tests/gnome_smoke.js"
```

A separate release smoke test sent one short real prompt, verified streamed replies and restored history, then archived the test conversation. It used ordinary allowance, with no reset credit redeemed.

Quick Chat is limited to conversation: its helper verifies read-only operation and disables command tools, connectors, plugins, browsing, and configured MCP servers before submitting a message. Requests requiring those capabilities belong in the full Codex app.

## How it reads usage

The reader briefly starts the installed `codex app-server` over local standard input/output, initializes the protocol, and requests `account/rateLimits/read` and `account/read`. It prefers `rateLimitsByLimitId`, falling back to `rateLimits`. The displayed allowance is `100 - usedPercent`, limited to 0–100. This is the shared Codex account allowance, not the context window of an individual conversation.

The reset count comes from `rateLimitResetCredits.availableCount`, which is authoritative even if detailed credit rows are unavailable or incomplete. A confirmed reset calls `account/rateLimitResetCredit/consume` with the saved idempotency key. When credit details are available, the app selects an available credit with the earliest known expiry. Server outcomes distinguish a newly applied reset, an already-applied attempt, no available credit, and nothing eligible to reset.

The protocol is documented in [OpenAI's Codex App Server documentation](https://learn.chatgpt.com/docs/app-server). The GNOME panel integration uses [Ayatana AppIndicator](https://github.com/AyatanaIndicators/libayatana-appindicator). Future Codex protocol changes may require an update to the reader.
