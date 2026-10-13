import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {MAX_INPUT, canSend, characterCount, emptyState, normalizeState, statusLabel} from './stateModel.js';

const BUS_NAME = 'org.codex.QuickChat';
const OBJECT_PATH = '/org/codex/QuickChat';

function label(text, styleClass, wrap = false) {
    const actor = new St.Label({text, style_class: styleClass, x_expand: true});
    actor.clutter_text.ellipsize = wrap ? Pango.EllipsizeMode.NONE : Pango.EllipsizeMode.END;
    actor.clutter_text.line_wrap = wrap;
    actor.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    return actor;
}

function button(text, callback, extraClass = '') {
    const actor = new St.Button({
        label: text,
        style_class: `button codex-quick-chat-button ${extraClass}`,
        can_focus: true,
        reactive: true,
        accessible_name: text,
    });
    actor.connect('clicked', callback);
    return actor;
}

function sensitive(actor, enabled) {
    actor.reactive = enabled;
    actor.can_focus = enabled;
}

export default class CodexQuickChatExtension extends Extension {
    enable() {
        this._enabled = true;
        this._connections = [];
        this._cancellable = new Gio.Cancellable();
        this._state = emptyState();
        this._stateVersion = 0;
        this._connecting = true;
        this._pending = null;
        this._refreshing = false;
        this._animationSource = 0;
        this._animationMode = null;
        this._happyUntil = 0;
        this._messageSignature = null;
        this._proxy = null;
        this._nativeDestroyed = false;
        this._dateMenu = Main.panel.statusArea.dateMenu;
        this._nativeList = this._dateMenu?._messageList;
        const parent = this._nativeList?.get_parent();
        if (!parent) {
            this.disable();
            throw new Error('Codex Quick Chat requires the GNOME 50 notification column');
        }

        try {
            this._settings = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
            this._watch(this._settings, 'changed::enable-animations', () => this._syncAnimation(true));
            this._frames = new Map();
            for (const frame of ['idle', 'blink', 'thinking-left', 'thinking-right', 'happy', 'error']) {
                this._frames.set(frame, Gio.FileIcon.new(
                    this.dir.get_child('icons').get_child(`bit-${frame}-symbolic.svg`)));
            }
            this._buildCard();

            // GNOME 50.1 dateMenu.js creates _messageList as calendarArea's
            // first child. Keep that entire native widget, including its empty
            // placeholder, scroll view, Clear button and signals, intact.
            this._column = new St.BoxLayout({
                orientation: Clutter.Orientation.VERTICAL,
                style_class: 'codex-quick-chat-column',
                x_expand: true, y_expand: true,
                visible: this._nativeList.visible,
            });
            const position = parent.get_children().indexOf(this._nativeList);
            parent.remove_child(this._nativeList);
            parent.insert_child_at_index(this._column, position);
            this._column.add_child(this._card);
            this._column.add_child(this._nativeList);
            this._watch(this._nativeList, 'notify::visible', () => {
                this._column.visible = this._nativeList.visible;
                this._syncAnimation();
            });
            this._watch(this._nativeList, 'destroy', () => { this._nativeDestroyed = true; });
            this._watch(this._dateMenu.menu, 'open-state-changed', (_menu, open) => {
                this._syncAnimation();
                if (open)
                    this._getState();
            });
            this._render();
            this._connectService();
        } catch (error) {
            this.disable();
            throw error;
        }
    }

    _watch(object, signal, callback) {
        this._connections.push([object, object.connect(signal, callback)]);
    }

