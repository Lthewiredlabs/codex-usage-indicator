// Run ONLY inside the isolated headless Shell:
// dbus-run-session -- gnome-shell-test-tool --headless --extension ZIP THIS_FILE
// A private in-process D-Bus fake owns the chat name before any Send is tested.
// No Codex process, user account, credentials, or model request is used.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Scripting from 'resource:///org/gnome/shell/ui/scripting.js';
import {ExtensionState} from 'resource:///org/gnome/shell/misc/extensionUtils.js';

const UUID = 'codex-quick-chat@lthewiredlabs';
const IFACE = `<node><interface name="org.codex.QuickChat">
<method name="GetState"><arg type="s" direction="out"/></method>
<method name="Send"><arg type="s" direction="in"/></method>
<method name="Stop"/><method name="NewChat"/><method name="OpenChat"/><method name="RefreshUsage"/>
<signal name="Changed"><arg type="s"/></signal>
</interface></node>`;

export const METRICS = {};
let checks = 0;
function assert(value, message) {
    if (!value)
        throw new Error(`Quick Chat smoke: ${message}`);
    checks++;
    print(`PASS: ${message}`);
}

async function until(predicate, message, timeout = 6000) {
    const deadline = GLib.get_monotonic_time() + timeout * 1000;
    while (!predicate()) {
        if (GLib.get_monotonic_time() >= deadline)
            throw new Error(`Timed out: ${message}`);
        await Scripting.sleep(50);
    }
}

