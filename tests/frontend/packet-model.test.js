'use strict';

// node:test for the packet builder's client model (design.md §6) and the
// page script's client functions, against tests/frontend/fake-packet-server.js.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const M = require(path.join(__dirname, '..', '..', 'static', 'packet-model.js'));
const C = require(path.join(__dirname, '..', '..', 'static', 'packet.js'));
const { createFakeServer, API } = require(path.join(__dirname, 'fake-packet-server.js'));
const PLAN = require(path.join(__dirname, '..', 'fixtures', 'packet-plan.json'));

// This file's stage: it runs page-plan and placement vectors whose model tag
// is at or before B7 in the fixture's stage order, and skips later ones by
// name.
const STAGE = 'B7';

const HA = 'a'.repeat(64);
const HB = 'b'.repeat(64);
const HC = 'c'.repeat(64);

function stage(hash, pages, extra) {
    return Object.assign({
        stage_id: (hash[0] + 'stage' + pages + 'x'.repeat(22)).slice(0, 22),
        pages, key: hash + '::', bytes: 1000, name: hash[0] + '.pdf', type: 'pdf',
    }, extra || {});
}

// A state with files a (3 pages) and b (2 pages) in one section, both bound.
function twoFiles() {
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }, { hash: HB, label: 'b.pdf' }]));
    s = M.bindStage(s, HA, stage(HA, 3)).state;
    s = M.bindStage(s, HB, stage(HB, 2)).state;
    return s;
}

// The plan as short strings: 'a0' is page 0 of file a, 'r90' marks rotation.
function seq(s, packet) {
    const p = packet || s.packet;
    return M.pagePlan(p, M.readyCounts(s.table, p)).pages
        .map(x => x.kind === 'file' ? x.hash[0] + x.src + (x.rot ? 'r' + x.rot : '') : x.kind).join(' ');
}

function deepFreeze(o) {
    if (o && typeof o === 'object' && !Object.isFrozen(o)) {
        Object.freeze(o);
        Object.values(o).forEach(deepFreeze);
    }
    return o;
}

class MemoryStorage {
    constructor() { this.map = new Map(); }
    get length() { return this.map.size; }
    key(i) { return Array.from(this.map.keys())[i] ?? null; }
    getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
    setItem(k, v) { this.map.set(k, String(v)); }
    removeItem(k) { this.map.delete(k); }
}

class ThrowingStorage {
    get length() { throw new Error('blocked'); }
    key() { throw new Error('blocked'); }
    getItem() { throw new Error('blocked'); }
    setItem() { throw new Error('blocked'); }
    removeItem() { throw new Error('blocked'); }
}

// ---------------------------------------------------------------------------
// Purity
// ---------------------------------------------------------------------------

test('every model function returns a new packet and leaves its input unchanged', () => {
    const s = twoFiles();
    s.packet = M.addSection(s.packet, 'Second');
    s.packet = M.addFiles(s.packet, [{ hash: HC, label: 'c.pdf' }], { section: 's2' });
    s.packet = M.insertGenerated(s.packet, 'blank', { section: 's1', index: 1 });
    const before = JSON.stringify(s.packet);
    deepFreeze(s);
    const t = s.table;
    const calls = [
        p => M.addFiles(p, [{ hash: HC, label: 'c2.pdf' }]),
        p => M.insertGenerated(p, 'blank', null),
        p => M.insertGenerated(p, 'divider', { section: 's2' }),
        p => M.insertGenerated(p, 'cover', null, { title: 'T' }),
        p => M.removeItems(p, ['i1']),
        p => M.clearPacket(p),
        p => M.duplicateItems(p, ['i1']),
        p => M.setLabel(p, 'i1', 'renamed.pdf'),
        p => M.moveItems(p, ['i2'], { section: 's1', index: 0 }),
        p => M.addSection(p, 'New'),
        p => M.renameSection(p, 's2', 'Renamed'),
        p => M.deleteSection(p, 's2'),
        p => M.moveSection(p, 's2', 0),
        p => M.setCover(p, { title: 'Cover' }),
        p => M.setDivider(p, 's1', true),
        p => M.addDividers(p),
        p => M.setOption(p, 'page_numbers', false),
        p => M.toggleOption(p, 'recto_starts'),
        p => M.setPaper(p, 'a4'),
        p => M.rotatePages(p, t, [{ item: 'i1', index: 0 }], 90),
        p => M.deletePages(p, t, [{ item: 'i1', index: 0 }]),
        p => M.duplicatePages(p, t, [{ item: 'i1', index: 0 }]),
        p => M.rotateItems(p, t, ['i2'], -90),
        p => M.movePages(p, t, [{ item: 'i1', index: 0 }], { item: 'i2', index: 1 }),
    ];
    calls.forEach((fn, n) => {
        const out = fn(s.packet);
        assert.notEqual(out, s.packet, 'call ' + n + ' returned the input');
        assert.equal(JSON.stringify(s.packet), before, 'call ' + n + ' changed the input');
    });
    // State-level functions leave their input alone too.
    const states = [
        () => M.bindStage(s, HA, stage(HA, 5)),
        () => M.expireStages(s, [s.table[HA].stage_id]),
        () => M.commit(s, M.clearPacket(s.packet)),
        () => M.releaseUndoOnly(s),
        () => M.releaseUnnamed(s),
    ];
    states.forEach(fn => fn());
    assert.equal(JSON.stringify(s.packet), before);
});

test('a change that changes nothing returns the same packet', () => {
    const s = twoFiles();
    assert.equal(M.setOption(s.packet, 'page_numbers', true), s.packet);
    assert.equal(M.setPaper(s.packet, 'letter'), s.packet);
    assert.equal(M.moveItems(s.packet, ['i1'], { section: 's1', index: 0 }), s.packet);
    assert.equal(M.removeItems(s.packet, ['nope']), s.packet);
    assert.equal(M.rotatePages(s.packet, s.table, [{ item: 'i1', index: 9 }], 90), s.packet);
    assert.equal(M.removeCover(s.packet), s.packet);
    assert.equal(M.setDivider(s.packet, 's1', false), s.packet);
    assert.equal(M.setDivider(s.packet, 'nope', true), s.packet);
    const covered = M.setCover(s.packet, { title: 'T' });
    assert.equal(M.setCover(covered, { title: ' T ' }), covered);
    const all = M.addDividers(s.packet);
    assert.equal(M.addDividers(all), all);
    assert.equal(M.renameSection(s.packet, 's1', ''), s.packet);
});

// ---------------------------------------------------------------------------
// Sections, cover and dividers ([B3], §2.1, §3.1)
// ---------------------------------------------------------------------------

// Files a (3 pages), b (2) and c (1), bound, in sections s1 'One' (a, b)
// and s2 'Two' (c).
function threeInTwoSections() {
    let s = twoFiles();
    s = M.commit(s, M.renameSection(s.packet, 's1', 'One'));
    s = M.commit(s, M.addSection(s.packet, 'Two'));
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HC, label: 'c.pdf' }], { section: 's2' }));
    s = M.bindStage(s, HC, stage(HC, 1)).state;
    return s;
}

test('add, rename, reorder and delete sections', () => {
    let p = M.emptyPacket('letter');
    p = M.addSection(p, '  Exhibit\nA  ');
    p = M.addSection(p, '');
    assert.deepEqual(p.sections.map(x => [x.id, x.title]), [['s1', ''], ['s2', 'Exhibit A'], ['s3', 'New section']]);
    p = M.addSection(p, 'First', 0);
    assert.equal(p.sections[0].id, 's4', 'ids stay unique');
    p = M.renameSection(p, 's2', 'x'.repeat(500));
    assert.equal(p.sections[2].title.length, M.TEXT_MAX.section);
    p = M.moveSection(p, 's4', 9);
    assert.deepEqual(p.sections.map(x => x.id), ['s1', 's2', 's3', 's4']);
    assert.equal(M.sectionName(p.sections[0]), 'Untitled section');
    assert.equal(M.sectionName(p.sections[3]), 'First');
});

test('deleting a section moves its contents to the previous one, with undo', () => {
    let s = threeInTwoSections();
    const before = s.packet;
    s = M.commit(s, M.deleteSection(s.packet, 's2'));
    assert.deepEqual(s.packet.sections.map(x => x.id), ['s1']);
    assert.deepEqual(s.packet.sections[0].items.map(i => i.id), ['i1', 'i2', 'i3']);
    s = M.undo(s);
    assert.equal(s.packet, before);
    // The first section's contents go to the start of the next one.
    const p = M.deleteSection(before, 's1');
    assert.deepEqual(p.sections.map(x => x.id), ['s2']);
    assert.deepEqual(p.sections[0].items.map(i => i.id), ['i1', 'i2', 'i3']);
    assert.equal(M.deleteSection(p, 's2'), p, 'the last section stays');
});

test('Move to section puts items and pages at the end of that section', () => {
    const s = threeInTwoSections();
    const items = M.moveItems(s.packet, ['i1'], { section: 's2', index: null });
    assert.deepEqual(items.sections.map(x => x.items.map(i => i.id)), [['i2'], ['i3', 'i1']]);
    const pages = M.movePages(s.packet, s.table, [{ item: 'i1', index: 1 }, { item: 'i2', index: 0 }],
        { section: 's2', index: null });
    assert.equal(seq(s, pages), 'a0 a2 b1 c0 a1 b0');
    assert.deepEqual(pages.sections[1].items.map(i => i.hash[0]), ['c', 'a', 'b']);
    // A Move menu step crosses into the next section.
    assert.deepEqual(M.itemStepDest(s.packet, 'i2', 'later'), { section: 's2', index: 0 });
});

test('the cover is one flag the plan always puts first', () => {
    let p = M.insertGenerated(M.emptyPacket(), 'cover', { section: 's1', index: 0 },
        { title: ' Application ', subtitle: 'J. Pluto', date: 'Oct 3', note: 'n'.repeat(400), extra: 'x' });
    assert.deepEqual(Object.keys(p.cover), M.COVER_FIELDS);
    assert.equal(p.cover.title, 'Application');
    assert.equal(p.cover.note.length, M.TEXT_MAX.note);
    p = M.setCover(p, { title: 'Again' });
    assert.deepEqual(p.cover, { title: 'Again', subtitle: '', date: '', note: '' });
    p = M.addFiles(p, [{ hash: HA, label: 'a.pdf' }]);
    assert.deepEqual(M.pagePlan(p, { [HA]: 2 }).pages.map(x => x.kind), ['cover', 'file', 'file']);
    assert.equal(M.removeCover(p).cover, null);
    assert.equal(M.clearPacket(p).cover.title, 'Again', 'Clear packet keeps the cover');
});

test('dividers: one section, all sections, and a section that gives no page', () => {
    const s = threeInTwoSections();
    let p = M.insertGenerated(s.packet, 'divider', { section: 's2', index: 0 });
    assert.deepEqual(p.sections.map(x => x.divider), [false, true]);
    assert.equal(seq(s, p), 'a0 a1 a2 b0 b1 divider c0');
    p = M.addDividers(p);
    assert.deepEqual(p.sections.map(x => x.divider), [true, true]);
    p = M.setDivider(p, 's1', false);
    assert.deepEqual(p.sections.map(x => x.divider), [false, true]);
    // A divider alone doesn't put a section in the plan.
    const emptied = M.removeItems(p, ['i3']);
    assert.equal(seq(s, emptied), 'a0 a1 a2 b0 b1');
});

