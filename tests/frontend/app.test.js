'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    formatFileSize,
    getExt,
    isAccepted,
    buildFormatHint,
    dedupeExtensions,
    moveItem,
    removeItem,
    computeDropIndex,
    escapeHtml,
    wantsThumb,
    firstThumb,
    thumbCellHtml,
    THUMB_ENDPOINT,
} = require(path.join(__dirname, '..', '..', 'static', 'app.js'));

// --------------------------------------------------------------------------
// formatFileSize
// --------------------------------------------------------------------------
test('formatFileSize: bytes below 1 KiB render as B', () => {
    assert.equal(formatFileSize(0), '0 B');
    assert.equal(formatFileSize(1), '1 B');
    assert.equal(formatFileSize(512), '512 B');
    assert.equal(formatFileSize(1023), '1023 B');
});

test('formatFileSize: 1024 boundary switches to KB', () => {
    assert.equal(formatFileSize(1024), '1.0 KB');
    assert.equal(formatFileSize(1536), '1.5 KB');
    assert.equal(formatFileSize(1024 * 1024 - 1), '1024.0 KB');
});

test('formatFileSize: 1 MiB boundary switches to MB', () => {
    assert.equal(formatFileSize(1024 * 1024), '1.0 MB');
    assert.equal(formatFileSize(5 * 1024 * 1024), '5.0 MB');
    assert.equal(formatFileSize(1024 * 1024 * 1.5), '1.5 MB');
});

test('formatFileSize: rounds to one decimal place', () => {
    assert.equal(formatFileSize(1126), '1.1 KB'); // 1126/1024 = 1.099...
    assert.equal(formatFileSize(1024 * 1024 * 2.25), '2.3 MB');
});

// --------------------------------------------------------------------------
// getExt
// --------------------------------------------------------------------------
test('getExt: returns lowercased extension with dot', () => {
    assert.equal(getExt('file.pdf'), '.pdf');
    assert.equal(getExt('FILE.PDF'), '.pdf');
    assert.equal(getExt('Report.Md'), '.md');
});

test('getExt: no extension returns empty string', () => {
    assert.equal(getExt('README'), '');
    assert.equal(getExt('noext'), '');
});

test('getExt: uses the last dot for multi-dot names', () => {
    assert.equal(getExt('archive.tar.gz'), '.gz');
    assert.equal(getExt('my.file.name.PDF'), '.pdf');
});

test('getExt: leading-dot / trailing-dot edge cases', () => {
    assert.equal(getExt('.gitignore'), '.gitignore');
    assert.equal(getExt('file.'), '.');
});

// --------------------------------------------------------------------------
// isAccepted
// --------------------------------------------------------------------------
test('isAccepted: matches an accepted extension', () => {
    const accepted = ['.pdf', '.md', '.docx'];
    assert.equal(isAccepted('doc.pdf', accepted), true);
    assert.equal(isAccepted('notes.md', accepted), true);
});

test('isAccepted: rejects an unaccepted extension', () => {
    const accepted = ['.pdf', '.md'];
    assert.equal(isAccepted('image.png', accepted), false);
    assert.equal(isAccepted('noext', accepted), false);
});

test('isAccepted: is case-insensitive on the filename', () => {
    const accepted = ['.pdf', '.md'];
    assert.equal(isAccepted('DOC.PDF', accepted), true);
    assert.equal(isAccepted('Report.Md', accepted), true);
});

test('isAccepted: uses only the final extension for multi-dot names', () => {
    const accepted = ['.pdf'];
    assert.equal(isAccepted('report.final.pdf', accepted), true);
    assert.equal(isAccepted('report.pdf.txt', accepted), false);
});

test('isAccepted: empty accepted list rejects everything', () => {
    assert.equal(isAccepted('doc.pdf', []), false);
});

// --------------------------------------------------------------------------
// buildFormatHint
// --------------------------------------------------------------------------
test('buildFormatHint: uppercases labels and strips leading dot', () => {
    assert.equal(buildFormatHint(['.pdf', '.md']), 'Supported: PDF, MD');
});

test('buildFormatHint: dedups repeated extensions', () => {
    assert.equal(
        buildFormatHint(['.pdf', '.md', '.pdf', '.md']),
        'Supported: PDF, MD'
    );
});

test('buildFormatHint: preserves first-seen order', () => {
    assert.equal(
        buildFormatHint(['.md', '.pdf', '.docx']),
        'Supported: MD, PDF, DOCX'
    );
});

test('buildFormatHint: single extension', () => {
    assert.equal(buildFormatHint(['.pdf']), 'Supported: PDF');
});

