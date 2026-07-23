'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    getExt,
    isPdf,
    validateExportOptions,
    buildExportPayload,
    escapeHtml,
} = require(path.join(__dirname, '..', '..', 'static', 'export.js'));

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
// validateExportOptions
// --------------------------------------------------------------------------
test('validateExportOptions accepts valid image options', () => {
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '150' }).valid, true);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'jpg', dpi: '30' }).valid, true);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'jpg', dpi: '600' }).valid, true);
});

test('validateExportOptions accepts text with no extra options', () => {
    assert.equal(validateExportOptions({ operation: 'text' }).valid, true);
});

test('validateExportOptions rejects unknown operation', () => {
    assert.equal(validateExportOptions({ operation: 'nope' }).valid, false);
    assert.equal(validateExportOptions({}).valid, false);
});

test('validateExportOptions rejects bad format', () => {
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'gif', dpi: '150' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: '', dpi: '150' }).valid, false);
});

test('validateExportOptions rejects out-of-range and non-numeric dpi', () => {
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '29' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '601' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '0' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: 'abc' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '' }).valid, false);
    assert.equal(validateExportOptions({ operation: 'images', fmt: 'png', dpi: '1.5' }).valid, false);
});

// --------------------------------------------------------------------------
// buildExportPayload
// --------------------------------------------------------------------------
test('buildExportPayload: images carries fmt + normalized dpi', () => {
    const ok = buildExportPayload({ operation: 'images', fmt: 'PNG', dpi: '150' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'images', fmt: 'png', dpi: '150' });
});

test('buildExportPayload: text has only operation', () => {
    const ok = buildExportPayload({ operation: 'text' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'text' });
});

test('buildExportPayload: rejects invalid options', () => {
    assert.equal(buildExportPayload({ operation: 'images', fmt: 'gif', dpi: '150' }).ok, false);
    assert.equal(buildExportPayload({ operation: 'images', fmt: 'png', dpi: '9000' }).ok, false);
    assert.equal(buildExportPayload({ operation: 'bogus' }).ok, false);
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml escapes special characters', () => {
    assert.equal(escapeHtml('<a & "b" \'c\'>'), '&lt;a &amp; &quot;b&quot; &#39;c&#39;&gt;');
});
