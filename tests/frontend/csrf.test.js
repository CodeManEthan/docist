'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    CSRF_HEADER,
    needsCsrf,
    withCsrf,
} = require(path.join(__dirname, '..', '..', 'static', 'csrf.js'));

const ORIGIN = 'https://docist.example';
const TOKEN = 'tok-123';

// --------------------------------------------------------------------------
// needsCsrf
// --------------------------------------------------------------------------
test('header name matches the server check', () => {
    assert.equal(CSRF_HEADER, 'X-CSRF-Token');
});

test('unsafe same-origin methods need the token, in any case', () => {
    for (const m of ['POST', 'post', 'PUT', 'PATCH', 'DELETE']) {
        assert.equal(needsCsrf(m, '/upload', ORIGIN), true, m);
    }
});

test('GET, HEAD and a missing method never need it', () => {
    assert.equal(needsCsrf('GET', '/formats', ORIGIN), false);
    assert.equal(needsCsrf('head', '/formats', ORIGIN), false);
    assert.equal(needsCsrf(undefined, '/formats', ORIGIN), false);
});

test('cross-origin and protocol-relative urls never get it', () => {
    assert.equal(needsCsrf('POST', 'https://evil.example/x', ORIGIN), false);
    assert.equal(needsCsrf('POST', '//evil.example/x', ORIGIN), false);
    assert.equal(needsCsrf('POST', 'http://docist.example/x', ORIGIN), false);
});

test('absolute same-origin url counts as same origin', () => {
    assert.equal(needsCsrf('POST', ORIGIN + '/pages/run', ORIGIN), true);
});

// --------------------------------------------------------------------------
// withCsrf
// --------------------------------------------------------------------------
test('same-origin POST gets the header on a fresh object', () => {
    assert.deepEqual(withCsrf('POST', '/upload', ORIGIN, TOKEN, undefined),
        { 'X-CSRF-Token': TOKEN });
});

test('existing object headers are kept and not mutated', () => {
    const h = { Accept: 'application/json' };
    const out = withCsrf('POST', '/upload', ORIGIN, TOKEN, h);
    assert.deepEqual(out, { Accept: 'application/json', 'X-CSRF-Token': TOKEN });
    assert.deepEqual(h, { Accept: 'application/json' });
});

test('array headers get a pair appended', () => {
    const out = withCsrf('POST', '/upload', ORIGIN, TOKEN, [['Accept', '*/*']]);
    assert.deepEqual(out, [['Accept', '*/*'], ['X-CSRF-Token', TOKEN]]);
});

test('Headers instances are copied with the token set', () => {
    const h = new Headers({ Accept: '*/*' });
    const out = withCsrf('POST', '/upload', ORIGIN, TOKEN, h);
    assert.ok(out instanceof Headers);
    assert.equal(out.get('X-CSRF-Token'), TOKEN);
    assert.equal(h.get('X-CSRF-Token'), null);
});

test('GET returns the headers untouched', () => {
    const h = { Accept: '*/*' };
    assert.equal(withCsrf('GET', '/formats', ORIGIN, TOKEN, h), h);
    assert.equal(withCsrf('GET', '/formats', ORIGIN, TOKEN, undefined), undefined);
});

test('cross-origin POST returns the headers untouched', () => {
    const h = { Accept: '*/*' };
    assert.equal(withCsrf('POST', 'https://evil.example/x', ORIGIN, TOKEN, h), h);
});

test('no token means no header', () => {
    assert.equal(withCsrf('POST', '/upload', ORIGIN, '', undefined), undefined);
});