test('the plan with a cover, dividers and recto starts gives the outline its numbers', () => {
    let s = threeInTwoSections();
    let p = M.setCover(M.addDividers(s.packet), { title: 'Cover' });
    p = M.setOption(p, 'recto_starts', true);
    const plan = M.pagePlan(p, M.readyCounts(s.table, p));
    assert.deepEqual(plan.pages.map(x => x.kind + ':' + x.label + (x.print ? '*' : '')),
        ['cover:null', 'pad:null', 'divider:1', 'file:2*', 'file:3*', 'file:4*', 'file:5*', 'file:6*',
            'divider:7', 'file:8*']);
    assert.deepEqual(plan.sections, { s1: { first: 3, label: 1 }, s2: { first: 9, label: 7 } });
    assert.deepEqual(plan.entries.filter(e => e.kind === 'section').map(e => [e.id, e.title, e.index]),
        [['s1', 'One', 2], ['s2', 'Two', 8]]);
});

test('a draft with a malformed cover or section is not used', () => {
    const s = threeInTwoSections();
    const good = JSON.parse(M.serializeDraft(M.setCover(s.packet, { title: 'T' }), s.table, 5));
    assert.ok(M.parseDraft(JSON.stringify(good)));
    const bad = fn => { const d = JSON.parse(JSON.stringify(good)); fn(d); return M.parseDraft(JSON.stringify(d)); };
    assert.equal(bad(d => { d.packet.cover = 'yes'; }), null);
    assert.equal(bad(d => { d.packet.cover.title = { html: '<b>' }; }), null);
    assert.equal(bad(d => { d.packet.sections[1].title = 7; }), null);
    assert.equal(bad(d => { d.packet.sections[1].divider = 'on'; }), null);
    assert.ok(bad(d => { d.packet.cover = null; }));
});

test('the "+" menu offers dividers once sections show, and a cover while there is none', () => {
    const one = twoFiles().packet;
    const ids = (p, sid) => C.plusEntries(p, sid).map(e => e.id);
    assert.deepEqual(ids(one, 's1'), ['blank', 'cover', 'files']);
    const two = threeInTwoSections().packet;
    assert.deepEqual(ids(two, 's2'), ['blank', 'divider', 'dividers', 'cover', 'files']);
    const withDivider = M.setDivider(two, 's2', true);
    assert.deepEqual(ids(withDivider, 's2'), ['blank', 'dividers', 'cover', 'files']);
    assert.deepEqual(ids(M.setCover(M.addDividers(two), { title: 'T' }), 's1'), ['blank', 'files']);
});

test('a packet of only a cover and dividers is not ready to build', () => {
    let s = M.emptyState('letter');
    s = M.commit(s, M.setCover(s.packet, { title: 'T' }));
    assert.equal(C.buildReadiness(s, null).ok, false);
    s = M.commit(s, M.insertGenerated(s.packet, 'blank', null));
    assert.equal(C.buildReadiness(s, null).ok, true);
});

// ---------------------------------------------------------------------------
// Moves
// ---------------------------------------------------------------------------

test('moving a page within its file reorders the run and writes explicit refs', () => {
    const s = twoFiles();
    const p = M.movePages(s.packet, s.table, [{ item: 'i1', index: 2 }], { item: 'i1', index: 0 });
    assert.equal(seq(s, p), 'a2 a0 a1 b0 b1');
    const item = M.findItem(p, 'i1').item;
    assert.equal(item.key, HA + '::');
    assert.equal(item.src_pages, 3);
});

test('moving a page into another file splits that run into two items', () => {
    const s = twoFiles();
    const p = M.movePages(s.packet, s.table, [{ item: 'i2', index: 0 }], { item: 'i1', index: 1 });
    assert.equal(seq(s, p), 'a0 b0 a1 a2 b1');
    const items = p.sections[0].items;
    assert.deepEqual(items.map(i => i.hash[0]), ['a', 'b', 'a', 'b']);
    assert.equal(items[0].id, 'i1', 'the first part keeps the item id');
    assert.equal(items[1].label, 'b.pdf', 'the moved run keeps its source label');
    assert.equal(new Set(items.map(i => i.id)).size, 4);
});

test('moving the split page back rejoins the runs', () => {
    const s = twoFiles();
    const p1 = M.movePages(s.packet, s.table, [{ item: 'i2', index: 0 }], { item: 'i1', index: 1 });
    const moved = p1.sections[0].items[1].id;
    const p2 = M.movePages(p1, s.table, [{ item: moved, index: 0 }], { item: p1.sections[0].items[3].id, index: 0 });
    assert.equal(seq(s, p2), 'a0 a1 a2 b0 b1');
    assert.equal(p2.sections[0].items.length, 2);
});

test('a page stepped into the next file and back rejoins its run', () => {
    const s = twoFiles();
    const p1 = M.movePages(s.packet, s.table, [{ item: 'i1', index: 2 }], M.pageStepDest(s.packet, s.table, { item: 'i1', index: 2 }, 'later'));
    assert.equal(seq(s, p1), 'a0 a1 b0 a2 b1');
    const back = p1.sections[0].items[2].id;
    const p2 = M.movePages(p1, s.table, [{ item: back, index: 0 }], M.pageStepDest(p1, s.table, { item: back, index: 0 }, 'earlier'));
    assert.equal(seq(s, p2), 'a0 a1 a2 b0 b1');
    assert.deepEqual(p2.sections[0].items.map(i => i.id), ['i1', 'i2']);
});

test('a move elsewhere leaves a duplicated card apart from its original', () => {
    // m3: rejoining is only for runs the move itself put side by side.
    let s = twoFiles();
    s = M.commit(s, M.rotatePages(s.packet, s.table, [{ item: 'i1', index: 0 }], 90));
    s = M.commit(s, M.duplicateItems(s.packet, ['i1']));
    const ids = s.packet.sections[0].items.map(i => i.id);
    assert.deepEqual(ids, ['i1', 'i3', 'i2']);
    const p = M.movePages(s.packet, s.table, [{ item: 'i2', index: 1 }], { item: 'i2', index: 0 });
    assert.deepEqual(p.sections[0].items.map(i => i.id + ':' + i.pages.length), ['i1:3', 'i3:3', 'i2:2']);
    // A page moved within the copy stays in the copy.
    const q = M.movePages(s.packet, s.table, [{ item: 'i3', index: 2 }], { item: 'i3', index: 0 });
    assert.deepEqual(q.sections[0].items.map(i => i.id + ':' + (i.pages ? i.pages.length : 'all')), ['i1:3', 'i3:3', 'i2:all']);
    assert.deepEqual(M.findItem(q, 'i3').item.pages.map(r => r.src), [2, 0, 1]);
});

test('moving pages across sections', () => {
    let s = twoFiles();
    s.packet = M.addSection(s.packet, 'Two');
    s.packet = M.addFiles(s.packet, [{ hash: HC, label: 'c.pdf' }], { section: 's2' });
    s = M.bindStage(s, HC, stage(HC, 2)).state;
    const p = M.movePages(s.packet, s.table, [{ item: 'i1', index: 1 }], { item: 'i3', index: 1 });
    assert.equal(seq(s, p), 'a0 a2 b0 b1 c0 a1 c1');
    assert.deepEqual(p.sections[1].items.map(i => i.hash[0]), ['c', 'a', 'c']);
    // Into the gap at the start of the second section.
    const q = M.movePages(s.packet, s.table, [{ item: 'i2', index: 1 }], { section: 's2', index: 0 });
    assert.equal(seq(s, q), 'a0 a1 a2 b0 b1 c0 c1');
    assert.equal(q.sections[1].items[0].hash, HB);
});

test('multi-select moves keep relative order and remove emptied items', () => {
    const s = twoFiles();
    const targets = [{ item: 'i2', index: 1 }, { item: 'i1', index: 0 }, { item: 'i1', index: 2 }];
    const p = M.movePages(s.packet, s.table, targets, { section: 's1', index: 0 });
    assert.equal(seq(s, p), 'a0 a2 b1 a1 b0');
    // Moving every page of b empties and removes its item.
    const q = M.movePages(s.packet, s.table, [{ item: 'i2', index: 0 }, { item: 'i2', index: 1 }], { item: 'i1', index: 0 });
    assert.equal(seq(s, q), 'b0 b1 a0 a1 a2');
    assert.ok(!M.findItem(q, 'i2'));
});

test('movedFirstIndex says where the first moved page lands', () => {
    const s = twoFiles();
    const cases = [
        [[{ item: 'i2', index: 0 }], { item: 'i1', index: 1 }],
        [[{ item: 'i1', index: 0 }], { section: 's1', index: 2 }],
        [[{ item: 'i1', index: 1 }, { item: 'i2', index: 1 }], { section: 's1', index: 0 }],
        [[{ item: 'i1', index: 2 }], { item: 'i2', index: 2 }],
    ];
    cases.forEach(([targets, dest]) => {
        const first = M.movedFirstIndex(s.packet, s.table, targets, dest);
        const p = M.movePages(s.packet, s.table, targets, dest);
        const order = M.pageOrder(p, M.readyCounts(s.table, p));
        const landed = order[first];
        const item = M.findItem(p, landed.item).item;
        const ref = M.itemRefs(item, M.readyCounts(s.table, p))[landed.index];
        const want = M.itemRefs(M.findItem(s.packet, targets[0].item).item, M.readyCounts(s.table, s.packet))[targets[0].index];
        // The first target in packet order is what lands first.
        const firstTarget = targets.slice().sort((x, y) => (x.item === y.item ? x.index - y.index : x.item < y.item ? -1 : 1))[0];
        const wantFirst = M.itemRefs(M.findItem(s.packet, firstTarget.item).item, M.readyCounts(s.table, s.packet))[firstTarget.index];
        assert.equal(item.hash, M.findItem(s.packet, firstTarget.item).item.hash);
        assert.equal(ref.src, wantFirst.src);
        assert.ok(want);
    });
});

test('Move menu steps: items and pages, earlier and later, start and end', () => {
    const s = twoFiles();
    assert.equal(M.itemStepDest(s.packet, 'i1', 'earlier'), null);
    const d = M.itemStepDest(s.packet, 'i1', 'later');
    assert.deepEqual(M.moveItems(s.packet, ['i1'], d).sections[0].items.map(i => i.id), ['i2', 'i1']);
    // A page steps past its neighbour, even across files.
    const step = (packet, page, where) => M.movePages(packet, s.table, [page], M.pageStepDest(packet, s.table, page, where));
    assert.equal(seq(s, step(s.packet, { item: 'i2', index: 0 }, 'earlier')), 'a0 a1 b0 a2 b1');
    assert.equal(seq(s, step(s.packet, { item: 'i1', index: 2 }, 'later')), 'a0 a1 b0 a2 b1');
    assert.equal(seq(s, step(s.packet, { item: 'i1', index: 2 }, 'start')), 'a2 a0 a1 b0 b1');
    assert.equal(seq(s, step(s.packet, { item: 'i1', index: 0 }, 'end')), 'a1 a2 a0 b0 b1');
    assert.equal(M.pageStepDest(s.packet, s.table, { item: 'i1', index: 0 }, 'earlier'), null);
    // Stepping past a blank lands on its far side.
    const withBlank = M.insertGenerated(s.packet, 'blank', { section: 's1', index: 1 });
    assert.equal(seq(s, withBlank), 'a0 a1 a2 blank b0 b1');
    assert.equal(seq(s, step(withBlank, { item: 'i2', index: 0 }, 'earlier')), 'a0 a1 a2 b0 blank b1');
    assert.equal(seq(s, step(withBlank, { item: 'i1', index: 2 }, 'later')), 'a0 a1 blank a2 b0 b1');
});

