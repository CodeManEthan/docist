'use strict';

// Every icon-only button (a single glyph such as ▲ ▼ ✕, or one entity, for
// its text) has an accessible name: an aria-label that matches its title.
// Scans the markup the tool scripts and templates build.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const SOURCES = [
    ...fs.readdirSync(path.join(ROOT, 'static')).filter(f => f.endsWith('.js')).map(f => path.join('static', f)),
    ...fs.readdirSync(path.join(ROOT, 'templates')).filter(f => f.endsWith('.html')).map(f => path.join('templates', f)),
];
const ICON_BUTTON = /<button\b([^>]*)>\s*([^\s<A-Za-z0-9]|&[a-z]+;)\s*<\/button>/g;

function attr(attrs, name) {
    const m = attrs.match(new RegExp(`\\b${name}="([^"]*)"`));
    return m ? m[1] : null;
}

function iconButtons() {
    const found = [];
    for (const rel of SOURCES) {
        const text = fs.readFileSync(path.join(ROOT, rel), 'utf8');
        for (const m of text.matchAll(ICON_BUTTON)) found.push({ rel, attrs: m[1], glyph: m[2] });
    }
    return found;
}

test('the Merge and Security file rows have icon-only buttons to check', () => {
    const rels = new Set(iconButtons().map(b => b.rel));
    assert.ok(rels.has(path.join('static', 'app.js')));
    assert.ok(rels.has(path.join('static', 'security.js')));
});

test('every icon-only button has an aria-label matching its title', () => {
    for (const { rel, attrs, glyph } of iconButtons()) {
        const label = attr(attrs, 'aria-label');
        assert.ok(label, `${rel}: the ${glyph} button has no aria-label`);
        const title = attr(attrs, 'title');
        if (title !== null) assert.equal(label, title, `${rel}: the ${glyph} button`);
    }
});
