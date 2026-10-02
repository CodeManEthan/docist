'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    PAPER_KEY,
    PAPERS,
    DEFAULT_PAPER,
    readPaper,
    writePaper,
} = require(path.join(__dirname, '..', '..', 'static', 'paper.js'));

function memoryStorage(initial) {
    const data = Object.assign({}, initial);
    return {
        data,
        getItem: (k) => (k in data ? data[k] : null),
        setItem: (k, v) => { data[k] = String(v); },
    };
}

const throwingStorage = {
    getItem() { throw new Error('SecurityError'); },
    setItem() { throw new Error('QuotaExceededError'); },
};

test('key, papers and default', () => {
    assert.equal(PAPER_KEY, 'docist.paper');
    assert.deepEqual(PAPERS, ['letter', 'a4']);
    assert.equal(DEFAULT_PAPER, 'letter');
});

test('readPaper returns what a working storage holds', () => {
    assert.equal(readPaper(memoryStorage({ 'docist.paper': 'a4' })), 'a4');
    assert.equal(readPaper(memoryStorage({ 'docist.paper': 'letter' })), 'letter');
});

test('readPaper falls back to letter on an empty storage', () => {
    assert.equal(readPaper(memoryStorage()), 'letter');
});

test('readPaper ignores a value that is not a paper', () => {
    assert.equal(readPaper(memoryStorage({ 'docist.paper': 'legal' })), 'letter');
    assert.equal(readPaper(memoryStorage({ 'docist.paper': 'A4' })), 'letter');
});

test('readPaper survives a throwing or missing storage', () => {
    assert.equal(readPaper(throwingStorage), 'letter');
    assert.equal(readPaper(null), 'letter');
    assert.equal(readPaper(undefined), 'letter');
});

test('writePaper stores a paper in a working storage', () => {
    const s = memoryStorage();
    assert.equal(writePaper(s, 'a4'), true);
    assert.equal(s.data['docist.paper'], 'a4');
    assert.equal(readPaper(s), 'a4');
});

test('writePaper refuses an unknown value and leaves storage alone', () => {
    const s = memoryStorage({ 'docist.paper': 'a4' });
    assert.equal(writePaper(s, 'legal'), false);
    assert.equal(s.data['docist.paper'], 'a4');
});

test('writePaper survives a throwing or missing storage', () => {
    assert.equal(writePaper(throwingStorage, 'a4'), false);
    assert.equal(writePaper(null, 'a4'), false);
});
