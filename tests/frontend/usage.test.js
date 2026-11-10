'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    REMAINING_HEADER,
    LIMIT_HEADER,
    parseCount,
    usageFromHeaders,
    usageText,
    applyUsage,
    wrapFetch,
    installUsageFetch,
} = require(path.join(__dirname, '..', '..', 'static', 'usage.js'));

const ORIGIN = 'https://docist.example';
const START = '7 of 10 left today';

function span(text = START) {
    return { textContent: text };
}

function usageResponse(remaining, limit, body = '{"ok":true}') {
    const headers = {};
    if (remaining !== undefined) headers[REMAINING_HEADER] = remaining;
    if (limit !== undefined) headers[LIMIT_HEADER] = limit;
    return new Response(body, { status: 200, headers });
}

// A fetch that answers every call with `response` and records the calls.
function fakeFetch(response) {
    const calls = [];
    const fn = function (input, init) {
        calls.push({ input, init, self: this });
        return Promise.resolve(response);
    };
    fn.calls = calls;
    return fn;
}

function wrap(origFetch, el) {
    return wrapFetch(origFetch, () => el, () => ORIGIN);
}

// --------------------------------------------------------------------------
// Pure helpers
// --------------------------------------------------------------------------
test('header names match the server', () => {
    assert.equal(REMAINING_HEADER, 'X-Docist-Usage-Remaining');
    assert.equal(LIMIT_HEADER, 'X-Docist-Usage-Limit');
});

test('parseCount takes only whole numbers', () => {
    assert.equal(parseCount('0'), 0);
    assert.equal(parseCount(' 12 '), 12);
    for (const bad of [null, undefined, '', 'unlimited', '-1', '1.5', '1e3', 'abc', '9'.repeat(20)]) {
        assert.equal(parseCount(bad), null, String(bad));
    }
});

test('usageFromHeaders needs both headers numeric', () => {
    const h = (pairs) => new Headers(pairs);
    assert.deepEqual(usageFromHeaders(h({ [REMAINING_HEADER]: '6', [LIMIT_HEADER]: '10' })),
        { remaining: 6, limit: 10 });
    assert.equal(usageFromHeaders(h({ [REMAINING_HEADER]: 'unlimited', [LIMIT_HEADER]: 'unlimited' })), null);
    assert.equal(usageFromHeaders(h({ [REMAINING_HEADER]: '6' })), null);
    assert.equal(usageFromHeaders(h({ [LIMIT_HEADER]: '10' })), null);
    assert.equal(usageFromHeaders(h({})), null);
    assert.equal(usageFromHeaders(undefined), null);
});

test('usageText matches the nav template', () => {
    assert.equal(usageText({ remaining: 0, limit: 10 }), '0 of 10 left today');
});

test('applyUsage with no span does nothing', () => {
    applyUsage(null, usageResponse('6', '10'));
});

// --------------------------------------------------------------------------
// The wrapper
// --------------------------------------------------------------------------
test('a numeric pair rewrites the span', async () => {
    const el = span();
    const res = usageResponse('6', '10');
    const out = await wrap(fakeFetch(res), el)('/pages/rotate', { method: 'POST' });
    assert.equal(out, res);
    assert.equal(el.textContent, '6 of 10 left today');
});

test('a 429 at the limit still rewrites the span', async () => {
    const el = span('1 of 10 left today');
    const res = new Response('{"code":"daily_limit"}', {
        status: 429, headers: { [REMAINING_HEADER]: '0', [LIMIT_HEADER]: '10' },
    });
    await wrap(fakeFetch(res), el)('/merge', { method: 'POST' });
    assert.equal(el.textContent, '0 of 10 left today');
});

test('unlimited leaves the span as it is', async () => {
    const el = span();
    await wrap(fakeFetch(usageResponse('unlimited', 'unlimited')), el)('/merge', { method: 'POST' });
    assert.equal(el.textContent, START);
});

test('a missing header leaves the span as it is', async () => {
    for (const res of [usageResponse('6', undefined), usageResponse(undefined, '10'),
        usageResponse(undefined, undefined)]) {
        const el = span();
        await wrap(fakeFetch(res), el)('/formats');
        assert.equal(el.textContent, START);
    }
});

