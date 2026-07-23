'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    validateOptionalInt,
    buildRunPayload,
} = require(path.join(__dirname, '..', '..', 'static', 'pages.js'));

// --------------------------------------------------------------------------
// validateOptionalInt
// --------------------------------------------------------------------------
test('validateOptionalInt treats empty as omit', () => {
    for (const v of ['', '   ', null, undefined]) {
        const r = validateOptionalInt(v, 10, 95, 'Quality');
        assert.equal(r.valid, true);
        assert.equal(r.omit, true);
    }
});

test('validateOptionalInt accepts in-range integers', () => {
    const r = validateOptionalInt('60', 10, 95, 'Quality');
    assert.equal(r.valid, true);
    assert.equal(r.omit, undefined);
    assert.equal(r.value, 60);
});

test('validateOptionalInt rejects out-of-range', () => {
    assert.equal(validateOptionalInt('5', 10, 95, 'Quality').valid, false);
    assert.equal(validateOptionalInt('96', 10, 95, 'Quality').valid, false);
    assert.equal(validateOptionalInt('71', 72, 300, 'DPI').valid, false);
    assert.equal(validateOptionalInt('301', 72, 300, 'DPI').valid, false);
});

test('validateOptionalInt rejects non-integers', () => {
    for (const bad of ['abc', '1.5', '6x', '--3']) {
        assert.equal(validateOptionalInt(bad, 10, 95, 'Quality').valid, false, `expected ${bad} invalid`);
    }
});

// --------------------------------------------------------------------------
// buildRunPayload — compress
// --------------------------------------------------------------------------
test('buildRunPayload: compress with no params omits optionals', () => {
    const r = buildRunPayload({ operation: 'compress' });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, { operation: 'compress' });
});

test('buildRunPayload: compress passes valid quality and dpi', () => {
    const r = buildRunPayload({
        operation: 'compress', imageQuality: '40', imageMaxDpi: '120',
    });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, {
        operation: 'compress', image_quality: '40', image_max_dpi: '120',
    });
});

test('buildRunPayload: compress only sends provided fields', () => {
    const r = buildRunPayload({ operation: 'compress', imageQuality: '80', imageMaxDpi: '' });
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields, { operation: 'compress', image_quality: '80' });
});

test('buildRunPayload: compress rejects bad quality', () => {
    assert.equal(buildRunPayload({ operation: 'compress', imageQuality: '5' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'compress', imageQuality: '99' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'compress', imageQuality: 'x' }).ok, false);
});

test('buildRunPayload: compress rejects bad dpi', () => {
    assert.equal(buildRunPayload({ operation: 'compress', imageMaxDpi: '10' }).ok, false);
    assert.equal(buildRunPayload({ operation: 'compress', imageMaxDpi: '999' }).ok, false);
});

test('buildRunPayload: compress is a recognised operation', () => {
    // Regression guard: compress must not fall through to the unknown-op error.
    const r = buildRunPayload({ operation: 'compress' });
    assert.notEqual(r.ok, false);
});
