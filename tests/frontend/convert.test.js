'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    getExt,
    buildTargetLabel,
    formatMatrix,
    validateConvertState,
    escapeHtml,
} = require(path.join(__dirname, '..', '..', 'static', 'convert.js'));

// --------------------------------------------------------------------------
// getExt
// --------------------------------------------------------------------------
test('getExt returns lowercased extension', () => {
    assert.equal(getExt('a.CSV'), '.csv');
    assert.equal(getExt('archive.tar.gz'), '.gz');
    assert.equal(getExt('noext'), '');
    assert.equal(getExt('report.XLSX'), '.xlsx');
});

// --------------------------------------------------------------------------
// buildTargetLabel
// --------------------------------------------------------------------------
test('buildTargetLabel uppercases and strips the dot', () => {
    assert.equal(buildTargetLabel('.png'), 'PNG');
    assert.equal(buildTargetLabel('.xlsx'), 'XLSX');
    assert.equal(buildTargetLabel('json'), 'JSON');
    assert.equal(buildTargetLabel(''), '');
});

// --------------------------------------------------------------------------
// formatMatrix
// --------------------------------------------------------------------------
test('formatMatrix renders sorted SOURCE -> targets lines', () => {
    const lines = formatMatrix({
        '.csv': ['.xlsx', '.json'],
        '.png': ['.jpg'],
    });
    assert.deepEqual(lines, [
        'CSV → XLSX, JSON',
        'PNG → JPG',
    ]);
});

test('formatMatrix skips sources with no targets and handles junk', () => {
    assert.deepEqual(formatMatrix({ '.csv': [] }), []);
    assert.deepEqual(formatMatrix({}), []);
    assert.deepEqual(formatMatrix(null), []);
    assert.deepEqual(formatMatrix('nope'), []);
});

test('formatMatrix sorts sources alphabetically', () => {
    const lines = formatMatrix({
        '.zzz': ['.a'],
        '.aaa': ['.b'],
    });
    assert.deepEqual(lines, ['AAA → B', 'ZZZ → A']);
});

// --------------------------------------------------------------------------
// validateConvertState
// --------------------------------------------------------------------------
test('validateConvertState accepts a valid selection', () => {
    const r = validateConvertState({
        ext: 'data.csv', target: '.xlsx', targets: ['.xlsx', '.json'],
    });
    assert.equal(r.valid, true);
});

test('validateConvertState rejects when no file/extension', () => {
    assert.equal(validateConvertState({ ext: '', target: '.xlsx', targets: ['.xlsx'] }).valid, false);
    assert.equal(validateConvertState({ ext: 'noext', target: '.xlsx', targets: ['.xlsx'] }).valid, false);
});

test('validateConvertState rejects when no targets available', () => {
    assert.equal(validateConvertState({ ext: 'a.csv', target: '', targets: [] }).valid, false);
});

test('validateConvertState rejects missing target choice', () => {
    assert.equal(validateConvertState({ ext: 'a.csv', target: '', targets: ['.xlsx'] }).valid, false);
});

test('validateConvertState rejects target not in the list', () => {
    const r = validateConvertState({ ext: 'a.csv', target: '.png', targets: ['.xlsx'] });
    assert.equal(r.valid, false);
    assert.match(r.error, /png/);
});

test('validateConvertState is case-insensitive on the target', () => {
    assert.equal(
        validateConvertState({ ext: 'a.csv', target: '.XLSX', targets: ['.xlsx'] }).valid,
        true
    );
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml escapes special characters', () => {
    assert.equal(escapeHtml('<a & "b" \'c\'>'), '&lt;a &amp; &quot;b&quot; &#39;c&#39;&gt;');
});

// --------------------------------------------------------------------------
// targetOptions: which extra choices a target needs (round prelaunch-fixes)
// --------------------------------------------------------------------------
const { targetOptions } = require(path.join(__dirname, '..', '..', 'static', 'convert.js'));

test('targetOptions: paper for page-laying targets, OCR for image to text', () => {
    assert.deepEqual(targetOptions('.pdf', ['.pdf', '.png'], []), { paper: true, ocr: false });
    assert.deepEqual(targetOptions('.txt', ['.pdf'], ['.txt']), { paper: false, ocr: true });
    assert.deepEqual(targetOptions('.jpg', ['.pdf'], ['.txt']), { paper: false, ocr: false });
});

test('targetOptions: no target or missing lists means neither', () => {
    assert.deepEqual(targetOptions('', ['.pdf'], ['.txt']), { paper: false, ocr: false });
    assert.deepEqual(targetOptions('.pdf', undefined, null), { paper: false, ocr: false });
});
