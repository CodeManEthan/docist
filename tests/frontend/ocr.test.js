'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    validateLanguage,
    buildRunPayload,
} = require(path.join(__dirname, '..', '..', 'static', 'pages.js'));

// --------------------------------------------------------------------------
// validateLanguage
// --------------------------------------------------------------------------
test('validateLanguage defaults empty to eng', () => {
    for (const v of ['', '   ', null, undefined]) {
        const r = validateLanguage(v);
        assert.equal(r.valid, true);
        assert.equal(r.value, 'eng');
    }
});

test('validateLanguage accepts single and joined codes', () => {
    assert.deepEqual(validateLanguage('eng'), { valid: true, value: 'eng' });
    assert.deepEqual(validateLanguage('eng+deu'), { valid: true, value: 'eng+deu' });
    assert.deepEqual(validateLanguage('  eng + deu '), { valid: true, value: 'eng+deu' });
    assert.equal(validateLanguage('chi_sim').valid, true);
});

test('validateLanguage rejects malformed codes', () => {
    for (const bad of ['en g', 'eng;deu', 'eng!', 'eng+', '+eng', 'e/g']) {
        assert.equal(validateLanguage(bad).valid, false, `expected ${bad} invalid`);
    }
});

// --------------------------------------------------------------------------
// buildRunPayload — ocr
// --------------------------------------------------------------------------
test('buildRunPayload: ocr defaults language and omits false flags', () => {
    const r = buildRunPayload({ operation: 'ocr' });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, { operation: 'ocr', language: 'eng' });
});

test('buildRunPayload: ocr passes language and boolean flags', () => {
    const r = buildRunPayload({
        operation: 'ocr', language: 'eng+deu', deskew: true, force: true,
    });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, {
        operation: 'ocr', language: 'eng+deu', deskew: 'true', force: 'true',
    });
});

test('buildRunPayload: ocr sends only the flags that are set', () => {
    const r = buildRunPayload({ operation: 'ocr', deskew: true, force: false });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, { operation: 'ocr', language: 'eng', deskew: 'true' });
});

test('buildRunPayload: ocr rejects a malformed language', () => {
    const r = buildRunPayload({ operation: 'ocr', language: 'bad lang' });
    assert.equal(r.ok, false);
    assert.match(r.error, /Tesseract/);
});
