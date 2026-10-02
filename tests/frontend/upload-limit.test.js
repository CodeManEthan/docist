'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    PART_ALLOWANCE,
    formDataBytes,
    limitMb,
    tooLargeBody,
    parseLimit,
    overLimit,
    wrapFetch,
} = require(path.join(__dirname, '..', '..', 'static', 'upload-limit.js'));

const ORIGIN = 'https://docist.example';
const MIB = 1024 * 1024;

function file(size, name = 'a.pdf') {
    return new File([new Uint8Array(size)], name, { type: 'application/pdf' });
}

// What fetch() would really send for this FormData.
async function realBodyLength(fd) {
    return (await new Response(fd).arrayBuffer()).byteLength;
}

test('formDataBytes counts files, text fields and 1 KiB per part', () => {
    const fd = new FormData();
    fd.append('files[]', file(1000, 'a.pdf'));
    fd.append('files[]', file(2000, 'b.pdf'));
    fd.append('paper', 'a4');
    const expected = 1000 + 2000 + 2
        + 3 * PART_ALLOWANCE
        + 'files[]'.length * 2 + 'paper'.length
        + 'a.pdf'.length + 'b.pdf'.length;
    assert.equal(formDataBytes(fd), expected);
});

test('text is counted in UTF-8 bytes', () => {
    const fd = new FormData();
    fd.append('note', 'café');      // 5 bytes in UTF-8, 4 characters
    assert.equal(formDataBytes(fd), PART_ALLOWANCE + 4 + 5);
});

test('the bound is never smaller than the body fetch sends', async () => {
    const cases = [
        [['files[]', file(0, 'x')]],
        [['files[]', file(12345, 'résumé.docx')], ['paper', 'letter']],
        [['a', ''], ['b', 'ü'.repeat(300)], ['files[]', file(1, 'n'.repeat(200))]],
    ];
    for (const entries of cases) {
        const fd = new FormData();
        for (const [k, v] of entries) fd.append(k, v);
        assert.ok(formDataBytes(fd) >= await realBodyLength(fd));
    }
});

test('limitMb and the 413 body match the server', () => {
    assert.equal(limitMb(50 * MIB), 50);
    assert.equal(limitMb(90 * MIB), 90);
    assert.equal(limitMb(95.5 * MIB), 95.5);
    assert.deepEqual(tooLargeBody(50 * MIB), {
        error: 'This upload is over the 50 MB limit.',
        code: 'upload_too_large',
        limit_mb: 50,
    });
});

test('parseLimit takes only a positive whole number', () => {
    assert.equal(parseLimit('52428800'), 52428800);
    assert.equal(parseLimit(' 94371840 '), 94371840);
    for (const bad of ['', null, undefined, '0', '-5', '1e9', 'abc', '12.5', '9'.repeat(20)]) {
        assert.equal(parseLimit(bad), 0, String(bad));
    }
});

test('overLimit checks only same-origin FormData POSTs', () => {
    const fd = new FormData();
    fd.append('files[]', file(5000));
    assert.equal(overLimit('POST', '/upload', fd, ORIGIN, 1000), true);
    assert.equal(overLimit('post', ORIGIN + '/upload', fd, ORIGIN, 1000), true);
    assert.equal(overLimit('POST', 'https://other.example/upload', fd, ORIGIN, 1000), false);
    assert.equal(overLimit('GET', '/upload', fd, ORIGIN, 1000), false);
    assert.equal(overLimit('POST', '/upload', 'a string', ORIGIN, 1000), false);
    assert.equal(overLimit('POST', '/upload', fd, ORIGIN, 0), false);   // no limit known
    assert.equal(overLimit('POST', 'http://[bad', fd, ORIGIN, 1000), false);
});

function fakeFetch() {
    const calls = [];
    const fn = async (input, init) => {
        calls.push([input, init]);
        return new Response('{"ok":true}', { status: 200 });
    };
    return { fn, calls };
}

test('over the limit the wrapper returns 413 without calling fetch', async () => {
    const inner = fakeFetch();
    const limit = 10 * 1024;
    const wrapped = wrapFetch(inner.fn, () => limit, () => ORIGIN);
    const fd = new FormData();
    fd.append('files[]', file(limit));          // the file alone fills the limit
    const res = await wrapped('/upload', { method: 'POST', body: fd });
    assert.equal(res.status, 413);
    assert.deepEqual(await res.json(), tooLargeBody(limit));
    assert.equal(inner.calls.length, 0);
});

test('at the limit minus the allowance it passes', async () => {
    const inner = fakeFetch();
    const limit = 10 * 1024;
    const wrapped = wrapFetch(inner.fn, () => limit, () => ORIGIN);
    const name = 'files[]';
    const filename = 'a.pdf';
    const fd = new FormData();
    fd.append(name, file(limit - PART_ALLOWANCE - name.length - filename.length, filename));
    assert.equal(formDataBytes(fd), limit);
    const res = await wrapped('/upload', { method: 'POST', body: fd });
    assert.equal(res.status, 200);
    assert.equal(inner.calls.length, 1);
    // One byte more is refused.
    const fd2 = new FormData();
    fd2.append(name, file(limit - PART_ALLOWANCE - name.length - filename.length + 1, filename));
    assert.equal((await wrapped('/upload', { method: 'POST', body: fd2 })).status, 413);
    assert.equal(inner.calls.length, 1);
});

test('GETs and foreign posts go straight through', async () => {
    const inner = fakeFetch();
    const wrapped = wrapFetch(inner.fn, () => 1, () => ORIGIN);
    const fd = new FormData();
    fd.append('files[]', file(100));
    await wrapped('/formats');
    await wrapped('https://other.example/x', { method: 'POST', body: fd });
    assert.equal(inner.calls.length, 2);
});