test('Move to position: before or after an item or a page', () => {
    const s = twoFiles();
    const d = M.itemDestNear(s.packet, ['i2'], 'i1', false);
    assert.deepEqual(M.moveItems(s.packet, ['i2'], d).sections[0].items.map(i => i.id), ['i2', 'i1']);
    assert.equal(M.itemDestNear(s.packet, ['i1'], 'i1', true), null);
    const t = [{ item: 'i1', index: 0 }];
    const pd = M.pageDestNear(s.packet, s.table, t, { item: 'i1', index: 2 }, true);
    assert.equal(seq(s, M.movePages(s.packet, s.table, t, pd)), 'a1 a2 a0 b0 b1');
    const pd2 = M.pageDestNear(s.packet, s.table, t, { item: 'i2', index: 1 }, false);
    assert.equal(seq(s, M.movePages(s.packet, s.table, t, pd2)), 'a1 a2 b0 a0 b1');
});

test('rotate, delete and duplicate pages', () => {
    const s = twoFiles();
    const r = M.rotatePages(s.packet, s.table, [{ item: 'i1', index: 1 }], -90);
    assert.equal(seq(s, r), 'a0 a1r270 a2 b0 b1');
    assert.equal(seq(s, M.rotatePages(r, s.table, [{ item: 'i1', index: 1 }], 90)), 'a0 a1 a2 b0 b1');
    const d = M.deletePages(s.packet, s.table, [{ item: 'i1', index: 0 }, { item: 'i2', index: 0 }, { item: 'i2', index: 1 }]);
    assert.equal(seq(s, d), 'a1 a2');
    assert.equal(seq(s, M.duplicatePages(s.packet, s.table, [{ item: 'i2', index: 0 }])), 'a0 a1 a2 b0 b0 b1');
    assert.equal(seq(s, M.rotateItems(s.packet, s.table, ['i2'], 90)), 'a0 a1 a2 b0r90 b1r90');
});

// ---------------------------------------------------------------------------
// Page plan: the shared vectors
// ---------------------------------------------------------------------------

test('the fixture names this stage', () => {
    assert.ok(PLAN.stage_order.includes(STAGE));
    const defaults = Object.assign({}, M.defaultOptions('letter'));
    assert.deepEqual(defaults, PLAN.option_defaults);
});

const atOrBefore = tag => PLAN.stage_order.indexOf(tag) <= PLAN.stage_order.indexOf(STAGE);
PLAN.vectors.forEach(v => {
    const name = 'page-plan vector ' + v.name + ' (model ' + v.stage.model + ')';
    if (!atOrBefore(v.stage.model)) {
        test(name, { skip: 'model tag ' + v.stage.model + ' is after ' + STAGE }, () => {});
        return;
    }
    test(name, () => {
        const packet = JSON.parse(JSON.stringify(v.packet));
        packet.options = Object.assign({}, PLAN.option_defaults, packet.options);
        const plan = M.pagePlan(packet, v.page_counts, v.contents_pages);
        // `index` (the page's place in its item) is the page script's
        // addition; the vectors don't list it.
        const pages = plan.pages.map(p => { const c = Object.assign({}, p); delete c.index; return c; });
        assert.deepEqual(pages, v.expect.pages);
        assert.deepEqual(plan.entries, v.expect.entries);
        assert.equal(plan.total, v.expect.pages.length, 'footer count matches the plan');
    });
});

test('the footer count is the plan length and counts only ready rows', () => {
    let s = twoFiles();
    assert.equal(M.pagePlan(s.packet, M.readyCounts(s.table, s.packet)).total, 5);
    s = Object.assign({}, s, { table: M.setRow(s.table, HB, { status: 'uploading' }) });
    assert.equal(M.pagePlan(s.packet, M.readyCounts(s.table, s.packet)).total, 3);
    assert.equal(C.buildReadiness(s, null).ok, false);
});

// ---------------------------------------------------------------------------
// Undo and redo
// ---------------------------------------------------------------------------

test('undo and redo: push, redo cleared by a new change', () => {
    let s = twoFiles();
    const p0 = s.packet;
    s = M.commit(s, M.removeItems(s.packet, ['i1']));
    s = M.undo(s);
    assert.equal(s.packet, p0);
    assert.equal(s.future.length, 1);
    s = M.redo(s);
    assert.ok(!M.findItem(s.packet, 'i1'));
    s = M.undo(s);
    s = M.commit(s, M.setOption(s.packet, 'bookmarks', false));
    assert.equal(s.future.length, 0, 'a new change clears redo');
    assert.equal(M.undo(M.emptyState()).packet.version, 1, 'undo with no past is a no-op');
});

test('undo is capped at 100 steps', () => {
    let s = M.emptyState('letter');
    for (let n = 1; n <= 130; n++) s = M.commit(s, M.setOption(s.packet, 'start_number', n + 1));
    assert.equal(s.past.length, M.UNDO_CAP);
    assert.equal(s.past[0].options.start_number, 31);
});

test('a status change leaves the packet and the stacks unchanged', () => {
    const s = twoFiles();
    const t = Object.assign({}, s, { table: M.setRow(s.table, HA, { status: 'uploading' }) });
    assert.equal(t.packet, s.packet);
    assert.equal(t.past, s.past);
    assert.equal(t.future, s.future);
    const b = M.bindStage(s, HA, stage(HA, 3));
    assert.equal(b.state.packet, s.packet, 'a binding that fits changes no packet');
    assert.equal(b.state.past.length, s.past.length);
});

test('undo past a card becoming ready keeps its pages', () => {
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }]));
    s = Object.assign({}, s, { table: M.setRow(s.table, HA, { status: 'uploading' }) });
    s = M.commit(s, M.setOption(s.packet, 'bookmarks', false));
    s = M.bindStage(s, HA, stage(HA, 3)).state;
    s = M.undo(s);
    assert.equal(seq(s), 'a0 a1 a2');
});

test('commitFrom makes a grab-mode drop one undo step', () => {
    let s = twoFiles();
    const snap = s.packet;
    const pastLen = s.past.length;
    s = M.replacePacket(s, M.moveItems(s.packet, ['i1'], { section: 's1', index: 1 }));
    s = M.replacePacket(s, M.moveItems(s.packet, ['i1'], { section: 's1', index: 0 }));
    s = M.replacePacket(s, M.moveItems(s.packet, ['i1'], { section: 's1', index: 1 }));
    s = M.commitFrom(s, snap);
    assert.equal(s.past.length, pastLen + 1);
    assert.equal(M.undo(s).packet, snap);
    const same = M.commitFrom(M.replacePacket(s, s.packet), s.packet);
    assert.equal(same.past.length, s.past.length);
});

// ---------------------------------------------------------------------------
// Holds
// ---------------------------------------------------------------------------

test('a stage is released only when the packet, past and future all drop its hash', () => {
    let s = twoFiles();
    s = M.commit(s, M.removeItems(s.packet, ['i2']));
    let r = M.releaseUnnamed(s);
    assert.deepEqual(r.release, [], 'past still names b');
    // Push b out of the history: 100 more changes.
    s = r.state;
    for (let n = 0; n < M.UNDO_CAP; n++) s = M.commit(s, M.setOption(s.packet, 'start_number', n + 2));
    r = M.releaseUnnamed(s);
    assert.deepEqual(r.release, [stage(HB, 2).stage_id]);
    assert.deepEqual(r.hashes, [HB]);
    assert.ok(!r.state.table[HB]);
    assert.deepEqual(M.keepIds(r.state), [stage(HA, 3).stage_id]);
});

test('a stage named only by the redo stack stays held until a change clears it', () => {
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }]));
    s = M.bindStage(s, HA, stage(HA, 3)).state;
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HC, label: 'c.pdf' }]));
    s = M.bindStage(s, HC, stage(HC, 1)).state;
    s = M.undo(s);
    let r = M.releaseUnnamed(s);
    assert.deepEqual(r.release, [], 'future still names c, so redo brings it back ready');
    assert.ok(r.state.table[HC]);
    s = M.commit(r.state, M.setOption(r.state.packet, 'start_number', 3));
    r = M.releaseUnnamed(s);
    assert.deepEqual(r.release, [stage(HC, 1).stage_id]);
});

test('two cards of one file are one hold', () => {
    let s = twoFiles();
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }]));
    assert.equal(M.itemOrder(s.packet).length, 3);
    assert.equal(M.keepIds(s).length, 2);
    // A split run is one hold too.
    s = M.commit(s, M.movePages(s.packet, s.table, [{ item: 'i2', index: 0 }], { item: 'i1', index: 1 }));
    assert.equal(M.keepIds(s).length, 2);
    assert.deepEqual(M.heldBytes(s), { bytes: 2000, files: 2 });
});

test('binding a hash to a new stage releases the old one at once', () => {
    const s = twoFiles();
    const r = M.bindStage(s, HA, stage(HA, 3, { stage_id: 'NEWstage0000000000000x' }));
    assert.deepEqual(r.release, [stage(HA, 3).stage_id]);
});

function ioFor(server, holder, getS, setS) {
    const toasts = [];
    return {
        toasts,
        io: {
            fetch: server.fetch,
            holder,
            get: getS,
            update: fn => setS(fn(getS())),
            release: ids => C.postHold(server.fetch, holder, [], ids),
            toast: t => toasts.push(t),
            sleep: () => Promise.resolve(),
        },
    };
}

function fileOf(name, size, fill) {
    return new File([new Uint8Array(size).fill(fill)], name, { type: 'application/pdf' });
}

