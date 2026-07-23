'use strict';

// Pure-helper tests for the OCR fallback added to the Export page. These cover
// the new `normalizeLanguage` helper and the OCR-aware branch of
// `buildExportPayload`. The pre-existing helpers are covered by export.test.js,
// which must keep passing unmodified.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    validateExportOptions,
    buildExportPayload,
    normalizeLanguage,
} = require(path.join(__dirname, '..', '..', 'static', 'export.js'));

// --------------------------------------------------------------------------
// normalizeLanguage
// --------------------------------------------------------------------------
test('normalizeLanguage trims, lowercases and defaults to eng', () => {
    assert.equal(normalizeLanguage('eng'), 'eng');
    assert.equal(normalizeLanguage('  DEU  '), 'deu');
    assert.equal(normalizeLanguage('Fra'), 'fra');
    assert.equal(normalizeLanguage(''), 'eng');
    assert.equal(normalizeLanguage('   '), 'eng');
    assert.equal(normalizeLanguage(null), 'eng');
    assert.equal(normalizeLanguage(undefined), 'eng');
});

// --------------------------------------------------------------------------
// buildExportPayload: OCR branch for text
// --------------------------------------------------------------------------
test('buildExportPayload: text without OCR stays minimal', () => {
    const ok = buildExportPayload({ operation: 'text' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'text' });
});

test('buildExportPayload: text with ocrFallback off stays minimal', () => {
    const ok = buildExportPayload({ operation: 'text', ocrFallback: false, language: 'eng' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'text' });
});

test('buildExportPayload: text with OCR carries flag + normalized language', () => {
    const ok = buildExportPayload({ operation: 'text', ocrFallback: true, language: '  DEU ' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'text', ocr_fallback: 'true', language: 'deu' });
});

test('buildExportPayload: OCR text with blank language defaults to eng', () => {
    const ok = buildExportPayload({ operation: 'text', ocrFallback: true, language: '' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'text', ocr_fallback: 'true', language: 'eng' });
});

test('buildExportPayload: images ignore OCR fields entirely', () => {
    const ok = buildExportPayload({ operation: 'images', fmt: 'png', dpi: '150', ocrFallback: true, language: 'deu' });
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.fields, { operation: 'images', fmt: 'png', dpi: '150' });
});

// --------------------------------------------------------------------------
// validateExportOptions: text with OCR options is still valid
// --------------------------------------------------------------------------
test('validateExportOptions accepts text with OCR options', () => {
    assert.equal(validateExportOptions({ operation: 'text', ocrFallback: true, language: 'eng' }).valid, true);
});