    _buildCard() {
        this._card = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'message codex-quick-chat-card',
            x_expand: true,
            reactive: true,
            accessible_name: 'Codex quick chat',
        });
        const header = new St.BoxLayout({style_class: 'codex-quick-chat-header'});
        this._face = new St.Icon({
            gicon: this._frames.get('idle'),
            style_class: 'codex-quick-chat-mascot',
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'Bit, the Codex mascot',
        });
        header.add_child(this._face);
        const heading = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'codex-quick-chat-heading',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        heading.add_child(label('Codex', 'codex-quick-chat-title'));
        this._status = label('Connecting…', 'codex-quick-chat-status');
        heading.add_child(this._status);
        header.add_child(heading);
        this._card.add_child(header);

        this._messages = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'codex-quick-chat-messages',
            x_expand: true,
        });
        this._preview = new St.ScrollView({
            style_class: 'codex-quick-chat-preview',
            x_expand: true,
            overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            child: this._messages,
            visible: false,
        });
        this._card.add_child(this._preview);
        this._error = label('', 'codex-quick-chat-error', true);
        this._error.hide();
        this._card.add_child(this._error);

        this._entry = new St.Entry({
            style_class: 'codex-quick-chat-entry',
            hint_text: 'Ask Codex…',
            can_focus: true,
            x_expand: true,
            accessible_name: `Message Codex, up to ${MAX_INPUT} characters. Enter to send.`,
        });
        this._entry.clutter_text.set_max_length(MAX_INPUT);
        this._entry.clutter_text.connect('activate', () => this._send());
        this._entry.clutter_text.connect('text-changed', () => this._syncControls());
        this._card.add_child(this._entry);

        const actions = new St.BoxLayout({style_class: 'codex-quick-chat-actions'});
        this._sendButton = button('Send', () => this._send(), 'default');
        this._stopButton = button('Stop', () => this._mutate('Stop'));
        this._newButton = button('New chat', () => this._mutate('NewChat'));
        this._openButton = button('Open in Codex', () => this._openChat());
        actions.add_child(this._sendButton);
        actions.add_child(this._stopButton);
        actions.add_child(this._newButton);
        actions.add_child(new St.Widget({x_expand: true}));
        actions.add_child(this._openButton);
        this._card.add_child(actions);

        const footer = new St.BoxLayout({style_class: 'codex-quick-chat-footer'});
        this._usage = label('Usage unavailable', 'codex-quick-chat-caption', true);
        this._usage.y_align = Clutter.ActorAlign.CENTER;
        footer.add_child(this._usage);
        this._refreshButton = new St.Button({
            style_class: 'button flat codex-quick-chat-refresh',
            child: new St.Icon({icon_name: 'view-refresh-symbolic'}),
            can_focus: true,
            accessible_name: 'Reconnect and refresh usage',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._refreshButton.connect('clicked', () => this._refresh());
        footer.add_child(this._refreshButton);
        this._card.add_child(footer);
        this._card.add_child(label('Messages use your Codex allowance', 'codex-quick-chat-caption'));
        this._count = label('', 'codex-quick-chat-caption');
        this._count.hide();
        this._card.add_child(this._count);
    }

    _connectService() {
        if (!this._enabled || this._proxyConnecting)
            return;
        this._proxyConnecting = true;
        const cancellable = this._cancellable;
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
            null, BUS_NAME, OBJECT_PATH, BUS_NAME, cancellable,
            (_source, result) => {
                let proxy;
                try {
                    proxy = Gio.DBusProxy.new_for_bus_finish(result);
                } catch (_error) {
                    if (this._enabled && this._cancellable === cancellable) {
                        this._proxyConnecting = false;
                        this._offline('Could not connect. Use Refresh to try again.');
                    }
                    return;
                }
                if (!this._enabled || this._cancellable !== cancellable)
                    return;
                this._proxyConnecting = false;
                this._proxy = proxy;
                this._watch(proxy, 'g-signal', (_proxy, _sender, name, parameters) => {
                    if (name === 'Changed') {
                        const [json] = parameters.deepUnpack();
                        this._acceptState(json);
                    }
                });
                this._watch(proxy, 'notify::g-name-owner', () => {
                    if (proxy.g_name_owner)
                        this._getState();
                    else
                        this._offline('Codex is disconnected. Use Refresh to reconnect.');
                });
                this._getState();
            });
    }

    _call(method, parameters = null) {
        const proxy = this._proxy;
        if (!proxy || !this._enabled)
            return Promise.reject(new Error('The chat service is not connected.'));
        return new Promise((resolve, reject) => {
            proxy.call(method, parameters, Gio.DBusCallFlags.NONE, 8000,
                this._cancellable, (_source, result) => {
                    try {
                        resolve(proxy.call_finish(result));
                    } catch (error) {
                        reject(error);
                    }
                });
        });
    }

    async _getState() {
        if (!this._enabled || this._gettingState || !this._proxy)
            return;
        this._gettingState = true;
        const cancellable = this._cancellable;
        const version = this._stateVersion;
        try {
            const result = await this._call('GetState');
            if (this._enabled && this._cancellable === cancellable && version === this._stateVersion)
                this._acceptState(result.deepUnpack()[0]);
        } catch (_error) {
            if (this._enabled && this._cancellable === cancellable && version === this._stateVersion)
                this._offline('Could not read Codex. Use Refresh to reconnect.');
        } finally {
            if (this._cancellable === cancellable)
                this._gettingState = false;
        }
    }

    _acceptState(json) {
        if (!this._enabled)
            return;
        let state;
        try {
            state = normalizeState(json);
        } catch (_error) {
            this._offline('Could not read the chat response. Use Refresh to reconnect.');
            return;
        }
        if (this._state.status === 'thinking' && state.status === 'idle' &&
            state.messages.some(message => message.role === 'assistant'))
            this._happyUntil = GLib.get_monotonic_time() + 2000000;
        this._state = state;
        this._stateVersion++;
        this._connecting = false;
        this._render();
    }

    _offline(message) {
        this._connecting = false;
        this._stateVersion++;
        this._state = {...this._state, status: 'offline', error: message, usage_stale: true};
        this._render();
    }

    async _send() {
        const text = this._entry.get_text();
        if (!canSend(this._state, text, Boolean(this._pending)))
            return;
        const success = await this._mutate('Send', new GLib.Variant('(s)', [text.trim()]));
        if (success && this._entry && this._entry.get_text() === text)
            this._entry.set_text('');
    }

    async _mutate(method, parameters = null) {
        if (!this._enabled || this._pending)
            return false;
        this._pending = method;
        const cancellable = this._cancellable;
        this._syncControls();
        let success = false;
        try {
            await this._call(method, parameters);
            success = true;
        } catch (error) {
            if (this._enabled && this._cancellable === cancellable) {
                Gio.DBusError.strip_remote_error(error);
                this._error.text = String(error.message).slice(0, 250);
                this._error.show();
            }
        } finally {
            if (this._enabled && this._cancellable === cancellable) {
                this._pending = null;
                this._syncControls();
                if (success)
                    this._getState();
            }
        }
        return success && this._enabled && this._cancellable === cancellable;
    }

    async _openChat() {
        const cancellable = this._cancellable;
        try {
            await this._call('OpenChat');
            if (this._enabled && this._cancellable === cancellable)
                this._dateMenu.menu.close();
        } catch (error) {
            if (this._enabled && this._cancellable === cancellable) {
                Gio.DBusError.strip_remote_error(error);
                this._error.text = String(error.message).slice(0, 250);
                this._error.show();
            }
        }
    }

    async _refresh() {
        if (!this._enabled || this._refreshing)
            return;
        if (!this._proxy) {
            this._connecting = true;
            this._render();
            this._connectService();
            return;
        }
        this._refreshing = true;
        const cancellable = this._cancellable;
        this._syncControls();
        try {
            await this._call('RefreshUsage');
            if (this._enabled && this._cancellable === cancellable)
                this._getState();
        } catch (_error) {
            if (this._enabled && this._cancellable === cancellable)
                this._offline('Could not refresh. Check Codex sign-in and try again.');
        } finally {
            if (this._enabled && this._cancellable === cancellable) {
                this._refreshing = false;
                this._syncControls();
            }
        }
    }

    _render() {
        if (!this._enabled || !this._card)
            return;
        this._status.text = statusLabel(this._state, this._connecting);
        this._error.text = this._state.error ?? '';
        this._error.visible = Boolean(this._state.error);
        this._usage.text = `${this._state.usage_label}${this._state.usage_stale ? ' · last known' : ''}`;
        const signature = JSON.stringify([this._state.messages, this._state.shortened]);
        if (signature !== this._messageSignature) {
            const adjustment = this._preview.vadjustment;
            const atBottom = adjustment.value + adjustment.page_size >= adjustment.upper - 8;
            this._messages.destroy_all_children();
            for (const message of this._state.messages) {
                const row = new St.BoxLayout({
                    orientation: Clutter.Orientation.VERTICAL,
                    style_class: 'codex-quick-chat-message',
                    x_expand: true,
                });
                row.add_child(label(message.role === 'user' ? 'You' : 'Codex', 'codex-quick-chat-role'));
                row.add_child(label(message.text, 'codex-quick-chat-body', true));
                this._messages.add_child(row);
            }
            if (this._state.shortened)
                this._messages.add_child(label('Showing a preview · open in Codex for the full chat', 'codex-quick-chat-caption', true));
            this._preview.visible = this._state.messages.length > 0;
            this._messageSignature = signature;
            if (atBottom) {
                // Allocation happens after this frame; follow streaming output
                // without taking focus away from the persistent composer.
                this._scrollToBottom = true;
                if (!this._adjustmentSignal) {
                    this._adjustmentSignal = adjustment.connect('changed', () => {
                        if (this._scrollToBottom)
                            adjustment.value = Math.max(adjustment.lower, adjustment.upper - adjustment.page_size);
                        this._scrollToBottom = false;
                    });
                    this._connections.push([adjustment, this._adjustmentSignal]);
                }
            }
        }
        this._syncControls();
        this._syncAnimation();
    }

    _syncControls() {
        if (!this._enabled || !this._sendButton)
            return;
        const thinking = this._state.status === 'thinking' || this._pending === 'Send';
        this._sendButton.visible = !thinking;
        this._stopButton.visible = thinking;
        sensitive(this._sendButton, canSend(this._state, this._entry.get_text(), Boolean(this._pending)));
        sensitive(this._stopButton, this._state.status === 'thinking' && !this._pending);
        sensitive(this._newButton, !thinking && !this._pending && this._state.status !== 'offline' &&
            (Boolean(this._state.thread_id) || this._state.status === 'error'));
        sensitive(this._openButton, Boolean(this._state.thread_id) && !this._pending && this._state.status !== 'offline');
        sensitive(this._refreshButton, !this._refreshing && !this._proxyConnecting);
        const count = characterCount(this._entry.get_text());
        this._count.text = `${count} / ${MAX_INPUT} characters`;
        this._count.visible = count >= MAX_INPUT - 200;
    }

    _syncAnimation(force = false) {
        if (!this._enabled || !this._face)
            return;
        let mode = this._state.status;
        if (mode === 'idle' && GLib.get_monotonic_time() < this._happyUntil)
            mode = 'happy';
        const animate = this._dateMenu.menu.isOpen && this._column?.visible &&
            this._settings.get_boolean('enable-animations');
        const key = `${mode}:${Boolean(animate)}`;
        if (!force && this._animationMode === key)
            return;
        this._stopAnimation();
        this._animationMode = key;
        const frame = mode === 'thinking' ? 'thinking-left'
            : mode === 'error' || mode === 'offline' ? 'error'
                : mode === 'happy' ? 'happy' : 'idle';
        this._face.gicon = this._frames.get(frame);
        if (animate && ['idle', 'thinking', 'happy'].includes(mode))
            this._scheduleFrame(mode, frame);
    }

    _scheduleFrame(mode, frame) {
        const delay = mode === 'thinking' ? 480 : mode === 'happy' ? 2000 : frame === 'blink' ? 140 : 4500;
        this._animationSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._animationSource = 0;
            if (!this._enabled || !this._dateMenu.menu.isOpen)
                return GLib.SOURCE_REMOVE;
            if (mode === 'happy') {
                this._happyUntil = 0;
                this._syncAnimation(true);
            } else {
                const next = mode === 'thinking'
                    ? frame === 'thinking-left' ? 'thinking-right' : 'thinking-left'
                    : frame === 'idle' ? 'blink' : 'idle';
                this._face.gicon = this._frames.get(next);
                this._scheduleFrame(mode, next);
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _stopAnimation() {
        if (this._animationSource)
            GLib.Source.remove(this._animationSource);
        this._animationSource = 0;
    }

    disable() {
        this._enabled = false;
        this._cancellable?.cancel();
        this._stopAnimation();
        for (const [object, id] of this._connections ?? []) {
            try {
                object.disconnect(id);
            } catch (_error) {
                // The Shell may have already destroyed a native actor.
            }
        }
        this._connections = [];
        const focus = global.stage.get_key_focus();
        if (focus && this._card?.contains(focus))
            this._dateMenu?.grab_key_focus();
        if (this._column && !this._nativeDestroyed && this._nativeList?.get_parent() === this._column) {
            const parent = this._column.get_parent();
            const position = parent?.get_children().indexOf(this._column) ?? -1;
            this._column.remove_child(this._nativeList);
            if (parent)
                parent.insert_child_at_index(this._nativeList, position);
        }
        if (this._column)
            this._column.destroy();
        else
            this._card?.destroy();
        this._column = null;
        this._card = null;
        this._entry = null;
        this._dateMenu = null;
        this._nativeList = null;
        this._proxy = null;
        this._settings = null;
        this._frames = null;
        this._cancellable = null;
        this._proxyConnecting = false;
        this._gettingState = false;
        this._adjustmentSignal = 0;
    }
}
