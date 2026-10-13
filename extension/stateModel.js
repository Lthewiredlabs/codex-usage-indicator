// Pure, bounded display rules. Backend strings are always rendered as plain text.
export const MAX_INPUT = 4000;
export const MAX_STATE_BYTES = 262144;
const STATUSES = new Set(['idle', 'thinking', 'error', 'offline']);

export function characterCount(text) {
    return Array.from(text).length;
}

function boundedText(value, length) {
    return typeof value === 'string' ? Array.from(value).slice(0, length).join('') : '';
}

export function normalizeState(json) {
    if (typeof json !== 'string' || json.length > MAX_STATE_BYTES)
        throw new Error('Invalid or oversized state');
    const value = JSON.parse(json);
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        !STATUSES.has(value.status) || !Array.isArray(value.messages))
        throw new Error('Invalid state shape');

    const messages = value.messages.slice(-4).filter(message =>
        message && ['user', 'assistant'].includes(message.role) &&
        typeof message.text === 'string').map(message => {
        const text = boundedText(message.text, 3000);
        return {
            role: message.role,
            text,
            shortened: text.length < message.text.length,
        };
    });
    return {
        status: value.status,
        thread_id: typeof value.thread_id === 'string' ? boundedText(value.thread_id, 200) : null,
        messages,
        error: boundedText(value.error, 350) || null,
        usage_label: boundedText(value.usage_label, 180) || 'Usage unavailable',
        usage_stale: value.usage_stale !== false,
        shortened: value.messages.length > messages.length || messages.some(message => message.shortened),
    };
}

export function emptyState() {
    return {
        status: 'offline', thread_id: null, messages: [], error: null,
        usage_label: 'Usage unavailable', usage_stale: true, shortened: false,
    };
}

export function canSend(state, text, pending = false) {
    return !pending && state.status !== 'offline' && state.status !== 'thinking' &&
        text.trim().length > 0 && characterCount(text) <= MAX_INPUT;
}

export function statusLabel(state, connecting = false) {
    if (connecting)
        return 'Connecting…';
    if (state.status === 'thinking')
        return 'Thinking…';
    if (state.status === 'offline')
        return 'Not connected';
    if (state.status === 'error')
        return 'Needs attention';
    return state.messages.some(message => message.role === 'assistant')
        ? 'Ready for your next question' : 'Ready to chat';
}
