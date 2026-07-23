'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    ALL_OPERATIONS,
    BATES_POSITIONS,
    HEADERFOOTER_SLOTS,
    validateHeaderFooterOptions,
    validateBatesOptions,
    validateSecurityState,
    buildSecurityPayload,
} = require(path.join(__dirname, '..', '..', 'static', 'security.js'));

const baseHeaderFooter = {
    file: {},
    operation: 'headerfooter',
    headerLeft: '',
    headerCenter: '',
    headerRight: '',
    footerLeft: '',
    footerCenter: 'Page {page} of {pages}',
    footerRight: '',
    fontSize: '9',
};

const baseBates = {
    file: {},
    operation: 'bates',
    prefix: 'ACME',
    start: '1',
    digits: '6',
    position: 'bottom-right',
    fontSize: '9',
};

// --------------------------------------------------------------------------
// constants
// --------------------------------------------------------------------------
test('ALL_OPERATIONS includes the new stamp operations', () => {
    assert.ok(ALL_OPERATIONS.includes('headerfooter'));
    assert.ok(ALL_OPERATIONS.includes('bates'));
});

test('BATES_POSITIONS exposes the four corners', () => {
    assert.deepEqual(BATES_POSITIONS,
        ['bottom-right', 'bottom-left', 'top-right', 'top-left']);
});

test('HEADERFOOTER_SLOTS names the six slots', () => {
    assert.deepEqual(HEADERFOOTER_SLOTS, [
        'headerLeft', 'headerCenter', 'headerRight',
        'footerLeft', 'footerCenter', 'footerRight',
    ]);
});

// --------------------------------------------------------------------------
// validateHeaderFooterOptions
// --------------------------------------------------------------------------
test('validateHeaderFooterOptions: at least one slot passes', () => {
    const r = validateHeaderFooterOptions(baseHeaderFooter);
    assert.equal(r.valid, true);
    assert.equal(r.error, null);
});

test('validateHeaderFooterOptions: all-empty fails', () => {
    const r = validateHeaderFooterOptions({
        ...baseHeaderFooter, footerCenter: '',
    });
    assert.equal(r.valid, false);
    assert.match(r.error, /slot/i);
});

test('validateHeaderFooterOptions: whitespace-only slots fail', () => {
    const r = validateHeaderFooterOptions({
        ...baseHeaderFooter, footerCenter: '   ', headerLeft: '  ',
    });
    assert.equal(r.valid, false);
});

test('validateHeaderFooterOptions: any single slot suffices', () => {
    for (const slot of HEADERFOOTER_SLOTS) {
        const empty = {
            ...baseHeaderFooter,
            headerLeft: '', headerCenter: '', headerRight: '',
            footerLeft: '', footerCenter: '', footerRight: '',
        };
        empty[slot] = 'x';
        assert.equal(validateHeaderFooterOptions(empty).valid, true, slot);
    }
});

test('validateHeaderFooterOptions: non-positive font size fails', () => {
    assert.equal(validateHeaderFooterOptions(
        { ...baseHeaderFooter, fontSize: '0' }).valid, false);
    assert.equal(validateHeaderFooterOptions(
        { ...baseHeaderFooter, fontSize: 'abc' }).valid, false);
});

// --------------------------------------------------------------------------
// validateBatesOptions
// --------------------------------------------------------------------------
test('validateBatesOptions: valid state passes', () => {
    const r = validateBatesOptions(baseBates);
    assert.equal(r.valid, true);
    assert.equal(r.error, null);
});

test('validateBatesOptions: start < 0 fails', () => {
    const r = validateBatesOptions({ ...baseBates, start: '-1' });
    assert.equal(r.valid, false);
    assert.match(r.error, /start/i);
});

test('validateBatesOptions: non-integer start fails', () => {
    assert.equal(validateBatesOptions({ ...baseBates, start: '1.5' }).valid, false);
    assert.equal(validateBatesOptions({ ...baseBates, start: 'x' }).valid, false);
});

test('validateBatesOptions: start of 0 is allowed', () => {
    assert.equal(validateBatesOptions({ ...baseBates, start: '0' }).valid, true);
});

test('validateBatesOptions: digits out of 3-10 range fail', () => {
    assert.equal(validateBatesOptions({ ...baseBates, digits: '2' }).valid, false);
    assert.equal(validateBatesOptions({ ...baseBates, digits: '11' }).valid, false);
});

test('validateBatesOptions: digits at bounds pass', () => {
    assert.equal(validateBatesOptions({ ...baseBates, digits: '3' }).valid, true);
    assert.equal(validateBatesOptions({ ...baseBates, digits: '10' }).valid, true);
});

test('validateBatesOptions: bad position fails', () => {
    assert.equal(validateBatesOptions({ ...baseBates, position: 'middle' }).valid, false);
});

// --------------------------------------------------------------------------
// validateSecurityState routes to the right validator
// --------------------------------------------------------------------------
test('validateSecurityState: headerfooter with no slots fails', () => {
    const r = validateSecurityState({
        ...baseHeaderFooter, footerCenter: '',
    });
    assert.equal(r.valid, false);
});

test('validateSecurityState: bates with bad digits fails', () => {
    const r = validateSecurityState({ ...baseBates, digits: '99' });
    assert.equal(r.valid, false);
});

test('validateSecurityState: valid headerfooter/bates pass', () => {
    assert.equal(validateSecurityState(baseHeaderFooter).valid, true);
    assert.equal(validateSecurityState(baseBates).valid, true);
});

test('validateSecurityState: no file still fails for stamp ops', () => {
    assert.equal(validateSecurityState(
        { ...baseHeaderFooter, file: null }).valid, false);
    assert.equal(validateSecurityState(
        { ...baseBates, file: null }).valid, false);
});

// --------------------------------------------------------------------------
// buildSecurityPayload
// --------------------------------------------------------------------------
test('buildSecurityPayload: headerfooter carries the six slots + font size', () => {
    const p = buildSecurityPayload(baseHeaderFooter);
    assert.deepEqual(p, {
        operation: 'headerfooter',
        header_left: '',
        header_center: '',
        header_right: '',
        footer_left: '',
        footer_center: 'Page {page} of {pages}',
        footer_right: '',
        font_size: '9',
    });
});

test('buildSecurityPayload: bates carries prefix/start/digits/position as strings', () => {
    const p = buildSecurityPayload(baseBates);
    assert.deepEqual(p, {
        operation: 'bates',
        prefix: 'ACME',
        start: '1',
        digits: '6',
        position: 'bottom-right',
    });
    assert.equal(typeof p.start, 'string');
    assert.equal(typeof p.digits, 'string');
});

test('buildSecurityPayload: bates empty prefix becomes empty string', () => {
    const p = buildSecurityPayload({ ...baseBates, prefix: '' });
    assert.equal(p.prefix, '');
});