test('at the cap, released undo-only stages make room and the retry succeeds', async () => {
    const server = createFakeServer({ capBytes: 4000 });
    const holder = 'HOLDERholderHOLDER0001';
    let s = M.emptyState('letter');
    const io = ioFor(server, holder, () => s, v => { s = v; });
    const files = [1, 2, 3, 4].map(n => fileOf('f' + n + '.pdf', 1000, n));
    const hashes = [];
    for (const f of files) hashes.push(await M.sha256Hex(await f.arrayBuffer(), crypto.subtle));
    s = M.commit(s, M.addFiles(s.packet, files.map((f, i) => ({ hash: hashes[i], label: f.name }))));
    for (let i = 0; i < 4; i++) assert.equal(await C.stageWithRelief(io.io, hashes[i], files[i]), 'ok');
    assert.equal(server.heldBy(holder).length, 4);

    // Remove two cards: their stages stay held, named only by the undo history.
    s = M.commit(s, M.removeItems(s.packet, ['i1', 'i2']));
    assert.deepEqual(M.releaseUnnamed(s).release, []);
    assert.equal(server.heldBy(holder).length, 4);

    // Add two files: the first stage is refused, relief releases the
    // undo-only stages, and the retry succeeds.
    const more = [fileOf('g1.pdf', 1000, 7), fileOf('g2.pdf', 1000, 8)];
    const mh = [];
    for (const f of more) mh.push(await M.sha256Hex(await f.arrayBuffer(), crypto.subtle));
    s = M.commit(s, M.addFiles(s.packet, more.map((f, i) => ({ hash: mh[i], label: f.name }))));
    assert.equal(await C.stageWithRelief(io.io, mh[0], more[0]), 'ok');
    assert.equal(io.toasts.length, 1);
    assert.match(io.toasts[0], /Made room/);
    assert.equal(await C.stageWithRelief(io.io, mh[1], more[1]), 'ok');
    assert.equal(server.heldBy(holder).length, 4);

    // Undoing one of those removals brings the card back expired.
    s = M.undo(s);
    s = M.undo(s);
    assert.ok(M.findItem(s.packet, 'i1'));
    assert.equal(M.rowStatus(s.table[hashes[0]], s.packet), 'expired');
    assert.equal(M.rowStatus(s.table[hashes[2]], s.packet), 'ready');
});

test('a paper change while a file converts stages it again when the request finishes', async () => {
    // M2: the request carries the paper of the moment it was sent, so the
    // answer binds Letter after the packet switched to A4. The queue's rule
    // re-checks the row when the request finishes and converts it again.
    const server = createFakeServer();
    const holder = 'HOLDERholderHOLDER0005';
    let s = M.emptyState('letter');
    const { io } = ioFor(server, holder, () => s, v => { s = v; });
    const files = new Map();
    const stager = C.createStageQueue(io, files, {});
    const realFetch = io.fetch;
    let sent = 0;
    io.fetch = async (url, init) => {
        if (url === '/packet/stage') {
            sent++;
            if (sent === 1) {
                // The user switches the paper while the first request is open;
                // the page's afterChange asks the queue for stale rows, and
                // the uploading row isn't one yet.
                s = M.commit(s, M.setPaper(s.packet, 'a4'));
                stager.restageStale();
            }
        }
        return realFetch(url, init);
    };
    const f = new File([new Uint8Array(40).fill(9)], 'notes.txt', { type: 'text/plain' });
    const h = await M.sha256Hex(await f.arrayBuffer(), crypto.subtle);
    files.set(h, f);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: h, label: 'notes.txt' }]));
    s = Object.assign({}, s, { table: M.setRow(s.table, h, { status: 'waiting', name: 'notes.txt' }) });
    await stager.enqueue(h);
    assert.equal(sent, 2, 'the file was staged again for the new paper');
    assert.equal(M.keyParts(s.table[h].key).paper, 'a4');
    assert.equal(M.rowStatus(s.table[h], s.packet), 'ready');
    assert.ok(C.buildReadiness(s, null).ok);
    // Once per paper: nothing more is queued.
    await stager.restageStale();
    assert.equal(sent, 2);
});

test('a stale row is staged once per paper, even if the server keeps the old paper', async () => {
    const server = createFakeServer();
    const holder = 'HOLDERholderHOLDER0006';
    let s = M.emptyState('letter');
    const { io } = ioFor(server, holder, () => s, v => { s = v; });
    const realFetch = io.fetch;
    let sent = 0;
    io.fetch = async (url, init) => {
        if (url !== '/packet/stage') return realFetch(url, init);
        sent++;
        const form = new FormData();
        form.append('file', init.body.get('file'));
        form.append('paper', 'letter');
        form.append('holder', init.body.get('holder'));
        return realFetch(url, { method: 'POST', body: form });
    };
    const files = new Map();
    const stager = C.createStageQueue(io, files, {});
    const f = new File([new Uint8Array(40).fill(4)], 'memo.txt', { type: 'text/plain' });
    const h = await M.sha256Hex(await f.arrayBuffer(), crypto.subtle);
    files.set(h, f);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: h, label: 'memo.txt' }]));
    await stager.enqueue(h);
    s = M.commit(s, M.setPaper(s.packet, 'a4'));
    await stager.restageStale();
    assert.equal(sent, 2);
    assert.equal(M.rowStatus(s.table[h], s.packet), 'stale', 'the wrong paper is left for the card to show');
});

test('a refusal that remains after relief shows the numbers', async () => {
    const server = createFakeServer({ capBytes: 1500 });
    let s = M.emptyState('letter');
    const holder = 'HOLDERholderHOLDER0002';
    const { io } = ioFor(server, holder, () => s, v => { s = v; });
    const f1 = fileOf('one.pdf', 1000, 1);
    const f2 = fileOf('two.pdf', 1000, 2);
    const h1 = await M.sha256Hex(await f1.arrayBuffer(), crypto.subtle);
    const h2 = await M.sha256Hex(await f2.arrayBuffer(), crypto.subtle);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: h1, label: 'one.pdf' }, { hash: h2, label: 'two.pdf' }]));
    assert.equal(await C.stageWithRelief(io, h1, f1), 'ok');
    assert.equal(await C.stageWithRelief(io, h2, f2), 'quota');
    assert.equal(s.table[h2].status, 'failed');
    assert.match(s.table[h2].message, /This packet holds 1 KB/);
});

test('a stage whose key carries another hash fails the card', async () => {
    let s = M.emptyState('letter');
    const f = fileOf('x.pdf', 10, 1);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'x.pdf' }]));
    const server = createFakeServer();
    const { io } = ioFor(server, 'HOLDERholderHOLDER0003', () => s, v => { s = v; });
    assert.equal(await C.stageWithRelief(io, HA, f), 'mismatch');
    assert.equal(s.table[HA].status, 'failed');
});

test('busy and rate-limit 429s wait and retry; CSRF and other refusals fail the card', async () => {
    let calls = 0;
    const answers = [
        new Response(JSON.stringify(API.endpoints.stage.responses.busy.body), { status: 429, headers: { 'Retry-After': '0' } }),
        new Response(JSON.stringify(API.shared.rate_limit.body), { status: 429, headers: { 'Retry-After': '0' } }),
        new Response(JSON.stringify(API.endpoints.stage.responses.encrypted.body), { status: 400 }),
    ];
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'x.pdf' }]));
    const io = {
        fetch: async () => answers[calls++], holder: 'h'.repeat(22), get: () => s,
        update: fn => { s = fn(s); }, release: async () => {}, toast: () => {}, sleep: async () => {},
    };
    assert.equal(await C.stageWithRelief(io, HA, fileOf('x.pdf', 1, 1)), 'rejected');
    assert.equal(calls, 3);
    assert.equal(s.table[HA].message, API.endpoints.stage.responses.encrypted.body.error);
});

test('classify reads every packet-api.json response by status and code', () => {
    const st = API.endpoints.stage.responses;
    assert.equal(C.classify(200, st.ok_pdf.body).kind, 'ok');
    assert.equal(C.classify(400, st.staging_quota.body).kind, 'quota');
    assert.equal(C.classify(400, st.staging_full.body).kind, 'full');
    assert.equal(C.classify(400, st.too_large.body).kind, 'too_large');
    assert.equal(C.classify(400, st.rejected.body).kind, 'rejected');
    assert.equal(C.classify(400, st.unreadable.body).code, 'unreadable');
    assert.equal(C.classify(413, st.upload_too_large.body).kind, 'upload_too_large');
    assert.equal(C.classify(429, st.busy.body).kind, 'busy');
    assert.equal(C.classify(404, API.endpoints.thumb.responses.not_found.body).kind, 'expired');
    const b = API.endpoints.build.responses;
    assert.equal(C.classify(400, b.bad_items.body).kind, 'bad_items');
    ['too_large', 'too_many_pages', 'empty'].forEach(k => {
        assert.notEqual(C.classify(400, b[k].body).kind, 'bad_items', k);
        assert.equal(C.classify(400, b[k].body).error, b[k].body.error);
    });
    assert.equal(C.classify(400, b.invalid.body).kind, 'bad_items');
    assert.equal(C.classify(429, b.daily_limit.body).kind, 'daily_limit');
    assert.equal(C.classify(400, API.shared.csrf.body).kind, 'csrf');
    assert.equal(C.classify(429, API.shared.rate_limit.body).kind, 'busy');
    // Every item code a build may name has its own card text.
    b.bad_items.body.items.concat(b.invalid.body.items).forEach(it => {
        assert.notEqual(C.itemCodeText(it.code), C.itemCodeText('no-such-code'), it.code);
    });
});

test('postHold splits more than 500 ids over several requests and merges the answers', async () => {
    const bodies = [];
    const fetchFn = async (url, init) => {
        const body = JSON.parse(init.body);
        bodies.push(body);
        const live = {};
        body.keep.forEach(id => { live[id] = 1; });
        return new Response(JSON.stringify({ live, gone: [] }), { status: 200 });
    };
    const keep = Array.from({ length: 700 }, (_, i) => 'k' + i);
    const answer = await C.postHold(fetchFn, 'h'.repeat(22), keep, ['r1']);
    assert.equal(bodies.length, 2);
    bodies.forEach(b => assert.ok(b.keep.length + b.release.length <= 500));
    assert.equal(Object.keys(answer.live).length, 700);
    assert.deepEqual(await C.postHold(fetchFn, 'h'.repeat(22), [], []), { live: {}, gone: [] });
});

test('build sends the ready stages by hash and the fake answers with the fixture body', async () => {
    const server = createFakeServer();
    let s = M.emptyState('letter');
    const holder = 'HOLDERholderHOLDER0004';
    const { io } = ioFor(server, holder, () => s, v => { s = v; });
    const f = fileOf('a.pdf', 100, 3);
    const h = await M.sha256Hex(await f.arrayBuffer(), crypto.subtle);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: h, label: 'a.pdf' }]));
    await C.stageWithRelief(io, h, f);
    assert.deepEqual(Object.keys(C.buildStages(s)), [h]);
    assert.equal(C.buildReadiness(s, 3).ok, true);
    assert.equal(C.buildReadiness(s, 0).ok, false, 'no operations left disables Build');
    const res = await C.postBuild(server.fetch, s, holder);
    assert.equal(res.status, 200);
    assert.deepEqual(res.body, API.endpoints.build.responses.ok.body);
    assert.equal(res.remaining, '7');
    server.expire(s.table[h].stage_id);
    const bad = await C.postBuild(server.fetch, s, holder);
    assert.equal(C.classify(bad.status, bad.body).kind, 'bad_items');
    assert.deepEqual(bad.body.items, [{ id: 'i1', code: 'expired' }]);
});

// ---------------------------------------------------------------------------
// Identity and fit
// ---------------------------------------------------------------------------

test('binding by hash fills every item with that hash', () => {
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }, { hash: HB, label: 'b.pdf' }, { hash: HA, label: 'a.pdf' }]));
    assert.equal(seq(s), '');
    s = M.bindStage(s, HA, stage(HA, 2)).state;
    assert.equal(seq(s), 'a0 a1 a0 a1');
});

