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
    appendPageToRanges,
    rangesFieldFor,
    morePagesNote,
    normalizeThumbs,
    THUMB_ENDPOINT,
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

// --------------------------------------------------------------------------
// appendPageToRanges — clicking a thumbnail feeds the ranges box
// --------------------------------------------------------------------------
test('appendPageToRanges: empty spec becomes the page number', () => {
    assert.equal(appendPageToRanges('', 3), '3');
    assert.equal(appendPageToRanges('   ', 3), '3');
    assert.equal(appendPageToRanges(null, 1), '1');
    assert.equal(appendPageToRanges(undefined, 7), '7');
});

test('appendPageToRanges: appends after an existing range', () => {
    assert.equal(appendPageToRanges('1-2', 3), '1-2,3');
    assert.equal(appendPageToRanges('1-2,4', 9), '1-2,4,9');
});

test('appendPageToRanges: appends after a single page', () => {
    assert.equal(appendPageToRanges('1', 2), '1,2');
});

test('appendPageToRanges: absorbs dangling commas and whitespace', () => {
    assert.equal(appendPageToRanges('1,', 3), '1,3');
    assert.equal(appendPageToRanges('1, ', 3), '1,3');
    assert.equal(appendPageToRanges(' 1 - 2 , 5 ', 8), '1 - 2,5,8');
    assert.equal(appendPageToRanges(',,', 4), '4');
});

test('appendPageToRanges: does not duplicate an exact token', () => {
    assert.equal(appendPageToRanges('3', 3), '3');
    assert.equal(appendPageToRanges('1,3,5', 3), '1,3,5');
    assert.equal(appendPageToRanges('1, 3 ,5', 3), '1,3,5'); // also normalizes
});

test('appendPageToRanges: a page covered by a range is still appended', () => {
    // '1-5' is not the token '3', and the backend dedupes overlaps anyway.
    assert.equal(appendPageToRanges('1-5', 3), '1-5,3');
});

test('appendPageToRanges: invalid page numbers leave the spec untouched', () => {
    assert.equal(appendPageToRanges('1-2', 0), '1-2');
    assert.equal(appendPageToRanges('1-2', -4), '1-2');
    assert.equal(appendPageToRanges('1-2', 'x'), '1-2');
    assert.equal(appendPageToRanges('1-2', null), '1-2');
    assert.equal(appendPageToRanges('1-2', undefined), '1-2');
    assert.equal(appendPageToRanges('', 0), '');
});

test('appendPageToRanges: accepts numeric strings for the page', () => {
    assert.equal(appendPageToRanges('1', '2'), '1,2');
});

test('appendPageToRanges: repeated clicks build a growing spec', () => {
    let spec = '';
    for (const page of [3, 4, 4, 10]) spec = appendPageToRanges(spec, page);
    assert.equal(spec, '3,4,10');
});

test('appendPageToRanges output stays valid for validateRangeSpec', () => {
    let spec = '';
    for (const page of [2, 5, 9]) spec = appendPageToRanges(spec, page);
    assert.equal(validateRangeSpec(spec).valid, true);
});

// --------------------------------------------------------------------------
// rangesFieldFor
// --------------------------------------------------------------------------
test('rangesFieldFor maps range-taking operations to their input ids', () => {
    assert.equal(rangesFieldFor('extract'), 'extractRanges');
    assert.equal(rangesFieldFor('remove'), 'removeRanges');
    assert.equal(rangesFieldFor('rotate'), 'rotateRanges');
});

test('rangesFieldFor returns null for operations without a ranges box', () => {
    for (const op of ['split', 'compress', 'ocr', 'nope', '', null, undefined]) {
        assert.equal(rangesFieldFor(op), null, `expected ${op} to have no field`);
    }
});

// --------------------------------------------------------------------------
// morePagesNote
// --------------------------------------------------------------------------
test('morePagesNote: nothing to say when everything was rendered', () => {
    assert.equal(morePagesNote(10, 10), '');
    assert.equal(morePagesNote(3, 3), '');
    assert.equal(morePagesNote(0, 0), '');
    assert.equal(morePagesNote(2, 5), ''); // nonsense input, no note
});

test('morePagesNote: reports the remainder past the cap', () => {
    assert.equal(morePagesNote(30, 24), '+6 more pages');
    assert.equal(morePagesNote(100, 24), '+76 more pages');
});

test('morePagesNote: singular for exactly one extra page', () => {
    assert.equal(morePagesNote(25, 24), '+1 more page');
});

test('morePagesNote: non-numeric input yields no note', () => {
    assert.equal(morePagesNote('x', 24), '');
    assert.equal(morePagesNote(30, null), '');
    assert.equal(morePagesNote(undefined, undefined), '');
});

// --------------------------------------------------------------------------
// normalizeThumbs
// --------------------------------------------------------------------------
const PNG_URL = 'data:image/png;base64,iVBORw0KGgo=';

test('normalizeThumbs: passes a well-formed payload through', () => {
    const out = normalizeThumbs({ pages: 30, rendered: 2, thumbs: [PNG_URL, PNG_URL] });
    assert.equal(out.pages, 30);
    assert.equal(out.rendered, 2);
    assert.equal(out.thumbs.length, 2);
});

test('normalizeThumbs: empty preview for junk payloads', () => {
    for (const bad of [null, undefined, {}, { error: 'x' }, { thumbs: 'no' }]) {
        assert.deepEqual(normalizeThumbs(bad), { pages: 0, rendered: 0, thumbs: [] });
    }
});

test('normalizeThumbs: drops entries that are not image data URLs', () => {
    const out = normalizeThumbs({
        pages: 3, rendered: 3,
        thumbs: [PNG_URL, 'https://evil.example/x.png', 7, 'javascript:alert(1)'],
    });
    assert.deepEqual(out.thumbs, [PNG_URL]);
    assert.equal(out.rendered, 1);
});

test('normalizeThumbs: falls back to the thumb count when pages is missing', () => {
    const out = normalizeThumbs({ thumbs: [PNG_URL, PNG_URL] });
    assert.equal(out.pages, 2);
    assert.equal(out.rendered, 2);
});

test('normalizeThumbs + morePagesNote compose for a capped document', () => {
    const out = normalizeThumbs({ pages: 40, thumbs: new Array(24).fill(PNG_URL) });
    assert.equal(morePagesNote(out.pages, out.rendered), '+16 more pages');
});

test('THUMB_ENDPOINT points at the preview route', () => {
    assert.equal(THUMB_ENDPOINT, '/preview/thumbs');
});
