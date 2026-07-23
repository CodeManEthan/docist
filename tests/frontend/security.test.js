'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    OPERATIONS,
    POSITIONS,
    getExt,
    isPdf,
    validateWatermarkOptions,
    validateSecurityState,
    buildSecurityPayload,
    escapeHtml,
} = require(path.join(__dirname, '..', '..', 'static', 'security.js'));

const baseWatermark = {
    file: {},
    operation: 'watermark',
    text: 'Confidential',
    position: 'center',
    opacity: '0.15',
    fontSize: '48',
    rotation: '45',
    password: '',
};

// --------------------------------------------------------------------------
// constants
// --------------------------------------------------------------------------
test('OPERATIONS / POSITIONS expose the expected values', () => {
    assert.deepEqual(OPERATIONS, ['watermark', 'protect', 'unlock']);
    assert.deepEqual(POSITIONS, ['center', 'header', 'footer']);
});

// --------------------------------------------------------------------------
// getExt / isPdf
// --------------------------------------------------------------------------
test('getExt returns lowercased extension', () => {
    assert.equal(getExt('a.PDF'), '.pdf');
    assert.equal(getExt('noext'), '');
});

test('isPdf only accepts .pdf', () => {
    assert.equal(isPdf('doc.pdf'), true);
    assert.equal(isPdf('DOC.PDF'), true);
    assert.equal(isPdf('doc.txt'), false);
    assert.equal(isPdf('doc'), false);
});

// --------------------------------------------------------------------------
// validateWatermarkOptions
// --------------------------------------------------------------------------
test('validateWatermarkOptions: valid state passes', () => {
    const r = validateWatermarkOptions(baseWatermark);
    assert.equal(r.valid, true);
    assert.equal(r.error, null);
});

test('validateWatermarkOptions: empty text fails', () => {
    const r = validateWatermarkOptions({ ...baseWatermark, text: '   ' });
    assert.equal(r.valid, false);
    assert.match(r.error, /text/i);
});

test('validateWatermarkOptions: bad position fails', () => {
    const r = validateWatermarkOptions({ ...baseWatermark, position: 'sideways' });
    assert.equal(r.valid, false);
});

test('validateWatermarkOptions: opacity out of range fails', () => {
    assert.equal(validateWatermarkOptions({ ...baseWatermark, opacity: '1.5' }).valid, false);
    assert.equal(validateWatermarkOptions({ ...baseWatermark, opacity: '-0.2' }).valid, false);
    assert.equal(validateWatermarkOptions({ ...baseWatermark, opacity: 'abc' }).valid, false);
});

test('validateWatermarkOptions: non-positive font size fails', () => {
    assert.equal(validateWatermarkOptions({ ...baseWatermark, fontSize: '0' }).valid, false);
    assert.equal(validateWatermarkOptions({ ...baseWatermark, fontSize: '-4' }).valid, false);
});

test('validateWatermarkOptions: non-numeric rotation fails', () => {
    assert.equal(validateWatermarkOptions({ ...baseWatermark, rotation: 'spin' }).valid, false);
});

// --------------------------------------------------------------------------
// validateSecurityState
// --------------------------------------------------------------------------
test('validateSecurityState: no file fails', () => {
    const r = validateSecurityState({ ...baseWatermark, file: null });
    assert.equal(r.valid, false);
    assert.match(r.error, /pdf/i);
});

test('validateSecurityState: protect requires a password', () => {
    const noPw = { ...baseWatermark, operation: 'protect', password: '' };
    assert.equal(validateSecurityState(noPw).valid, false);
    const withPw = { ...noPw, password: 'x' };
    assert.equal(validateSecurityState(withPw).valid, true);
});

test('validateSecurityState: unlock requires a password', () => {
    const noPw = { ...baseWatermark, operation: 'unlock', password: '' };
    assert.equal(validateSecurityState(noPw).valid, false);
});

test('validateSecurityState: unknown operation fails', () => {
    assert.equal(validateSecurityState({ ...baseWatermark, operation: 'bogus' }).valid, false);
});

// --------------------------------------------------------------------------
// buildSecurityPayload
// --------------------------------------------------------------------------
test('buildSecurityPayload: watermark carries all fields as strings', () => {
    const p = buildSecurityPayload(baseWatermark);
    assert.deepEqual(p, {
        operation: 'watermark',
        text: 'Confidential',
        position: 'center',
        opacity: '0.15',
        font_size: '48',
        rotation: '45',
    });
    assert.equal(typeof p.opacity, 'string');
});

test('buildSecurityPayload: watermark trims text', () => {
    const p = buildSecurityPayload({ ...baseWatermark, text: '  Draft  ' });
    assert.equal(p.text, 'Draft');
});

test('buildSecurityPayload: protect carries only operation + password', () => {
    const p = buildSecurityPayload({ ...baseWatermark, operation: 'protect', password: 'pw' });
    assert.deepEqual(p, { operation: 'protect', password: 'pw' });
});

test('buildSecurityPayload: unlock carries only operation + password', () => {
    const p = buildSecurityPayload({ ...baseWatermark, operation: 'unlock', password: 'pw' });
    assert.deepEqual(p, { operation: 'unlock', password: 'pw' });
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml escapes HTML metacharacters', () => {
    assert.equal(escapeHtml('<a href="x">&\'</a>'),
        '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
});
