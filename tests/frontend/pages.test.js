'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    getExt,
    isPdf,
    validateRangeSpec,
    buildRunPayload,
    escapeHtml,
} = require(path.join(__dirname, '..', '..', 'static', 'pages.js'));

// --------------------------------------------------------------------------
// getExt / isPdf
// --------------------------------------------------------------------------
test('getExt returns lowercased extension', () => {
    assert.equal(getExt('a.PDF'), '.pdf');
    assert.equal(getExt('doc.tar.gz'), '.gz');
    assert.equal(getExt('noext'), '');
});

test('isPdf is case-insensitive and dot-anchored', () => {
    assert.equal(isPdf('report.pdf'), true);
    assert.equal(isPdf('report.PDF'), true);
    assert.equal(isPdf('report.txt'), false);
    assert.equal(isPdf('pdf'), false);
});

// --------------------------------------------------------------------------
// validateRangeSpec
// --------------------------------------------------------------------------
test('validateRangeSpec accepts valid specs', () => {
    assert.equal(validateRangeSpec('1').valid, true);
    assert.equal(validateRangeSpec('1-3').valid, true);
    assert.equal(validateRangeSpec('1-3,5,8-10').valid, true);
    assert.equal(validateRangeSpec('  1 - 3 , 5 ').valid, true);
});

test('validateRangeSpec rejects empty', () => {
    assert.equal(validateRangeSpec('').valid, false);
    assert.equal(validateRangeSpec('   ').valid, false);
    assert.equal(validateRangeSpec(null).valid, false);
    assert.equal(validateRangeSpec(undefined).valid, false);
});

test('validateRangeSpec rejects garbage', () => {
    for (const bad of ['abc', '1-', '-3', '1-2-3', '1,,3', '1.5', '1-a']) {
        assert.equal(validateRangeSpec(bad).valid, false, `expected ${bad} invalid`);
    }
});

test('validateRangeSpec rejects zero and backwards ranges', () => {
    assert.equal(validateRangeSpec('0').valid, false);
    assert.equal(validateRangeSpec('0-3').valid, false);
    assert.equal(validateRangeSpec('5-2').valid, false);
});

// --------------------------------------------------------------------------
// buildRunPayload
// --------------------------------------------------------------------------
test('buildRunPayload: extract needs valid ranges', () => {
    const ok = buildRunPayload({ operation: 'extract', ranges: '1-3,5' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'extract', ranges: '1-3,5' });

    const bad = buildRunPayload({ operation: 'extract', ranges: 'x' });
    assert.equal(bad.ok, false);
});

test('buildRunPayload: remove trims ranges', () => {
    const ok = buildRunPayload({ operation: 'remove', ranges: ' 2 ' });
    assert.equal(ok.ok, true);
    assert.equal(ok.fields.ranges, '2');
});

test('buildRunPayload: rotate requires valid angle', () => {
    const ok = buildRunPayload({ operation: 'rotate', angle: '90' });
    assert.equal(ok.ok, true);
    assert.equal(ok.fields.angle, '90');
    assert.equal('ranges' in ok.fields, false);

    const bad = buildRunPayload({ operation: 'rotate', angle: '45' });
    assert.equal(bad.ok, false);
});

test('buildRunPayload: rotate with optional ranges', () => {
    const ok = buildRunPayload({ operation: 'rotate', angle: '180', ranges: '1-2' });
    assert.equal(ok.ok, true);
    assert.equal(ok.fields.ranges, '1-2');

    const bad = buildRunPayload({ operation: 'rotate', angle: '180', ranges: 'bad' });
    assert.equal(bad.ok, false);
});

test('buildRunPayload: split every_n needs positive int', () => {
    const ok = buildRunPayload({ operation: 'split', splitMode: 'every_n', splitValue: '3' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'split', split_mode: 'every_n', split_value: '3' });

    assert.equal(buildRunPayload({ operation: 'split', splitMode: 'every_n', splitValue: '0' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'split', splitMode: 'every_n', splitValue: 'x' }).ok, false);
});

test('buildRunPayload: split ranges normalizes semicolon list', () => {
    const ok = buildRunPayload({ operation: 'split', splitMode: 'ranges', splitValue: '1-3; 4-6 ;7-10' });
    assert.equal(ok.ok, true);
    assert.equal(ok.fields.split_value, '1-3;4-6;7-10');

    assert.equal(buildRunPayload({ operation: 'split', splitMode: 'ranges', splitValue: '' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'split', splitMode: 'ranges', splitValue: '1-3;bad' }).ok, false);
});

test('buildRunPayload: rejects unknown operation and split mode', () => {
    assert.equal(buildRunPayload({ operation: 'nope' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'split', splitMode: 'nope', splitValue: '1' }).ok, false);
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml escapes special characters', () => {
    assert.equal(escapeHtml('<a & "b" \'c\'>'), '&lt;a &amp; &quot;b&quot; &#39;c&#39;&gt;');
});
