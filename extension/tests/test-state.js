import {MAX_INPUT, canSend, characterCount, emptyState, normalizeState, statusLabel} from '../stateModel.js';

let checks = 0;
function assert(value, message) {
    if (!value)
        throw new Error(message);
    checks++;
}

function rejects(value) {
    try {
        normalizeState(value);
    } catch (_error) {
        return true;
    }
    return false;
}

const valid = {status: 'idle', messages: [], usage_label: '5h 85% · Week 72%', usage_stale: false};
const state = normalizeState(JSON.stringify(valid));
assert(state.usage_label === valid.usage_label, 'Usage remains the provider label');
assert(!state.usage_stale, 'Fresh usage is preserved');
assert(statusLabel(state) === 'Ready to chat', 'Empty conversation is ready');
assert(statusLabel(state, true) === 'Connecting…', 'Connecting is separate from offline');
assert(canSend(state, 'A question'), 'Idle accepts an explicit question');
assert(!canSend(state, '  '), 'Blank input is rejected');
assert(!canSend(state, 'Question', true), 'Pending Send cannot duplicate');
assert(!canSend({...state, status: 'thinking'}, 'Question'), 'Thinking cannot duplicate');
assert(!canSend(emptyState(), 'Question'), 'Offline cannot send');
assert(characterCount('😀'.repeat(MAX_INPUT)) === MAX_INPUT, 'Unicode is counted as characters');
assert(canSend(state, '😀'.repeat(MAX_INPUT)), '4000 Unicode characters are accepted');
assert(!canSend(state, 'x'.repeat(MAX_INPUT + 1)), 'Input over the limit is rejected');
for (const bad of ['null', '[]', '{', '{}', JSON.stringify({...valid, status: 'unexpected'}), 'x'.repeat(262145)])
    assert(rejects(bad), 'Unknown or oversized state is rejected');

const mixed = normalizeState(JSON.stringify({...valid, messages: [null, {role: 'system', text: 'hidden'},
    {role: 'assistant', text: '<b>literal plain text</b>'}, {role: 'user', text: 8}]}));
assert(mixed.messages.length === 1, 'Malformed and unsupported messages are ignored');
assert(mixed.messages[0].text === '<b>literal plain text</b>', 'Markup remains literal text');
assert(statusLabel(mixed) === 'Ready for your next question', 'A response shows continuation state');
const long = normalizeState(JSON.stringify({...valid, messages: Array.from({length: 9}, () =>
    ({role: 'assistant', text: '😀'.repeat(5000)}))}));
assert(long.messages.length === 4, 'Only the last four messages are previewed');
assert(characterCount(long.messages[0].text) === 3000, 'Preview size is bounded without splitting Unicode');
assert(long.shortened, 'Shortened preview is disclosed');
assert(normalizeState(JSON.stringify({status: 'error', messages: []})).usage_stale, 'Missing usage is unknown');
print(`PASS: ${checks} state-model checks`);