test('a new binding with the same key and count keeps page edits', () => {
    let s = twoFiles();
    s = M.commit(s, M.rotatePages(s.packet, s.table, [{ item: 'i1', index: 0 }], 90));
    s = M.expireStages(s, [s.table[HA].stage_id]);
    assert.equal(seq(s), 'b0 b1');
    const r = M.bindStage(s, HA, stage(HA, 3, { stage_id: 'AGAINagainAGAINagain00' }));
    assert.deepEqual(r.reset, []);
    assert.equal(seq(r.state), 'a0r90 a1 a2 b0 b1');
});

test('a different count or key resets the item in the packet and every snapshot', () => {
    let s = twoFiles();
    s = M.commit(s, M.deletePages(s.packet, s.table, [{ item: 'i1', index: 0 }]));
    s = M.commit(s, M.rotatePages(s.packet, s.table, [{ item: 'i1', index: 0 }], 180));
    s = M.undo(s);                      // the rotation is now in future
    const r = M.bindStage(s, HA, stage(HA, 6, { stage_id: 'SIXsixSIXsixSIXsix0000' }));
    assert.deepEqual(r.reset, ['a.pdf']);
    const all = [r.state.packet].concat(r.state.past, r.state.future);
    const holding = all.filter(p => M.findItem(p, 'i1'));
    assert.equal(holding.length, 3, 'the packet, one past snapshot and one future');
    holding.forEach(p => {
        const item = M.findItem(p, 'i1').item;
        assert.equal(item.pages, null);
        assert.equal(item.key, undefined);
    });
    assert.equal(seq(r.state), 'a0 a1 a2 a3 a4 a5 b0 b1');
    // A different key with the same count resets too.
    let t = twoFiles();
    t = M.commit(t, M.rotatePages(t.packet, t.table, [{ item: 'i2', index: 0 }], 90));
    const k = M.bindStage(t, HB, stage(HB, 2, { key: HB + ':reflow:a4', stage_id: 'KEYkeyKEYkeyKEYkey0000' }));
    assert.equal(M.findItem(k.state.packet, 'i2').item.pages, null);
});

test('a re-added file with a different hash becomes a new card', () => {
    let s = twoFiles();
    s = M.expireStages(s, [s.table[HA].stage_id]);
    assert.equal(M.rowStatus(s.table[HA], s.packet), 'expired');
    const found = M.findItem(s.packet, 'i1');
    const next = M.addFiles(s.packet, [{ hash: HC, label: 'a.pdf' }], { section: found.section.id, index: found.i + 1 });
    assert.deepEqual(M.newItemIds(s.packet, next), ['i3']);
    assert.deepEqual(next.sections[0].items.map(i => i.hash[0]), ['a', 'c', 'b']);
    // The expired card keeps its place, label and refs.
    assert.equal(M.findItem(next, 'i1').item.label, 'a.pdf');
});

// ---------------------------------------------------------------------------
// Paper
// ---------------------------------------------------------------------------

test('paper: one undo step, rows go stale and bind again, undo makes them stale again, PDFs never stale', () => {
    let s = twoFiles();
    s = M.bindStage(s, HC, stage(HC, 1, { key: HC + ':reflow:letter', type: 'docx' })).state;
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HC, label: 'c.docx' }]));
    s = M.commit(s, M.rotatePages(s.packet, s.table, [{ item: 'i3', index: 0 }], 90));
    const impact = M.paperImpact(s, 'a4');
    assert.deepEqual(impact, { files: 1, edited: 1 });
    const pastLen = s.past.length;
    s = M.commit(s, M.setPaper(s.packet, 'a4'));
    assert.equal(s.past.length, pastLen + 1);
    assert.deepEqual(M.staleHashes(s), [HC]);
    assert.equal(M.rowStatus(s.table[HA], s.packet), 'ready', 'a PDF never goes stale');
    assert.equal(seq(s), 'a0 a1 a2 b0 b1', 'a stale row gives no pages');
    s = M.bindStage(s, HC, stage(HC, 1, { key: HC + ':reflow:a4', type: 'docx', stage_id: 'A4a4A4a4A4a4A4a4A4a400' })).state;
    assert.deepEqual(M.staleHashes(s), []);
    assert.equal(seq(s), 'a0 a1 a2 b0 b1 c0', 'the edit on the old key was reset');
    s = M.undo(s);
    assert.equal(s.packet.options.paper, 'letter');
    assert.deepEqual(M.staleHashes(s), [HC]);
});

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

test('drafts serialise and restore per tab id', () => {
    const store = new MemoryStorage();
    const s = twoFiles();
    const table = M.setRow(s.table, HC, { status: 'uploading', name: 'c.pdf' });
    assert.ok(M.saveDraft(store, 'TABone', s.packet, table, 1000));
    assert.ok(M.saveDraft(store, 'TABtwo', M.emptyPacket('a4'), {}, 2000));
    const d = M.loadDraft(store, 'TABone');
    assert.deepEqual(d.packet, s.packet);
    assert.equal(d.table[HA].stage_id, s.table[HA].stage_id);
    assert.equal(d.table[HC].status, 'expired', 'a row with no stage comes back expired');
    assert.equal(M.loadDraft(store, 'nobody'), null);
    assert.equal(M.latestDraft(store, 'TABtwo').tabId, 'TABone');
});

test('a fresh tab restores from the draft alone, with live and expired stages', async () => {
    const server = createFakeServer();
    const store = new MemoryStorage();
    // Tab one stages two files and saves its draft.
    let one = M.emptyState('letter');
    const tabOne = 'TABoneTABoneTABone0001';
    const { io } = ioFor(server, tabOne, () => one, v => { one = v; });
    const fa = fileOf('a.pdf', 50, 1);
    const fb = fileOf('b.pdf', 50, 2);
    const ha = await M.sha256Hex(await fa.arrayBuffer(), crypto.subtle);
    const hb = await M.sha256Hex(await fb.arrayBuffer(), crypto.subtle);
    one = M.commit(one, M.addFiles(one.packet, [{ hash: ha, label: 'a.pdf' }, { hash: hb, label: 'b.pdf' }]));
    await C.stageWithRelief(io, ha, fa);
    await C.stageWithRelief(io, hb, fb);
    M.saveDraft(store, tabOne, one.packet, one.table, Date.now());
    server.expire(one.table[hb].stage_id);

    // A new tab finds it and restores it.
    const tabTwo = 'TABtwoTABtwoTABtwo0002';
    const latest = M.latestDraft(store, tabTwo);
    const r = M.restoreDraft(latest.draft);
    assert.equal(r.keep.length, 2);
    const answer = await C.postHold(server.fetch, tabTwo, r.keep, []);
    const two = M.applyHold(r.state, answer);
    assert.equal(M.rowStatus(two.table[ha], two.packet), 'ready');
    assert.equal(M.rowStatus(two.table[hb], two.packet), 'expired');
    assert.ok(server.entries.get(one.table[ha].stage_id).holders.has(tabTwo), 'keep added the new holder');
    assert.equal(two.past.length, 0, 'undo stacks are not saved');
});

test('a reload of the same tab releases the stages only its lost undo history named', async () => {
    // §3.2: a tab holds only stages its packet, past or future names. The
    // undo stacks aren't saved, so after a reload the stages only they named
    // must be released, or quota relief can never reach them.
    const server = createFakeServer({ capBytes: 2500 });
    const store = new MemoryStorage();
    const tab = 'TABsameTABsameTABsame1';
    let s = M.emptyState('letter');
    const { io } = ioFor(server, tab, () => s, v => { s = v; });
    const fa = fileOf('a.pdf', 1000, 1);
    const fb = fileOf('b.pdf', 1000, 2);
    const ha = await M.sha256Hex(await fa.arrayBuffer(), crypto.subtle);
    const hb = await M.sha256Hex(await fb.arrayBuffer(), crypto.subtle);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: ha, label: 'a.pdf' }, { hash: hb, label: 'b.pdf' }]));
    assert.equal(await C.stageWithRelief(io, ha, fa), 'ok');
    assert.equal(await C.stageWithRelief(io, hb, fb), 'ok');
    s = M.commit(s, M.removeItems(s.packet, ['i2']));
    assert.equal(M.releaseUnnamed(s).release.length, 0, 'b is undo-only, still held');
    M.saveDraft(store, tab, s.packet, s.table, Date.now());
    const sa = s.table[ha].stage_id;
    const sb = s.table[hb].stage_id;

    // Reload: the same tab id restores its own draft.
    const r = M.restoreDraft(M.loadDraft(store, tab));
    assert.deepEqual(Object.keys(r.state.table), [ha], 'the restored table holds only rows the packet names');
    assert.deepEqual(r.keep, [sa]);
    assert.deepEqual(r.release, [sb]);
    const answer = await C.postHold(server.fetch, tab, r.keep, r.release);
    s = M.applyHold(r.state, answer);
    assert.deepEqual(server.heldBy(tab), [sa], 'b is no longer held by this tab');

    // So a new file fits under the cap with no refusal.
    const fc = fileOf('c.pdf', 1000, 3);
    const hc = await M.sha256Hex(await fc.arrayBuffer(), crypto.subtle);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: hc, label: 'c.pdf' }]));
    const second = ioFor(server, tab, () => s, v => { s = v; });
    assert.equal(await C.stageWithRelief(second.io, hc, fc), 'ok');
    assert.equal(second.toasts.length, 0, 'no relief was needed');
});

test('restoring another tab\'s draft releases nothing it holds', async () => {
    const server = createFakeServer();
    const store = new MemoryStorage();
    const one = 'TABoneTABoneTABone0003';
    let s = M.emptyState('letter');
    const { io } = ioFor(server, one, () => s, v => { s = v; });
    const fa = fileOf('a.pdf', 50, 1);
    const fb = fileOf('b.pdf', 50, 2);
    const ha = await M.sha256Hex(await fa.arrayBuffer(), crypto.subtle);
    const hb = await M.sha256Hex(await fb.arrayBuffer(), crypto.subtle);
    s = M.commit(s, M.addFiles(s.packet, [{ hash: ha, label: 'a.pdf' }, { hash: hb, label: 'b.pdf' }]));
    await C.stageWithRelief(io, ha, fa);
    await C.stageWithRelief(io, hb, fb);
    s = M.commit(s, M.removeItems(s.packet, ['i2']));
    M.saveDraft(store, one, s.packet, s.table, Date.now());
    const two = 'TABtwoTABtwoTABtwo0004';
    const r = M.restoreDraft(M.latestDraft(store, two).draft);
    await C.postHold(server.fetch, two, r.keep, r.release);
    assert.ok(server.heldBy(one).includes(s.table[hb].stage_id), 'tab one keeps its undo-only stage');
    assert.deepEqual(server.heldBy(two), [s.table[ha].stage_id]);
});