// --------------------------------------------------------------------------
// dedupeExtensions
// --------------------------------------------------------------------------
test('dedupeExtensions: removes duplicates preserving order', () => {
    assert.deepEqual(
        dedupeExtensions(['.pdf', '.md', '.pdf']),
        ['.pdf', '.md']
    );
});

// --------------------------------------------------------------------------
// moveItem
// --------------------------------------------------------------------------
test('moveItem: moves an item down and returns a new array', () => {
    const arr = ['a', 'b', 'c'];
    const out = moveItem(arr, 0, 2);
    assert.deepEqual(out, ['b', 'c', 'a']);
    assert.notEqual(out, arr); // new array
    assert.deepEqual(arr, ['a', 'b', 'c']); // original untouched
});

test('moveItem: moves an item up', () => {
    assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 0), ['c', 'a', 'b']);
});

test('moveItem: no-op when from === to returns same reference', () => {
    const arr = ['a', 'b', 'c'];
    assert.equal(moveItem(arr, 1, 1), arr);
});

test('moveItem: out-of-range target (below 0) is a no-op', () => {
    const arr = ['a', 'b', 'c'];
    assert.equal(moveItem(arr, 0, -1), arr); // moving first item up
});

test('moveItem: out-of-range target (>= length) is a no-op', () => {
    const arr = ['a', 'b', 'c'];
    assert.equal(moveItem(arr, 2, 3), arr); // moving last item down
});

test('moveItem: adjacent swaps at boundaries', () => {
    assert.deepEqual(moveItem(['a', 'b', 'c'], 0, 1), ['b', 'a', 'c']);
    assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 1), ['a', 'c', 'b']);
});

// --------------------------------------------------------------------------
// removeItem
// --------------------------------------------------------------------------
test('removeItem: removes at index and returns a new array', () => {
    const arr = ['a', 'b', 'c'];
    const out = removeItem(arr, 1);
    assert.deepEqual(out, ['a', 'c']);
    assert.notEqual(out, arr);
    assert.deepEqual(arr, ['a', 'b', 'c']); // original untouched
});

test('removeItem: removes first and last', () => {
    assert.deepEqual(removeItem(['a', 'b', 'c'], 0), ['b', 'c']);
    assert.deepEqual(removeItem(['a', 'b', 'c'], 2), ['a', 'b']);
});

test('removeItem: out-of-range index leaves contents unchanged', () => {
    assert.deepEqual(removeItem(['a', 'b'], 5), ['a', 'b']);
});

test('removeItem: on single-element array yields empty array', () => {
    assert.deepEqual(removeItem(['only'], 0), []);
});

// --------------------------------------------------------------------------
// computeDropIndex — drag-drop target math incl. source-shift adjustment
// --------------------------------------------------------------------------
test('computeDropIndex: drop above a row, dragging downward', () => {
    // list [a,b,c,d]; drag a (0) onto top of c (2): target 2, src<target -> 1
    assert.equal(computeDropIndex(0, 2, false), 1);
});

test('computeDropIndex: drop below a row, dragging downward', () => {
    // drag a (0) below c (2): target 3, src<target -> 2 (a lands after c)
    assert.equal(computeDropIndex(0, 2, true), 2);
});

test('computeDropIndex: drop above a row, dragging upward', () => {
    // drag d (3) onto top of b (1): target 1, src(3) not < 1 -> 1
    assert.equal(computeDropIndex(3, 1, false), 1);
});

test('computeDropIndex: drop below a row, dragging upward', () => {
    // drag d (3) below b (1): target 2, src(3) not < 2 -> 2
    assert.equal(computeDropIndex(3, 1, true), 2);
});

test('computeDropIndex: dropping onto self is a no-op-ish index', () => {
    // drag b (1) onto top of itself: target 1, src(1) not < 1 -> 1 (== from)
    assert.equal(computeDropIndex(1, 1, false), 1);
    // drag b (1) below itself: target 2, src(1) < 2 -> 1 (== from)
    assert.equal(computeDropIndex(1, 1, true), 1);
});

test('computeDropIndex: first/last boundaries', () => {
    // drop above the very first row
    assert.equal(computeDropIndex(3, 0, false), 0);
    // drag first item below last row of a 4-item list
    assert.equal(computeDropIndex(0, 3, true), 3);
});

test('computeDropIndex + moveItem compose to reorder correctly (drag down)', () => {
    const arr = ['a', 'b', 'c', 'd'];
    // drag a (0) below c (2)
    const target = computeDropIndex(0, 2, true);
    assert.deepEqual(moveItem(arr, 0, target), ['b', 'c', 'a', 'd']);
});

