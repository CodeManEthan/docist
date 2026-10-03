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

// This file's stage: it runs page-plan vectors whose model tag is at or
// before B2 in the fixture's stage order, and skips later ones by name.
const STAGE = 'B2';

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
    ['too_large', 'too_many_pages', 'empty', 'unsupported'].forEach(k => {
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
