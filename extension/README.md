# GNOME Quick Chat surface

This extension targets GNOME Shell **50**. Its card sits above the existing
notification list inside the clock menu. It preserves the native notification
widget and calendar, then restores the notification widget's original parent
and position when disabled. It does not add another panel menu.

The composer remains in place while responses change, so incoming text never
recreates the entry or steals its focus. Enter or Send explicitly submits a
message. Input is limited to 4,000 Unicode characters. The response preview is
limited to four messages, 3,000 characters each, in a bounded scroll area.
Open in Codex is the path to the full conversation. No backend strings are
interpreted as markup.

Bit uses local symbolic SVGs. Idle blinking and thinking frames run only while
the clock menu is open and GNOME's `enable-animations` preference is enabled.
The extension cancels outstanding D-Bus requests and removes its timer and
external signal handlers when disabled.

## Service contract

The session bus name and interface are `org.codex.QuickChat`; the object path is
`/org/codex/QuickChat`. All proxy construction and method calls are asynchronous.

- `GetState() → s`: JSON state; also emitted by `Changed(s)`.
- `Send(s)`, `Stop()`, `NewChat()`, `OpenChat()`, `RefreshUsage()`.
- State fields: `status`, `thread_id`, `messages`, `error`, `usage_label`, and
  `usage_stale`. Unknown fields are ignored; invalid state becomes offline.

## Verification

Run the pure display/input checks with:

```sh
gjs -m extension/tests/test-state.js
```

The separate `tests/gnome_smoke.js` harness is intended for
`gnome-shell-test-tool --headless --extension ZIP ABSOLUTE_TEST_SCRIPT` inside
`dbus-run-session`. It exports a fake service on that isolated session bus and
does not call Codex or consume account usage. It checks native actor placement,
entry focus, explicit send/stop/new-chat behavior, preview bounds, disabled
animation, offline recovery, and repeated enable/disable cleanup.

The surface uses GNOME internal date-menu actors, so a new Shell major version
requires checking the upstream structure before extending metadata support.

Verified against the official **50.1** sources:

- [dateMenu.js](https://raw.githubusercontent.com/GNOME/gnome-shell/50.1/js/ui/dateMenu.js)
- [calendar.js](https://raw.githubusercontent.com/GNOME/gnome-shell/50.1/js/ui/calendar.js)
- [popupMenu.js](https://raw.githubusercontent.com/GNOME/gnome-shell/50.1/js/ui/popupMenu.js)
- [St.ScrollView API](https://raw.githubusercontent.com/GNOME/gnome-shell/50.1/src/st/st-scroll-view.h)