test('old drafts are pruned at load, keeping the 10 newest', () => {
    const store = new MemoryStorage();
    const now = 100 * 3600 * 1000;
    M.saveDraft(store, 'old', M.emptyPacket(), {}, now - M.DRAFT_MAX_AGE_MS - 1);
    for (let i = 0; i < 12; i++) M.saveDraft(store, 't' + i, M.emptyPacket(), {}, now - i);
    store.setItem(M.DRAFT_PREFIX + 'junk', '{not json');
    store.setItem('docist.paper', 'a4');
    const removed = M.pruneDrafts(store, now);
    assert.deepEqual(removed.sort(), ['junk', 'old', 't10', 't11'].sort());
    assert.equal(M.listDrafts(store).length, 10);
    assert.equal(store.getItem('docist.paper'), 'a4', 'other keys are left alone');
});

test('drafts older than the maximum age are pruned even when fewer than 10 are kept', () => {
    const store = new MemoryStorage();
    const now = 100 * 3600 * 1000;
    const maxAge = 24 * 3600 * 1000;
    M.saveDraft(store, 'fresh', M.emptyPacket(), {}, now - 1000);
    M.saveDraft(store, 'edge', M.emptyPacket(), {}, now - maxAge);
    M.saveDraft(store, 'stale', M.emptyPacket(), {}, now - maxAge - 1);
    assert.deepEqual(M.pruneDrafts(store, now, maxAge, 10), ['stale']);
    assert.deepEqual(M.listDrafts(store).map(d => d.tabId), ['fresh', 'edge']);
});

test('a draft whose ids, rotations or stage ids are malformed is not used as given', () => {
    const s = twoFiles();
    const good = JSON.parse(M.serializeDraft(s.packet, s.table, 5));
    assert.ok(M.parseDraft(JSON.stringify(good)));
    const bad = (fn) => { const d = JSON.parse(JSON.stringify(good)); fn(d); return M.parseDraft(JSON.stringify(d)); };
    assert.equal(bad(d => { d.packet.sections[0].items[0].id = 'i1" onfocus="x'; }), null);
    assert.equal(bad(d => { d.packet.sections[0].id = '<s>'; }), null);
    assert.equal(bad(d => { d.packet.sections[0].items[0].kind = 'later-kind'; }), null);
    assert.equal(bad(d => { d.packet.options.paper = 'legal'; }), null);
    assert.equal(bad(d => {
        Object.assign(d.packet.sections[0].items[0], { key: HA + '::', src_pages: 3, pages: [{ src: 0, rot: 45 }] });
    }), null);
    const odd = bad(d => { d.table[HA].stage_id = '../../etc'; });
    assert.equal(odd.table[HA].stage_id, null);
    assert.equal(odd.table[HA].status, 'expired', 'a row with a malformed stage id comes back expired');
});

test('storage that throws is survived', () => {
    const bad = new ThrowingStorage();
    assert.equal(M.saveDraft(bad, 't', M.emptyPacket(), {}, 1), false);
    assert.equal(M.loadDraft(bad, 't'), null);
    assert.deepEqual(M.listDrafts(bad), []);
    assert.deepEqual(M.pruneDrafts(bad, 1), []);
    assert.equal(M.latestDraft(bad, 't'), null);
});

test('tab ids are 22 urlsafe characters', () => {
    const id = M.makeTabId(Uint8Array.from({ length: 22 }, (_, i) => i * 11));
    assert.ok(M.isTabId(id));
    assert.equal(id.length, 22);
    assert.ok(!M.isTabId('short'));
    assert.ok(!M.isTabId('has spaces in it, no no no'));
});

// ---------------------------------------------------------------------------
// Small client helpers
// ---------------------------------------------------------------------------

test('quota message, retry-after, status text and result name', () => {
    const body = Object.assign({}, API.endpoints.stage.responses.staging_quota.body);
    assert.match(C.quotaMessage(body), /^Over your staging limit of 200 MB\. This packet holds 180 MB\. Your other tabs, or other people on your network, hold 18 MB\./);
    assert.equal(C.retryAfterMs('3', 0), 3000);
    assert.equal(C.retryAfterMs(null, 2000), 2000);
    assert.equal(C.statusText('expired', {}, 'letter', false), 'Expired, re-add this file');
    assert.equal(C.statusText('stale', {}, 'a4', true), 'Converting again for A4…');
    assert.equal(C.statusText('stale', {}, 'a4', false), 'Re-add to use the new paper size');
    assert.equal(C.resultName(M.emptyPacket()), 'packet.pdf');
    const covered = M.insertGenerated(M.emptyPacket(), 'cover', null, { title: 'Application / J. Pluto' });
    assert.equal(C.resultName(covered), 'Application  J. Pluto.pdf');
    assert.equal(C.escapeHtml('<a href="x">'), '&lt;a href=&quot;x&quot;&gt;');
});

test('rangeKeys selects from the anchor to the target in view order', () => {
    const order = ['a', 'b', 'c', 'd'];
    assert.deepEqual(M.rangeKeys(order, 'd', 'b'), ['b', 'c', 'd']);
    assert.deepEqual(M.rangeKeys(order, null, 'c'), ['c']);
});

// ---------------------------------------------------------------------------
// Fix round (verification-b3.md)
// ---------------------------------------------------------------------------

// A drawn page, top to bottom (heights in px): toolbar 0-60, cover card
// 80-160, then four sections. Only file cards are in `cards`; the cover and
// divider cards never are, which is the point of M1.
function dropLayout() {
    return [
        // Exhibit A: heading 170-200, divider card 200-280, items from 300.
        { id: 's1', top: 170, bottom: 500, start: 300, count: 2, collapsed: false,
            cards: [{ index: 0, top: 300, bottom: 380 }, { index: 1, top: 400, bottom: 480 }] },
        // Exhibit B: heading 520-550, divider card 550-630, one card.
        { id: 's2', top: 520, bottom: 740, start: 650, count: 1, collapsed: false,
            cards: [{ index: 0, top: 650, bottom: 730 }] },
        // Collapsed: heading only, three items hidden.
        { id: 's3', top: 760, bottom: 790, start: 790, count: 3, collapsed: true, cards: [] },
        // Empty: heading 810-840, divider card 840-900, no items.
        { id: 's4', top: 810, bottom: 920, start: 910, count: 0, collapsed: false, cards: [] },
    ];
}

test('M1: every drop height lands in a section, whatever cards sit around it', () => {
    const layout = dropLayout();
    const at = y => C.dropTarget(y, layout).slot;
    assert.deepEqual(at(20), { section: 's1', index: 0 }, 'the toolbar is the start of the first section');
    assert.deepEqual(at(100), { section: 's1', index: 0 }, 'the cover card is the start of the first section');
    assert.deepEqual(at(185), { section: 's1', index: 0 }, 'a heading is the start of its section');
    assert.deepEqual(at(240), { section: 's1', index: 0 }, 'a divider card is the start of its section');
    assert.deepEqual(at(390), { section: 's1', index: 1 });
    assert.deepEqual(at(470), { section: 's1', index: 2 }, 'below the last card is the section end');
    assert.deepEqual(at(510), { section: 's2', index: 0 }, 'the gap before a section is its start');
    assert.deepEqual(at(600), { section: 's2', index: 0 }, "a later section's divider is its start");
    assert.deepEqual(at(775), { section: 's3', index: 0 }, 'a collapsed section takes the drop at its start');
    assert.deepEqual(at(870), { section: 's4', index: 0 }, 'an empty section takes the drop');
    assert.deepEqual(at(5000), { section: 's4', index: 0 }, 'below everything is the end of the last section');
    for (let y = -200; y < 1200; y += 7) {
        const r = C.dropTarget(y, layout);
        assert.ok(r.slot && layout.some(s => s.id === r.slot.section), 'y ' + y + ' places the drop');
        assert.ok(Number.isFinite(r.line), 'y ' + y + ' shows the insertion line');
    }
});

test('M1: the insertion line sits where the files will land', () => {
    const layout = dropLayout();
    assert.equal(C.dropTarget(100, layout).line, 300 - 12, 'above the first file card, below the divider');
    assert.equal(C.dropTarget(470, layout).line, 480 + 10, 'after the last card');
    assert.equal(C.dropTarget(870, layout).line, 910, 'at the top of an empty section');
    assert.equal(C.dropTarget(775, layout).line, 790, 'under a collapsed heading');
    assert.deepEqual(C.dropTarget(100, []), { slot: null, line: null, open: null }, 'no drawn sections: the end of the packet');
});

test('M1: a drop into a collapsed section opens it so the new cards show', () => {
    assert.equal(C.dropTarget(775, dropLayout()).open, 's3');
    assert.equal(C.dropTarget(390, dropLayout()).open, null);
});

test('m1: after Delete section, focus goes to the receiving section or the moved items', () => {
    let p = M.addSection(M.addSection(M.emptyPacket(), 'Two'), 'Three');
    p = M.addFiles(p, [{ hash: HA, label: 'a.pdf' }], { section: 's3', index: 0 });
    // Three sections left two: the receiving section's menu.
    const twoLeft = M.deleteSection(p, 's3');
    assert.equal(C.sectionDeleteFocus(twoLeft, p, 's3'), 's:s2:menu');
    // One section left: no headings, so the first item that moved.
    const pair = M.deleteSection(p, 's2');
    const one = M.deleteSection(pair, 's3');
    assert.equal(one.sections.length, 1);
    assert.equal(C.sectionDeleteFocus(one, pair, 's3'), 'c:' + p.sections[2].items[0].id + ':open');
    // One section left and nothing moved: the first card (null).
    const empty = M.addSection(M.emptyPacket(), 'Two');
    assert.equal(C.sectionDeleteFocus(M.deleteSection(empty, 's2'), empty, 's2'), null);
});

test('m2: the preview draws cover and divider text as the PDF prints it', () => {
    assert.equal(M.pdfText('申請書 Ünïcödé — “quotes” €'), '■■■ Ünïcödé — “quotes” €');
    assert.equal(M.pdfText('Ελληνικά'), 'Ελληνικ■', 'Greek prints through Symbol; the accented ά does not');
    assert.equal(M.pdfText('Łódź'), '■ód■');
    assert.equal(M.pdfText('Ölçü 😀 end'), 'Ölçü ■ end', 'one box per character, not per UTF-16 unit');
    assert.equal(M.pdfText('  a​b c d\t '), 'a b c d', 'what the server cleans to a space');
    assert.equal(M.pdfText('lone \ud800 surrogate'), 'lone surrogate');
    assert.equal(M.pdfText(null), '');
    assert.deepEqual(M.unprintable(['Łódź', 'Łuk', 'plain', '申']), ['Ł', 'ź', '申']);
    assert.deepEqual(M.unprintable(['Café “ok” – €5']), []);
});

test('n1: a draft with null or missing section and cover text restores as text', () => {
    const s = twoFiles();
    const good = JSON.parse(M.serializeDraft(s.packet, s.table, 5));
    good.packet.sections[0].title = null;
    delete good.packet.sections[0].divider;
    good.packet.cover = { title: 'T', subtitle: null };
    const back = M.parseDraft(JSON.stringify(good));
    assert.ok(back);
    assert.equal(back.packet.sections[0].title, '');
    assert.equal(back.packet.sections[0].divider, false);
    assert.deepEqual(back.packet.cover, { title: 'T', subtitle: '', date: '', note: '' });
    const typed = JSON.parse(JSON.stringify(good));
    typed.packet.sections[0].title = 5;
    assert.equal(M.parseDraft(JSON.stringify(typed)), null, 'a wrong type is still refused');
});

