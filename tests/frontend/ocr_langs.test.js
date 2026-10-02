'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
    joinLanguages,
    selectedLanguage,
} = require(path.join(__dirname, '..', '..', 'static', 'ocr-langs.js'));

test('one language stays as it is', () => {
    assert.equal(joinLanguages('spa', ''), 'spa');
    assert.equal(joinLanguages('chi_sim', null), 'chi_sim');
});

test('a second language is joined with +', () => {
    assert.equal(joinLanguages('eng', 'spa'), 'eng+spa');
});

test('a repeated second language adds nothing', () => {
    assert.equal(joinLanguages('eng', 'eng'), 'eng');
});

test('a blank first language falls back to eng', () => {
    assert.equal(joinLanguages('', ''), 'eng');
    assert.equal(joinLanguages(undefined, 'spa'), 'eng+spa');
});

test('selectedLanguage reads both selects', () => {
    const doc = (els) => ({ getElementById: (id) => els[id] || null });
    assert.equal(selectedLanguage(doc({ ocrLanguage: { value: 'fra' },
                                        ocrLanguage2: { value: 'deu' } })), 'fra+deu');
    assert.equal(selectedLanguage(doc({ ocrLanguage: { value: 'fra' } })), 'fra');
    assert.equal(selectedLanguage(doc({})), 'eng');
});
