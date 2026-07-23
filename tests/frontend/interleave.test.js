'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    buildModeOptions,
    canMerge,
    MERGE_MODES,
    buildMergeOptions,
} = require(path.join(__dirname, '..', '..', 'static', 'app.js'));

// --------------------------------------------------------------------------
// buildModeOptions
// --------------------------------------------------------------------------
test('buildModeOptions: defaults to standard + reverse_second true', () => {
    assert.deepEqual(buildModeOptions({}), {
        mode: 'standard',
        reverse_second: 'true',
    });
});

test('buildModeOptions: undefined input behaves like empty object', () => {
    assert.deepEqual(buildModeOptions(undefined), buildModeOptions({}));
});

test('buildModeOptions: keeps a valid mode', () => {
    for (const mode of MERGE_MODES) {
        assert.equal(buildModeOptions({ mode }).mode, mode);
    }
});

test('buildModeOptions: falls back to standard for an unknown mode', () => {
    assert.equal(buildModeOptions({ mode: 'sideways' }).mode, 'standard');
    assert.equal(buildModeOptions({ mode: '' }).mode, 'standard');
});

test('buildModeOptions: serializes reverse_second as "true"/"false"', () => {
    assert.equal(buildModeOptions({ reverseSecond: false }).reverse_second, 'false');
    assert.equal(buildModeOptions({ reverseSecond: true }).reverse_second, 'true');
});

test('buildModeOptions: reverse_second defaults to true when omitted/null', () => {
    assert.equal(buildModeOptions({ mode: 'interleave' }).reverse_second, 'true');
    assert.equal(
        buildModeOptions({ reverseSecond: null }).reverse_second,
        'true'
    );
});

test('buildModeOptions: all values are strings (FormData-ready)', () => {
    const out = buildModeOptions({ mode: 'interleave', reverseSecond: false });
    for (const v of Object.values(out)) {
        assert.equal(typeof v, 'string');
    }
});

test('buildModeOptions: always returns exactly mode + reverse_second', () => {
    const keys = Object.keys(buildModeOptions({ mode: 'interleave' })).sort();
    assert.deepEqual(keys, ['mode', 'reverse_second']);
});

// --------------------------------------------------------------------------
// canMerge
// --------------------------------------------------------------------------
test('canMerge: standard mode needs at least one file', () => {
    assert.equal(canMerge({ mode: 'standard', fileCount: 0 }), false);
    assert.equal(canMerge({ mode: 'standard', fileCount: 1 }), true);
    assert.equal(canMerge({ mode: 'standard', fileCount: 5 }), true);
});

test('canMerge: interleave mode needs exactly two files', () => {
    assert.equal(canMerge({ mode: 'interleave', fileCount: 0 }), false);
    assert.equal(canMerge({ mode: 'interleave', fileCount: 1 }), false);
    assert.equal(canMerge({ mode: 'interleave', fileCount: 2 }), true);
    assert.equal(canMerge({ mode: 'interleave', fileCount: 3 }), false);
});

test('canMerge: unknown/absent mode is treated as standard', () => {
    assert.equal(canMerge({ fileCount: 1 }), true);
    assert.equal(canMerge({ fileCount: 0 }), false);
    assert.equal(canMerge({ mode: 'bogus', fileCount: 1 }), true);
});

test('canMerge: missing/invalid fileCount counts as zero', () => {
    assert.equal(canMerge({ mode: 'standard' }), false);
    assert.equal(canMerge({}), false);
    assert.equal(canMerge(undefined), false);
    assert.equal(canMerge({ mode: 'standard', fileCount: 'x' }), false);
});

// --------------------------------------------------------------------------
// Interaction: mode options are independent of buildMergeOptions' shape
// --------------------------------------------------------------------------
test('buildMergeOptions keeps its five-key shape (mode lives separately)', () => {
    const keys = Object.keys(buildMergeOptions({})).sort();
    assert.deepEqual(keys, [
        'blank_pages',
        'bookmarks',
        'number_position',
        'page_numbers',
        'start_number',
    ]);
});