test('n4: a pending action runs once, early on flush or later on its own', () => {
    const ran = [];
    const pending = C.createPending();
    pending.set(() => ran.push('rename'));
    pending.flush();
    pending.flush();
    assert.deepEqual(ran, ['rename'], 'flush runs it once');
    pending.set(() => ran.push('first'));
    pending.set(() => ran.push('second'));
    assert.deepEqual(ran, ['rename', 'first'], 'a new action runs the one before it first');
    pending.flush();
    assert.deepEqual(ran, ['rename', 'first', 'second']);
});

// ---------------------------------------------------------------------------
// The contents page ([B5])
// ---------------------------------------------------------------------------

function contentsPacket(sections, options) {
    return {
        version: 1, title: '', cover: null,
        options: Object.assign(M.defaultOptions('letter'), { contents: true }, options || {}),
        sections: sections.map(([id, title, items, divider]) => ({ id, title, divider: !!divider, items })),
    };
}
const cFile = (id, hash, label) => ({ id, kind: 'file', hash, label: label == null ? id + '.pdf' : label, pages: null });

test('the contents length is its row count over 30, one page at least', () => {
    assert.equal(M.CONTENTS_ROWS, 30);
    for (let n = 0; n < 200; n++) assert.equal(M.contentsPageCount(n), Math.max(1, Math.ceil(n / 30)), String(n));
});

test('contents rows follow the heading rule: sections with files under them, or files alone', () => {
    const counts = { [HA]: 2, [HB]: 1 };
    const two = contentsPacket([['s1', 'Resume', [cFile('i1', HA, 'resume.docx'), { id: 'x', kind: 'blank' }], true],
        ['s2', '', [cFile('i2', HB, 'Łódź\n.pdf')]], ['s3', 'Empty', [], true]], { start_number: 4 });
    const plan = M.pagePlan(two, counts);
    assert.deepEqual(M.contentsRows(two, plan).map(r => [r.kind, r.title, r.label, r.index, r.indent, r.page]), [
        ['section', 'Resume', 4, 1, false, 0],
        ['item', 'resume.docx', 5, 2, true, 0],
        ['section', 'Untitled section', 8, 5, false, 0],
        ['item', '■ód■ .pdf', 8, 5, true, 0],
    ]);
    const one = contentsPacket([['s1', '', [cFile('i1', HA), cFile('i2', HB, '')]]]);
    assert.deepEqual(M.contentsRows(one, M.pagePlan(one, counts)).map(r => [r.kind, r.title, r.label, r.index, r.indent]), [
        ['item', 'i1.pdf', 1, 1, false], ['item', 'Untitled', 3, 3, false],
    ]);
});

test('the plan measures the contents from its rows unless a length is given', () => {
    const counts = {};
    const items = [];
    for (let k = 0; k < 31; k++) {
        const h = String(k).padStart(64, '0');
        counts[h] = 1;
        items.push(cFile('i' + k, h));
    }
    const flat = contentsPacket([['s1', '', items]]);
    const plan = M.pagePlan(flat, counts);
    assert.deepEqual(plan.pages.slice(0, 3).map(p => p.kind), ['contents', 'contents', 'file']);
    assert.equal(plan.total, 33);
    assert.equal(M.contentsRows(flat, plan)[30].page, 1);
    assert.equal(plan.entries[1].index, 2, 'links land after both contents pages');
    assert.equal(M.pagePlan(flat, counts, 1).total, 32, 'a given length wins');
    // 29 + 1 files and two headings: 32 rows, two pages.
    const sectioned = contentsPacket([['s1', 'A', items.slice(0, 29)], ['s2', 'B', items.slice(30)]]);
    assert.equal(M.pagePlan(sectioned, counts).pages.filter(p => p.kind === 'contents').length, 2);
    const vector = PLAN.vectors.find(v => v.name === 'contents-after-cover');
    const packet = JSON.parse(JSON.stringify(vector.packet));
    packet.options = Object.assign({}, PLAN.option_defaults, packet.options);
    assert.deepEqual(M.pagePlan(packet, vector.page_counts).pages.map(p => p.kind), ['cover', 'contents', 'file', 'file']);
    // With recto starts, the measured length moves the pads.
    const recto = contentsPacket([['s1', '', items]], { recto_starts: true });
    assert.deepEqual(M.pagePlan(recto, counts).pages.slice(0, 3).map(p => p.kind), ['contents', 'contents', 'file']);
    const coverRecto = Object.assign({}, recto, { cover: { title: 'T', subtitle: '', date: '', note: '' } });
    assert.deepEqual(M.pagePlan(coverRecto, counts).pages.slice(0, 5).map(p => p.kind), ['cover', 'pad', 'contents', 'contents', 'file']);
});

test('the contents option is one undo step and off leaves no contents page', () => {
    let s = twoFiles();
    const on = M.setOption(s.packet, 'contents', true);
    assert.notEqual(on, s.packet);
    assert.equal(s.packet.options.contents, false, 'the input is unchanged');
    s = M.commit(s, on);
    let plan = M.pagePlan(s.packet, M.readyCounts(s.table, s.packet));
    assert.equal(plan.pages[0].kind, 'contents');
    s = M.undo(s);
    plan = M.pagePlan(s.packet, M.readyCounts(s.table, s.packet));
    assert.ok(!plan.pages.some(p => p.kind === 'contents'));
});

test('renaming a file card cleans and caps its label, and empty keeps the old one', () => {
    const s = twoFiles();
    const id = s.packet.sections[0].items[0].id;
    const p = M.setLabel(s.packet, id, '  Exhibit\tA \n resume.pdf  ');
    assert.equal(M.findItem(p, id).item.label, 'Exhibit A resume.pdf');
    assert.equal(M.findItem(s.packet, id).item.label, 'a.pdf', 'the input is unchanged');
    assert.equal(M.setLabel(s.packet, id, '   '), s.packet);
    assert.equal(M.findItem(M.setLabel(s.packet, id, 'x'.repeat(500)), id).item.label.length, M.TEXT_MAX.label);
    const plan = M.pagePlan(Object.assign({}, p, { options: Object.assign({}, p.options, { contents: true }) }),
        M.readyCounts(s.table, p));
    assert.equal(M.contentsRows(p, plan)[0].title, 'Exhibit A resume.pdf');
});


// ---------------------------------------------------------------------------
// Page numbers on the page images ([B7], design §2.1, §3.3 rule 7)
// ---------------------------------------------------------------------------
PLAN.placement_vectors.forEach(v => {
    const name = 'placement vector ' + v.name + ' (model ' + v.stage.model + ')';
    if (!atOrBefore(v.stage.model)) {
        test(name, { skip: 'model tag ' + v.stage.model + ' is after ' + STAGE }, () => {});
        return;
    }
    test(name, () => {
        const got = M.numberPlacement(v.number_position, v.width);
        assert.ok(Math.abs(got.x - v.expect.x) < 1e-6, got.x + ' vs ' + v.expect.x);
        assert.equal(got.y, v.expect.y);
        assert.equal(got.align, v.expect.align);
    });
});

const LETTER_RUN = [1, 612, 792, 0, 612, 792];
// A state with file a (sizes given as runs) bound, plus optional items.
function sized(sizes, pages, items) {
    let s = M.emptyState('letter');
    s = M.commit(s, M.addFiles(s.packet, [{ hash: HA, label: 'a.pdf' }]));
    s = M.bindStage(s, HA, stage(HA, pages, sizes ? { sizes } : {})).state;
    (items || []).forEach(kind => {
        const sec = s.packet.sections[0];
        s = M.commit(s, M.insertGenerated(s.packet, kind, { section: sec.id, index: sec.items.length }));
    });
    return s;
}
function geometry(s, packet) {
    const p = packet || s.packet;
    const plan = M.pagePlan(p, M.readyCounts(s.table, p));
    return { plan, geo: M.pageGeometry(p, s.table, plan) };
}

test('binding keeps a stage answer\'s sizes, checked, and drops bad ones', () => {
    let s = sized([[2, 612, 792, 0, 612, 792], [1, 792, 612, 90, 792, 612]], 3);
    assert.deepEqual(s.table[HA].sizes, [[2, 612, 792, 0, 612, 792], [1, 792, 612, 90, 792, 612]]);
    const bad = [
        [[2, 612, 792, 0, 612, 792]],                 // counts short of pages
        [[3, 612, 792, 45.5, 612, 792]],              // rotate not an integer
        [[3, 612, 792, 360, 612, 792]],               // rotate not reduced
        [[3, 0, 792, 0, 612, 792]],                   // no width
        [[3, 612, 792, 0, 612]],                      // short run
        [[3, '612', 792, 0, 612, 792]],               // not a number
        'runs',
    ];
    bad.forEach(sizes => {
        s = M.bindStage(s, HA, stage(HA, 3, { sizes, stage_id: 'z'.repeat(22) })).state;
        assert.equal(s.table[HA].sizes, undefined, JSON.stringify(sizes));
    });
    // A good answer, then one without sizes (staged before B7): none kept.
    s = M.bindStage(s, HA, stage(HA, 3, { sizes: [[3, 612, 792, 0, 612, 792]] })).state;
    assert.ok(s.table[HA].sizes);
    s = M.bindStage(s, HA, stage(HA, 3)).state;
    assert.equal(s.table[HA].sizes, undefined);
    assert.equal(M.cleanSizes([[1, 612, 792, 0, 612, 792]], 1)[0].length, 6);
});

test('a file page\'s displayed size follows its own /Rotate and each user turn', () => {
    // Page 0 portrait, page 1 a crop of a landscape page that carries /Rotate 90.
    let s = sized([[1, 612, 792, 0, 612, 792], [1, 700, 500, 90, 792, 612]], 2);
    [0, 90, 180, 270].forEach(turn => {
        const t = s.table;
        let st = s;
        if (turn) st = Object.assign({}, s, { packet: M.rotatePages(s.packet, t, [{ item: s.packet.sections[0].items[0].id, index: 0 }, { item: s.packet.sections[0].items[0].id, index: 1 }], turn) });
        const { geo } = geometry(st);
        const q = turn % 180 !== 0;
        assert.deepEqual([geo[0].w, geo[0].h], q ? [792, 612] : [612, 792], 'portrait page at ' + turn);
        assert.deepEqual(geo[0].image, [612, 792]);
        assert.equal(geo[0].turn, turn);
        // Own /Rotate 90 shows the 700 x 500 crop as 500 x 700; a quarter user turn takes it back.
        assert.deepEqual(geo[1].image, [500, 700]);
        assert.deepEqual([geo[1].w, geo[1].h], q ? [700, 500] : [500, 700], 'rotated page at ' + turn);
    });
});