test('computeDropIndex + moveItem compose to reorder correctly (drag up)', () => {
    const arr = ['a', 'b', 'c', 'd'];
    // drag d (3) above b (1)
    const target = computeDropIndex(3, 1, false);
    assert.deepEqual(moveItem(arr, 3, target), ['a', 'd', 'b', 'c']);
});

// --------------------------------------------------------------------------
// escapeHtml
// --------------------------------------------------------------------------
test('escapeHtml: escapes all five special characters', () => {
    assert.equal(
        escapeHtml(`&<>"'`),
        '&amp;&lt;&gt;&quot;&#39;'
    );
});

test('escapeHtml: neutralizes a script-injection filename', () => {
    const evil = '<script>alert("xss")</script>.pdf';
    const out = escapeHtml(evil);
    assert.ok(!out.includes('<script>'));
    assert.equal(
        out,
        '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;.pdf'
    );
});

test('escapeHtml: leaves ordinary text unchanged', () => {
    assert.equal(escapeHtml('my-report_v2.pdf'), 'my-report_v2.pdf');
});

// --------------------------------------------------------------------------
// wantsThumb — only PDFs get a preview fetched
// --------------------------------------------------------------------------
test('wantsThumb: true for .pdf, case-insensitive', () => {
    assert.equal(wantsThumb('report.pdf'), true);
    assert.equal(wantsThumb('REPORT.PDF'), true);
    assert.equal(wantsThumb('a.b.Pdf'), true);
});

test('wantsThumb: false for every non-PDF (they keep the plain row)', () => {
    for (const name of ['notes.md', 'photo.png', 'sheet.docx', 'noext', 'pdf']) {
        assert.equal(wantsThumb(name), false, `expected ${name} to be skipped`);
    }
});

test('wantsThumb: only the final extension counts', () => {
    assert.equal(wantsThumb('report.pdf.txt'), false);
});

// --------------------------------------------------------------------------
// firstThumb — reading the /preview/thumbs payload
// --------------------------------------------------------------------------
const PNG_URL = 'data:image/png;base64,iVBORw0KGgo=';

test('firstThumb: returns the first data URL', () => {
    assert.equal(
        firstThumb({ pages: 3, rendered: 3, thumbs: [PNG_URL, 'data:image/png;base64,AAA'] }),
        PNG_URL
    );
});

test('firstThumb: empty string for missing / malformed payloads', () => {
    assert.equal(firstThumb(null), '');
    assert.equal(firstThumb(undefined), '');
    assert.equal(firstThumb({}), '');
    assert.equal(firstThumb({ error: 'nope' }), '');
    assert.equal(firstThumb({ thumbs: [] }), '');
    assert.equal(firstThumb({ thumbs: 'not-an-array' }), '');
});

test('firstThumb: rejects a non-image / non-data first entry', () => {
    assert.equal(firstThumb({ thumbs: [42] }), '');
    assert.equal(firstThumb({ thumbs: ['https://evil.example/x.png'] }), '');
    assert.equal(firstThumb({ thumbs: ['javascript:alert(1)'] }), '');
});

// --------------------------------------------------------------------------
// thumbCellHtml — row cell markup per state
// --------------------------------------------------------------------------
test('thumbCellHtml: loading state renders a placeholder', () => {
    const html = thumbCellHtml('loading');
    assert.ok(html.includes('thumb-loading'));
    assert.ok(html.includes('file-thumb'));
    assert.ok(!html.includes('<img'));
});

test('thumbCellHtml: ready state renders the image', () => {
    const html = thumbCellHtml('ready', PNG_URL);
    assert.ok(html.includes('<img class="thumb-img"'));
    assert.ok(html.includes(PNG_URL));
    assert.ok(!html.includes('thumb-loading'));
});

test('thumbCellHtml: error / unknown / missing states render nothing', () => {
    assert.equal(thumbCellHtml('error'), '');
    assert.equal(thumbCellHtml('none'), '');
    assert.equal(thumbCellHtml(undefined), '');
    assert.equal(thumbCellHtml(''), '');
});

test('thumbCellHtml: ready without a URL degrades to nothing', () => {
    assert.equal(thumbCellHtml('ready', ''), '');
    assert.equal(thumbCellHtml('ready'), '');
});

test('thumbCellHtml: escapes the src so a crafted URL cannot break out', () => {
    const html = thumbCellHtml('ready', 'data:image/png;base64,A"><script>x</script>');
    assert.ok(!html.includes('<script>'));
    assert.ok(html.includes('&quot;'));
});

test('THUMB_ENDPOINT points at the preview route', () => {
    assert.equal(THUMB_ENDPOINT, '/preview/thumbs');
});
