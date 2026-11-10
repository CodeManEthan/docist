'use strict';

// The scripts in templates/_csrf_head.html load as classic scripts into one
// global scope, on every page. A function declared at the top level of two of
// them is silently replaced by the later one, and a const or let declared in
// two throws. The unit tests load each file alone, so this checks the set.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');

function headScripts() {
    const head = fs.readFileSync(path.join(ROOT, 'templates', '_csrf_head.html'), 'utf8');
    return [...head.matchAll(/<script src="\/static\/([^"]+\.js)"/g)].map((m) => m[1]);
}

// Names declared at the top level (column 0) of a script.
function topLevelNames(source) {
    const re = /^(?:async\s+)?(?:function\*?\s*([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))/gm;
    return [...source.matchAll(re)].map((m) => m[1] || m[2]);
}

test('the head lists the three fetch wrappers', () => {
    assert.deepEqual(headScripts(), ['csrf.js', 'upload-limit.js', 'usage.js']);
});

test('no two head scripts declare the same top-level name', () => {
    const owner = new Map();
    const clashes = [];
    for (const file of headScripts()) {
        const source = fs.readFileSync(path.join(ROOT, 'static', file), 'utf8');
        const names = topLevelNames(source);
        assert.ok(names.length > 0, `${file}: no top-level names found`);
        for (const name of names) {
            if (owner.has(name) && owner.get(name) !== file) {
                clashes.push(`${name} (${owner.get(name)}, ${file})`);
            }
            owner.set(name, file);
        }
    }
    assert.deepEqual(clashes, []);
});