test('the stamp\'s quarter-turn test applies to the total rotation', () => {
    // /Rotate 45 is not a quarter turn, and neither is 45 + 90.
    let s = sized([[1, 612, 792, 45, 612, 792]], 1);
    const id = s.packet.sections[0].items[0].id;
    let { geo } = geometry(s);
    assert.deepEqual([geo[0].w, geo[0].h], [612, 792]);
    s = Object.assign({}, s, { packet: M.rotatePages(s.packet, s.table, [{ item: id, index: 0 }], 90) });
    ({ geo } = geometry(s));
    assert.deepEqual([geo[0].w, geo[0].h], [612, 792]);
    // 270 + 90 is a whole turn.
    s = sized([[1, 612, 792, 270, 612, 792]], 1);
    s = Object.assign({}, s, { packet: M.rotatePages(s.packet, s.table, [{ item: s.packet.sections[0].items[0].id, index: 0 }], 90) });
    ({ geo } = geometry(s));
    assert.deepEqual([geo[0].w, geo[0].h], [612, 792]);
    assert.equal(M.quarterTurn(-90), true);
    assert.equal(M.quarterTurn(450), true);
    assert.equal(M.quarterTurn(135), false);
});

test('a blank copies the media box and total rotation of the body page before it', () => {
    // A crop smaller than its media box: the blank takes the media box.
    let s = sized([[1, 400, 550, 0, 612, 792]], 1, ['blank']);
    let { plan, geo } = geometry(s);
    assert.deepEqual(plan.pages.map(p => p.kind), ['file', 'blank']);
    assert.deepEqual([geo[0].w, geo[0].h], [400, 550]);
    assert.deepEqual([geo[1].w, geo[1].h], [612, 792]);
    assert.equal(geo[1].image, undefined);
    // Turned 90 by the user: the blank is turned too, so it shows landscape.
    const id = s.packet.sections[0].items[0].id;
    s = Object.assign({}, s, { packet: M.rotatePages(s.packet, s.table, [{ item: id, index: 0 }], 90) });
    ({ geo } = geometry(s));
    assert.deepEqual([geo[1].w, geo[1].h], [792, 612]);
    assert.equal(geo[1].rotation % 360, 90);
});

test('a blank with no page before it takes the packet paper, portrait', () => {
    let s = M.emptyState('a4');
    s = M.commit(s, M.insertGenerated(s.packet, 'blank', { section: s.packet.sections[0].id, index: 0 }));
    s = M.commit(s, M.setCover(s.packet, { title: 'T' }));
    const { plan, geo } = geometry(s);
    assert.deepEqual(plan.pages.map(p => p.kind), ['cover', 'blank']);
    assert.deepEqual([geo[1].w, geo[1].h], M.PAPER_POINTS.a4);
    assert.deepEqual([geo[0].w, geo[0].h], M.PAPER_POINTS.a4);
});

test('a pad copies the page just before it, whatever its kind', () => {
    // a (landscape, 1 page) in section 1, recto starts: section 2 starts after a pad.
    let s = sized([[1, 792, 612, 0, 792, 612]], 1);
    s = M.commit(s, M.addSection(s.packet, 'Two'));
    s = M.commit(s, M.insertGenerated(s.packet, 'blank', { section: s.packet.sections[1].id, index: 0 }));
    s = M.commit(s, M.setOption(s.packet, 'recto_starts', true));
    let { plan, geo } = geometry(s);
    assert.deepEqual(plan.pages.map(p => p.kind), ['file', 'pad', 'blank']);
    assert.deepEqual([geo[1].w, geo[1].h], [792, 612]);
    // After a cover, a pad is the paper.
    s = M.commit(s, M.setCover(s.packet, { title: 'T' }));
    ({ plan, geo } = geometry(s));
    assert.deepEqual(plan.pages.map(p => p.kind), ['cover', 'pad', 'file', 'pad', 'blank']);
    assert.deepEqual([geo[1].w, geo[1].h], [612, 792]);
});

test('a row without sizes shows no number, nor does a blank that copies it', () => {
    const s = sized(null, 2, ['blank']);
    const { plan, geo } = geometry(s);
    assert.deepEqual(plan.pages.map(p => p.kind), ['file', 'file', 'blank']);
    assert.deepEqual(geo, [null, null, null]);
    assert.deepEqual(M.shownNumbers(plan, geo), [null, null, null]);
    assert.deepEqual(plan.pages.map(p => p.print), [true, true, true]);
});

test('which pages show a number for each option set', () => {
    let s = sized([[3, 612, 792, 0, 612, 792]], 3, ['blank']);
    const shown = st => { const { plan, geo } = geometry(st); return M.shownNumbers(plan, geo); };
    assert.deepEqual(shown(s), [1, 2, 3, 4]);
    s = M.commit(s, M.setOption(s.packet, 'start_number', 7));
    assert.deepEqual(shown(s), [7, 8, 9, 10]);
    s = M.commit(s, M.setCover(s.packet, { title: 'T' }));
    s = M.commit(s, M.setOption(s.packet, 'contents', true));
    assert.deepEqual(shown(s), [null, null, 7, 8, 9, 10], 'front matter shows none');
    // Two sections with dividers and recto starts: dividers and pads count
    // but print only with number_generated.
    s = M.commit(s, M.addSection(s.packet, 'Two'));
    const two = s.packet.sections[1].id;
    s = M.commit(s, M.insertGenerated(s.packet, 'blank', { section: two, index: 0 }));
    s = M.commit(s, M.addDividers(s.packet));
    s = M.commit(s, M.setOption(s.packet, 'recto_starts', true));
    const kinds = st => geometry(st).plan.pages.map(p => p.kind).join(' ');
    assert.equal(kinds(s), 'cover pad contents pad divider file file file blank pad divider blank');
    assert.deepEqual(shown(s), [null, null, null, null, null, 8, 9, 10, 11, null, null, 14]);
    s = M.commit(s, M.setOption(s.packet, 'number_generated', true));
    assert.deepEqual(shown(s), [null, null, null, null, 7, 8, 9, 10, 11, 12, 13, 14]);
    s = M.commit(s, M.setOption(s.packet, 'page_numbers', false));
    assert.ok(shown(s).every(n => n === null), 'numbers off shows none');
});

test('a draft keeps a bound row\'s sizes and drops bad ones', () => {
    const s = sized([[2, 612, 792, 0, 612, 792]], 2);
    const text = M.serializeDraft(s.packet, s.table, 5);
    assert.deepEqual(M.parseDraft(text).table[HA].sizes, [[2, 612, 792, 0, 612, 792]]);
    const d = JSON.parse(text);
    d.table[HA].sizes = [[9, 612, 792, 0, 612, 792]];
    assert.equal(M.parseDraft(JSON.stringify(d)).table[HA].sizes, undefined);
    d.table[HA].sizes = '<img>';
    assert.equal(M.parseDraft(JSON.stringify(d)).table[HA].sizes, undefined);
});

// The page script's layer: its box and the number's place, in hundredths of
// the sheet width.
const LETTER_RATIO = 792 / 612;
test('the number layer is the image\'s drawn box, and the number sits by rule 7', () => {
    // A Letter page on a Letter sheet fills it.
    const geo = { w: 612, h: 792, image: [612, 792], turn: 0 };
    let b = C.numberBox(geo, 'bottom-right', LETTER_RATIO);
    assert.ok(Math.abs(b.width - 100) < 1e-9 && Math.abs(b.height - 100 * LETTER_RATIO) < 1e-9);
    assert.ok(Math.abs(b.x - 562 / 612 * 100) < 1e-9);
    assert.ok(Math.abs(b.baseline - 30 / 612 * 100) < 1e-9);
    assert.ok(Math.abs(b.size - 10 / 612 * 100) < 1e-9);
    assert.equal(b.align, 'left');
    b = C.numberBox(geo, 'bottom-center', LETTER_RATIO);
    assert.equal(b.align, 'center');
    assert.ok(Math.abs(b.x - 50) < 1e-9);
    // A landscape page sits letterboxed in the Letter sheet: full width, 612/792 of it high.
    b = C.numberBox({ w: 792, h: 612, image: [792, 612], turn: 0 }, 'bottom-left', LETTER_RATIO);
    assert.ok(Math.abs(b.width - 100) < 1e-9 && Math.abs(b.height - 100 * 612 / 792) < 1e-9);
    assert.ok(Math.abs(b.x - 50 / 792 * 100) < 1e-9);
    // A portrait page turned 90: the sheet turns and shrinks to .77, so the
    // layer, upright, is the turned box at that scale.
    b = C.numberBox({ w: 792, h: 612, image: [612, 792], turn: 90 }, 'bottom-right', LETTER_RATIO);
    assert.equal(b.turn, 90);
    assert.ok(Math.abs(b.width - 100 * LETTER_RATIO * 0.77) < 1e-9);
    assert.ok(Math.abs(b.height - 100 * 0.77) < 1e-9);
    assert.ok(Math.abs(b.x - 742 / 792 * b.width) < 1e-9);
    // A drawn blank's sheet is its own shape.
    b = C.numberBox({ w: 792, h: 612 }, 'bottom-center', 612 / 792);
    assert.ok(Math.abs(b.width - 100) < 1e-9 && Math.abs(b.height - 100 * 612 / 792) < 1e-9);
});

test('the layer stays upright: it undoes the sheet\'s turn and scale', () => {
    const html = C.numberLayer({ w: 792, h: 612, image: [612, 792], turn: 270 }, 12, 'bottom-center', LETTER_RATIO, false);
    assert.match(html, /transform:translate\(-50%,-50%\) rotate\(-270deg\) scale\(1\.2987\)/);
    assert.match(html, /<text y="1" text-anchor="middle">12</);
    assert.match(html, /font-size:1\.2582cqw/, 'to scale in the pop-up');
    const flat = C.numberLayer({ w: 612, h: 792, image: [612, 792], turn: 180 }, 3, 'bottom-left', LETTER_RATIO, false);
    assert.match(flat, /rotate\(-180deg\)"/);
    assert.doesNotMatch(flat, /scale\(/);
    assert.equal(C.numberLayer(null, 3, 'bottom-left', LETTER_RATIO, false), '');
    assert.equal(C.numberLayer({ w: 612, h: 792 }, null, 'bottom-left', LETTER_RATIO, false), '');
});

test('on small images the number is at least 9 px and ends inside the image', () => {
    const geo = { w: 612, h: 792, image: [612, 792], turn: 0 };
    const right = C.numberLayer(geo, 123, 'bottom-right', LETTER_RATIO, true);
    assert.match(right, /font-size:max\(9px, 1\.634cqw\)/);
    assert.match(right, /left:min\(91\.8301cqw, calc\(100cqw - 1\.668em - 1px\)\)/);
    const centre = C.numberLayer(geo, 123, 'bottom-center', LETTER_RATIO, true);
    assert.match(centre, /left:50cqw;/);
    // The pop-up is never shifted.
    assert.match(C.numberLayer(geo, 123, 'bottom-right', LETTER_RATIO, false), /left:91\.8301cqw;/);
});

test('the too-large tile is told apart by its PNG text chunk', () => {
    const bytes = s => Uint8Array.from(Array.from(s, c => c.charCodeAt(0)));
    assert.equal(C.isPlaceholderPng(bytes('\x89PNG\r\n\x1a\n....tEXtdocist\x00placeholder....IDAT')), true);
    assert.equal(C.isPlaceholderPng(bytes('\x89PNG\r\n\x1a\n....IDAT docist placeholder')), false);
    assert.equal(C.isPlaceholderPng(new Uint8Array(0)), false);
});