export async function run() {
    let ownerId = 0;
    let exported = null;
    const manager = Main.extensionManager;
    await until(() => manager.lookup(UUID)?.state === ExtensionState.ACTIVE || manager.lookup(UUID)?.error,
        'extension load');
    const extension = manager.lookup(UUID);
    assert(extension.state === ExtensionState.ACTIVE, `extension loads: ${extension.error || 'active'}`);
    const app = extension.stateObj;
    const dateMenu = Main.panel.statusArea.dateMenu;
    const nativeList = dateMenu._messageList;
    const originalContainer = app._column.get_parent();
    const originalIndex = originalContainer.get_children().indexOf(app._column);
    const nativeChildren = nativeList.get_children();
    const nativeCalendar = dateMenu._calendar;
    const initialState = () => ({
        status: 'idle', thread_id: null, messages: [], error: null,
        usage_label: '5h 85% · Week 72%', usage_stale: false,
    });
    let state = initialState();
    const calls = {send: 0, stop: 0, newChat: 0, open: 0, refresh: 0};
    const publish = () => exported.emit_signal('Changed', new GLib.Variant('(s)', [JSON.stringify(state)]));
    const service = {
        GetState() { return JSON.stringify(state); },
        Send(message) {
            calls.send++;
            state = {...state, status: 'thinking', thread_id: 'fake-smoke-thread',
                messages: [...state.messages, {role: 'user', text: message}]};
            publish();
        },
        Stop() { calls.stop++; state = {...state, status: 'idle'}; publish(); },
        NewChat() { calls.newChat++; state = initialState(); publish(); },
        OpenChat() { calls.open++; },
        RefreshUsage() { calls.refresh++; publish(); },
    };
    try {
        exported = Gio.DBusExportedObject.wrapJSObject(IFACE, service);
        exported.export(Gio.DBus.session, '/org/codex/QuickChat');
        await new Promise((resolve, reject) => {
            ownerId = Gio.bus_own_name_on_connection(Gio.DBus.session, 'org.codex.QuickChat',
                Gio.BusNameOwnerFlags.NONE, resolve,
                () => reject(new Error('Cannot own fake chat bus name; refusing Send tests')));
        });
        await app._getState();
        await until(() => app._state.status === 'idle', 'fake backend connection');
        assert(calls.send === 0, 'opening/connecting never sends a model request');
        assert(app._entry.clutter_text.get_max_length() === 4000, 'entry has a hard 4000-character limit');
        assert(!app._openButton.reactive, 'Open in Codex is disabled before a thread exists');
        Main.overview.hide();
        await Scripting.sleep(400);
        dateMenu.menu.open();
        await Scripting.sleep(400);
        app._entry.grab_key_focus();
        app._entry.set_text('A fake smoke-test question');
        assert(dateMenu.menu.isOpen, 'the composer is inside the clock menu');
        assert(global.stage.get_key_focus() === app._entry.clutter_text, 'the composer accepts keyboard focus');
        assert(app._animationSource !== 0, 'the mascot animates while the clock menu is open');
        app._entry.clutter_text.emit('activate');
        await until(() => calls.send === 1 && !app._pending, 'explicit Enter send');
        assert(app._state.status === 'thinking' && app._stopButton.visible, 'explicit Send shows Thinking and Stop');
        assert(app._entry.get_text() === '', 'accepted input is cleared');
        assert(dateMenu.menu.isOpen && global.stage.get_key_focus() === app._entry.clutter_text,
            'sending preserves composer focus and the open clock menu');
        app._entry.set_text('Next draft');
        app._entry.clutter_text.emit('activate');
        await Scripting.sleep(100);
        assert(calls.send === 1, 'Enter while thinking cannot submit a duplicate');
        const frame = app._face.gicon.to_string();
        await Scripting.sleep(510);
        assert(app._face.gicon.to_string() !== frame, 'thinking advances the mascot frame');
        app._stopButton.emit('clicked', 1);
        await until(() => calls.stop === 1 && app._state.status === 'idle', 'Stop');
        assert(app._entry.get_text() === 'Next draft', 'stopping preserves the next draft');
        state = {...state, messages: [...state.messages,
            {role: 'assistant', text: 'A bounded reply preview. '.repeat(220)}]};
        publish();
        await Scripting.sleep(350);
        const screenshotPath = GLib.getenv('CODEX_SMOKE_SCREENSHOT');
        if (screenshotPath) {
            const output = Gio.File.new_for_path(screenshotPath).replace(null, false, Gio.FileCreateFlags.NONE, null);
            try {
                // The Shell screenshot module promisifies screenshot(bool,
                // GOutputStream). Capture only this isolated fake desktop.
                await new Shell.Screenshot().screenshot(false, output);
            } finally {
                output.close(null);
            }
            print(`SMOKE_SCREENSHOT ${screenshotPath}`);
        }
        assert(app._preview.visible && app._preview.height > 0 && app._preview.height < 300,
            'long responses have a bounded scroll preview');
        assert(app._preview.vadjustment.upper > app._preview.vadjustment.page_size,
            'long responses scroll');
        assert(app._card.width <= nativeList.width + 5, 'chat does not widen the native notification column');
        assert(app._card.height + nativeList.height < 710, 'card and native notifications fit the test monitor');
        assert(nativeCalendar === dateMenu._calendar && nativeList.get_children().every((child, i) => child === nativeChildren[i]),
            'calendar and native notification children remain intact');

        app._settings.set_boolean('enable-animations', false);
        await Scripting.sleep(100);
        assert(app._animationSource === 0, 'reduced motion stops mascot timers');
        app._settings.set_boolean('enable-animations', true);
        await Scripting.sleep(100);
        assert(app._animationSource !== 0, 'animation resumes when enabled');
        dateMenu.menu.close();
        assert(app._animationSource === 0, 'closed clock menu has no animation timer');
        dateMenu.menu.open();
        state = {status: 'error', thread_id: null, messages: [], error: 'Choose New chat',
            usage_label: 'Usage unavailable', usage_stale: true};
        publish();
        await Scripting.sleep(150);
        assert(app._newButton.reactive, 'New chat recovers an error with no thread id');
        app._newButton.emit('clicked', 1);
        await until(() => calls.newChat === 1 && app._state.status === 'idle', 'New chat');
        assert(calls.send === 1, 'New chat does not start a model request');

        exported.emit_signal('Changed', new GLib.Variant('(s)', ['{"status":"unexpected"}']));
        await Scripting.sleep(150);
        assert(app._state.status === 'offline' && !app._sendButton.reactive, 'malformed state produces a safe offline UI');
        app._refreshButton.emit('clicked', 1);
        await until(() => app._state.status === 'idle' && calls.refresh > 0, 'reconnect');
        Gio.bus_unown_name(ownerId);
        ownerId = 0;
        await until(() => app._state.status === 'offline', 'service disappearance');
        assert(app._error.visible && app._state.usage_stale, 'service disappearance marks usage stale and exposes recovery');

        app._entry.grab_key_focus();
        await manager._callExtensionDisable(UUID);
        assert(!app._enabled && !app._animationSource && !app._proxy, 'disable cancels animation and drops backend references');
        assert(nativeList.get_parent() === originalContainer && originalContainer.get_children()[originalIndex] === nativeList,
            'disable restores the original native notification parent and order');
        assert(nativeCalendar === dateMenu._calendar, 'disable preserves the native calendar');
        await manager._callExtensionEnable(UUID);
        assert(extension.state === ExtensionState.ACTIVE && app._card && nativeList.get_parent() === app._column,
            'extension re-enables without duplicate or destroyed actors');
        await manager._callExtensionDisable(UUID);
        assert(nativeList.get_parent() === originalContainer, 'second disable restores native layout again');
        print(`CODEX_QUICK_CHAT_SMOKE_PASS ${checks}`);
    } finally {
        if (ownerId)
            Gio.bus_unown_name(ownerId);
        exported?.unexport();
        dateMenu.menu.close();
    }
}
