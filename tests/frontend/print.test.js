'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    getExt,
    isPdf,
    bookletPageOrder,
    validatePrintOptions,
    buildPrintPayload,
    escapeHtml,
} = require(path.join(__dirname, '..', '..', 'static', 'print.js'));

// --------------------------------------------------------------------------
// getExt / isPdf
// --------------------------------------------------------------------------
test('isPdf is case-insensitive and dot-anchored', () => {
    assert.equal(isPdf('report.pdf'), true);
    assert.equal(isPdf('report.PDF'), true);
    assert.equal(isPdf('report.txt'), false);
    assert.equal(getExt('a.PDF'), '.pdf');
});

// --------------------------------------------------------------------------
// bookletPageOrder — parity with the Python backend expectations
// --------------------------------------------------------------------------
test('bookletPageOrder matches backend sequences', () => {
    assert.deepEqual(bookletPageOrder(4), [[3, 0], [1, 2]]);
    assert.deepEqual(bookletPageOrder(8), [[7, 0], [1, 6], [5, 2], [3, 4]]);
    assert.deepEqual(bookletPageOrder(12), [
        [11, 0], [1, 10], [9, 2], [3, 8], [7, 4], [5, 6],
    ]);
});

test('bookletPageOrder pads 5 -> 8 with nulls for blanks', () => {
    assert.deepEqual(bookletPageOrder(5), [
        [null, 0], [1, null], [null, 2], [3, 4],
    ]);
});

test('bookletPageOrder sheet count is padded/2', () => {
    const cases = [[1, 2], [2, 2], [3, 2], [4, 2], [5, 4], [7, 4], [8, 4], [9, 6], [12, 6], [13, 8]];
    for (const [count, sheets] of cases) {
        assert.equal(bookletPageOrder(count).length, sheets, `count=${count}`);
    }
});

test('bookletPageOrder covers every real page exactly once', () => {
    for (const count of [1, 2, 3, 5, 7, 10]) {
        const seen = [];
        for (const [l, r] of bookletPageOrder(count)) {
            for (const v of [l, r]) if (v !== null) seen.push(v);
        }
        seen.sort((a, b) => a - b);
        assert.deepEqual(seen, Array.from({ length: count }, (_, i) => i), `count=${count}`);
    }
});

test('bookletPageOrder rejects non-positive counts', () => {
    for (const bad of [0, -1, null, undefined]) {
        assert.throws(() => bookletPageOrder(bad));
    }
});

// --------------------------------------------------------------------------
// validatePrintOptions
// --------------------------------------------------------------------------
test('validatePrintOptions accepts nup 2/4 and booklet', () => {
    assert.equal(validatePrintOptions({ operation: 'nup', n: '2' }).valid, true);
    assert.equal(validatePrintOptions({ operation: 'nup', n: '4' }).valid, true);
    assert.equal(validatePrintOptions({ operation: 'booklet' }).valid, true);
});

test('validatePrintOptions rejects bad n and unknown op', () => {
    assert.equal(validatePrintOptions({ operation: 'nup', n: '3' }).valid, false);
    assert.equal(validatePrintOptions({ operation: 'nup', n: '' }).valid, false);
    assert.equal(validatePrintOptions({ operation: 'nup', n: '1' }).valid, false);
    assert.equal(validatePrintOptions({ operation: 'nope' }).valid, false);
});

// --------------------------------------------------------------------------
// buildPrintPayload
// --------------------------------------------------------------------------
test('buildPrintPayload: nup carries n', () => {
    const ok = buildPrintPayload({ operation: 'nup', n: '4' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'nup', n: '4' });
});

test('buildPrintPayload: booklet omits n', () => {
    const ok = buildPrintPayload({ operation: 'booklet' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'booklet' });
});

test('buildPrintPayload: propagates validation errors', () => {
    assert.equal(buildPrintPayload({ operation: 'nup', n: '5' }).ok, false);
    assert.equal(buildPrintPayload({ operation: 'nope' }).ok, false);
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml escapes special characters', () => {
    assert.equal(escapeHtml('<a & "b" \'c\'>'), '&lt;a &amp; &quot;b&quot; &#39;c&#39;&gt;');
});
