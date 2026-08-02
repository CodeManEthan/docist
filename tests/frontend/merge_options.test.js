'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    buildMergeOptions,
    NUMBER_POSITIONS,
} = require(path.join(__dirname, '..', '..', 'static', 'app.js'));

// --------------------------------------------------------------------------
// buildMergeOptions
// --------------------------------------------------------------------------
test('buildMergeOptions: defaults reproduce the backend defaults', () => {
    assert.deepEqual(buildMergeOptions({}), {
        page_numbers: 'true',
        blank_pages: 'false',
        bookmarks: 'true',
        number_position: 'bottom-right',
        start_number: '1',
    });
});

test('buildMergeOptions: undefined input behaves like empty object', () => {
    assert.deepEqual(buildMergeOptions(undefined), buildMergeOptions({}));
});

test('buildMergeOptions: serializes booleans as "true"/"false" strings', () => {
    const out = buildMergeOptions({
        pageNumbers: false,
        blankPages: false,
        bookmarks: false,
    });
    assert.equal(out.page_numbers, 'false');
    assert.equal(out.blank_pages, 'false');
    assert.equal(out.bookmarks, 'false');
});

test('buildMergeOptions: all values are strings (FormData-ready)', () => {
    const out = buildMergeOptions({ pageNumbers: true, startNumber: 7 });
    for (const v of Object.values(out)) {
        assert.equal(typeof v, 'string');
    }
});

test('buildMergeOptions: keeps a valid position', () => {
    for (const pos of NUMBER_POSITIONS) {
        assert.equal(
            buildMergeOptions({ numberPosition: pos }).number_position,
            pos
        );
    }
});

test('buildMergeOptions: falls back to bottom-right for an unknown position', () => {
    assert.equal(
        buildMergeOptions({ numberPosition: 'top-left' }).number_position,
        'bottom-right'
    );
    assert.equal(
        buildMergeOptions({ numberPosition: '' }).number_position,
        'bottom-right'
    );
});

test('buildMergeOptions: coerces start number to a positive integer string', () => {
    assert.equal(buildMergeOptions({ startNumber: 5 }).start_number, '5');
    assert.equal(buildMergeOptions({ startNumber: '12' }).start_number, '12');
    assert.equal(buildMergeOptions({ startNumber: '3.9' }).start_number, '3');
});

test('buildMergeOptions: clamps bad start numbers to 1', () => {
    assert.equal(buildMergeOptions({ startNumber: 0 }).start_number, '1');
    assert.equal(buildMergeOptions({ startNumber: -4 }).start_number, '1');
    assert.equal(buildMergeOptions({ startNumber: 'abc' }).start_number, '1');
    assert.equal(buildMergeOptions({ startNumber: '' }).start_number, '1');
});

test('buildMergeOptions: always returns the full set of keys', () => {
    const keys = Object.keys(buildMergeOptions({ pageNumbers: false })).sort();
    assert.deepEqual(keys, [
        'blank_pages',
        'bookmarks',
        'number_position',
        'page_numbers',
        'start_number',
    ]);
});