test('a page with no span gets its response all the same', async () => {
    const res = usageResponse('6', '10');
    const out = await wrap(fakeFetch(res), null)('/merge', { method: 'POST' });
    assert.equal(out, res);
});

test('a cross-origin response leaves the span as it is', async () => {
    const el = span();
    await wrap(fakeFetch(usageResponse('6', '10')), el)('https://other.example/x');
    assert.equal(el.textContent, START);
    await wrap(fakeFetch(usageResponse('6', '10')), el)(ORIGIN + '/merge');
    assert.equal(el.textContent, '6 of 10 left today');
});

test('a Request input is judged by its url', async () => {
    const el = span();
    await wrap(fakeFetch(usageResponse('5', '10')), el)(new Request(ORIGIN + '/convert', { method: 'POST' }));
    assert.equal(el.textContent, '5 of 10 left today');
    await wrap(fakeFetch(usageResponse('4', '10')), el)(new Request('https://other.example/x'));
    assert.equal(el.textContent, '5 of 10 left today');
});

test('the response comes back with its body unread', async () => {
    const res = usageResponse('6', '10', '{"download_url":"/download/x.pdf"}');
    const out = await wrap(fakeFetch(res), span())('/merge', { method: 'POST' });
    assert.equal(out.bodyUsed, false);
    assert.deepEqual(await out.json(), { download_url: '/download/x.pdf' });
});

test('the call reaches fetch unchanged', async () => {
    const orig = fakeFetch(usageResponse('6', '10'));
    const init = { method: 'POST', body: 'x' };
    const self = { name: 'window' };
    await wrap(orig, span()).call(self, '/merge', init);
    assert.equal(orig.calls.length, 1);
    assert.equal(orig.calls[0].input, '/merge');
    assert.equal(orig.calls[0].init, init);
    assert.equal(orig.calls[0].self, self);
});

test('a rejected fetch rejects as before and changes nothing', async () => {
    const el = span();
    const err = new TypeError('Failed to fetch');
    const orig = () => Promise.reject(err);
    await assert.rejects(wrap(orig, el)('/merge', { method: 'POST' }), (e) => e === err);
    assert.equal(el.textContent, START);
});

test('a span lookup that throws never fails the fetch', async () => {
    const res = usageResponse('6', '10');
    const wrapped = wrapFetch(fakeFetch(res), () => { throw new Error('boom'); }, () => ORIGIN);
    assert.equal(await wrapped('/merge'), res);
});

test('the span is looked up per response, not at install', async () => {
    let el = null;
    const wrapped = wrapFetch(fakeFetch(usageResponse('3', '10')), () => el, () => ORIGIN);
    el = span();
    await wrapped('/merge');
    assert.equal(el.textContent, '3 of 10 left today');
});

// --------------------------------------------------------------------------
// Install
// --------------------------------------------------------------------------
function fakeWindow(el) {
    return {
        fetch: fakeFetch(usageResponse('2', '10')),
        location: { origin: ORIGIN },
        document: { querySelector: (sel) => (sel === '.nav-usage' ? el : null) },
    };
}

test('install wraps window.fetch once and reads .nav-usage', async () => {
    const el = span();
    const win = fakeWindow(el);
    installUsageFetch(win);
    const once = win.fetch;
    assert.equal(once.__docistUsage, true);
    installUsageFetch(win);
    assert.equal(win.fetch, once);
    await win.fetch('/merge', { method: 'POST' });
    assert.equal(el.textContent, '2 of 10 left today');
});

test('install keeps the earlier wrappers underneath', async () => {
    const win = fakeWindow(span());
    const inner = win.fetch;
    inner.__docistCsrf = true;
    installUsageFetch(win);
    assert.notEqual(win.fetch, inner);
    await win.fetch('/merge');
    assert.equal(inner.calls.length, 1);
});

test('install with no fetch does nothing', () => {
    const win = { location: { origin: ORIGIN }, document: { querySelector: () => null } };
    installUsageFetch(win);
    assert.equal(win.fetch, undefined);
});
