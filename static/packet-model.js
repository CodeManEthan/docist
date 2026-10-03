// ---------------------------------------------------------------------------
// Docist packet builder: the client model (design.md §3)
//
// Pure functions only. The page script (static/packet.js) holds one state
// object and changes it only through these, then re-renders:
//
//   state = { packet, past, future, table }
//
// - packet: the JSON document sent to /packet/build (§3.1). File items name
//   their file by the SHA-256 of its bytes, never by stage id.
// - past / future: snapshot undo stacks of earlier packets, capped at 100.
// - table: the binding table, one row per hash: the bound stage (stage_id,
//   key, page count, bytes), the file's name and type, and a status. Rows
//   change outside the undo history and never change the packet, except
//   that binding a new stage resets page edits that no longer fit (§3.1).
//
// Every function takes its inputs and returns new values; nothing here
// mutates an argument or touches the DOM, storage or the network. Storage
// helpers take the storage object as an argument and swallow its errors,
// as static/paper.js does. A CommonJS export guard at the bottom exposes
// everything to node:test.
// ---------------------------------------------------------------------------

const UNDO_CAP = 100;
const PAPERS = ['letter', 'a4'];
const NUMBER_POSITIONS = ['bottom-right', 'bottom-center', 'bottom-left'];
const ROTATIONS = [0, 90, 180, 270];
const DRAFT_PREFIX = 'packet-draft:';
const DRAFTS_KEPT = 10;
const DRAFT_MAX_AGE_MS = 24 * 60 * 60 * 1000;   // DOCIST_STAGING_MAX_HOURS

// Row statuses kept in the table. 'stale' is never stored: a ready non-PDF
// row whose key has another paper reads as stale (rowStatus), so undoing a
// paper change makes rows stale again without touching the table.
const STATUSES = ['waiting', 'uploading', 'ready', 'failed', 'expired'];

// ============================ Small helpers ===============================

function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function sectionsOf(packet) {
    return packet.sections || [];
}

// [{section, item, s, i}] in packet order.
function itemOrder(packet) {
    const out = [];
    sectionsOf(packet).forEach((section, s) => {
        section.items.forEach((item, i) => out.push({ section, item, s, i }));
    });
    return out;
}

function findItem(packet, itemId) {
    for (const entry of itemOrder(packet)) {
        if (entry.item.id === itemId) return entry;
    }
    return null;
}

function findSection(packet, sectionId) {
    const s = sectionsOf(packet).findIndex(sec => sec.id === sectionId);
    return s === -1 ? null : { section: packet.sections[s], s };
}

// The next free id with this prefix ('i' for items, 's' for sections):
// one past the highest number in use in the packet.
function idMaker(packet, prefix) {
    let max = 0;
    const scan = id => {
        const m = typeof id === 'string' && id.startsWith(prefix) ? /^\D+(\d+)$/.exec(id) : null;
        if (m) max = Math.max(max, Number(m[1]));
    };
    sectionsOf(packet).forEach(sec => {
        scan(sec.id);
        sec.items.forEach(item => scan(item.id));
    });
    return () => prefix + (++max);
}

// Ids of items in `after` that weren't in `before` (for focusing new cards).
function newItemIds(before, after) {
    const old = new Set(itemOrder(before).map(e => e.item.id));
    return itemOrder(after).map(e => e.item.id).filter(id => !old.has(id));
}

// Lowercase extension without the dot, or ''.
function fileType(name) {
    const i = String(name).lastIndexOf('.');
    return i === -1 ? '' : String(name).slice(i + 1).toLowerCase();
}

// ============================ The packet ==================================

function defaultOptions(paper) {
    return {
        paper: PAPERS.includes(paper) ? paper : 'letter',
        page_numbers: true,
        number_position: 'bottom-right',
        start_number: 1,
        number_generated: false,
        bookmarks: true,
        contents: false,
        recto_starts: false,
    };
}

// A new packet: one untitled section, not shown as a heading (§2.1).
function emptyPacket(paper) {
    return {
        version: 1,
        title: '',
        options: defaultOptions(paper),
        cover: null,
        sections: [{ id: 's1', title: '', divider: false, items: [] }],
    };
}

function emptyState(paper) {
    return { packet: emptyPacket(paper), past: [], future: [], table: {} };
}

// Where a slot is: {section: id, index} is the gap before the index-th item
// of that section, counted without the items being moved. null means the end
// of the last section.
function resolveSlot(packet, at) {
    const sections = sectionsOf(packet);
    if (!at || at.section == null) {
        const last = sections[sections.length - 1];
        return { s: sections.length - 1, index: last.items.length };
    }
    const found = findSection(packet, at.section);
    if (!found) return resolveSlot(packet, null);
    const n = found.section.items.length;
    const index = at.index == null ? n : Math.max(0, Math.min(n, at.index));
    return { s: found.s, index };
}

// Add file items at a slot. `files` is [{hash, label}]. Each becomes a file
// item with all pages of its stage (pages: null), in the packet from the drop.
function addFiles(packet, files, at) {
    if (!files || !files.length) return packet;
    const next = clone(packet);
    const id = idMaker(next, 'i');
    const slot = resolveSlot(next, at);
    const items = files.map(f => ({
        id: id(), kind: 'file', hash: f.hash, label: String(f.label || 'file'), pages: null,
    }));
    next.sections[slot.s].items.splice(slot.index, 0, ...items);
    return next;
}

// Insert a generated page or flag. 'blank' is an item at the slot. 'divider'
// turns the slot's section divider on. 'cover' sets the packet's one cover
// (the plan always puts it first, §3.3).
function insertGenerated(packet, kind, at, fields) {
    const next = clone(packet);
    if (kind === 'blank') {
        const slot = resolveSlot(next, at);
        next.sections[slot.s].items.splice(slot.index, 0, { id: idMaker(next, 'i')(), kind: 'blank' });
        return next;
    }
    if (kind === 'divider') {
        const slot = resolveSlot(next, at);
        if (next.sections[slot.s].divider) return packet;
        next.sections[slot.s].divider = true;
        return next;
    }
    if (kind === 'cover') {
        const f = fields || {};
        next.cover = {
            title: String(f.title || ''), subtitle: String(f.subtitle || ''),
            date: String(f.date || ''), note: String(f.note || ''),
        };
        return next;
    }
    return packet;
}

function removeItems(packet, itemIds) {
    const drop = new Set(itemIds);
    if (!itemOrder(packet).some(e => drop.has(e.item.id))) return packet;
    const next = clone(packet);
    next.sections.forEach(sec => { sec.items = sec.items.filter(item => !drop.has(item.id)); });
    return next;
}

// Every item gone, sections reset to one; options and cover kept.
function clearPacket(packet) {
    if (!itemOrder(packet).length && sectionsOf(packet).length === 1) return packet;
    const next = clone(packet);
    next.sections = [{ id: next.sections[0].id, title: '', divider: false, items: [] }];
    return next;
}

// Copies of whole items, each right after its original.
function duplicateItems(packet, itemIds) {
    const want = new Set(itemIds);
    if (!want.size) return packet;
    const next = clone(packet);
    const id = idMaker(next, 'i');
    let changed = false;
    next.sections.forEach(sec => {
        const out = [];
        sec.items.forEach(item => {
            out.push(item);
            if (want.has(item.id)) {
                out.push(Object.assign(clone(item), { id: id() }));
                changed = true;
            }
        });
        sec.items = out;
    });
    return changed ? next : packet;
}

function setLabel(packet, itemId, label) {
    const found = findItem(packet, itemId);
    const text = String(label || '').trim();
    if (!found || found.item.kind !== 'file' || !text || found.item.label === text) return packet;
    const next = clone(packet);
    findItem(next, itemId).item.label = text;
    return next;
}

// Move whole items to a slot, keeping their relative packet order. The slot
// index counts the target section's items without the moved ones.
function moveItems(packet, itemIds, at) {
    const want = new Set(itemIds);
    const moved = itemOrder(packet).filter(e => want.has(e.item.id)).map(e => e.item);
    if (!moved.length) return packet;
    const next = clone(packet);
    next.sections.forEach(sec => { sec.items = sec.items.filter(item => !want.has(item.id)); });
    const slot = resolveSlot(next, at);
    next.sections[slot.s].items.splice(slot.index, 0, ...clone(moved));
    return JSON.stringify(next) === JSON.stringify(packet) ? packet : next;
}

function moveItem(packet, itemId, at) {
    return moveItems(packet, [itemId], at);
}

// ---- Sections (the model has them all; the section UI is [B3]) ----

function addSection(packet, title, index) {
    const next = clone(packet);
    const section = { id: idMaker(next, 's')(), title: String(title || 'New section'), divider: false, items: [] };
    const at = index == null ? next.sections.length : Math.max(0, Math.min(next.sections.length, index));
    next.sections.splice(at, 0, section);
    return next;
}

function renameSection(packet, sectionId, title) {
    const found = findSection(packet, sectionId);
    const text = String(title || '').trim();
    if (!found || found.section.title === text) return packet;
    const next = clone(packet);
    next.sections[found.s].title = text;
    return next;
}

// Delete a section; its items move to the end of the previous section (or
// the start of the next one when it is the first).
function deleteSection(packet, sectionId) {
    const found = findSection(packet, sectionId);
    if (!found || packet.sections.length < 2) return packet;
    const next = clone(packet);
    const [gone] = next.sections.splice(found.s, 1);
    if (found.s > 0) next.sections[found.s - 1].items.push(...gone.items);
    else next.sections[0].items.unshift(...gone.items);
    return next;
}

function moveSection(packet, sectionId, index) {
    const found = findSection(packet, sectionId);
    if (!found) return packet;
    const to = Math.max(0, Math.min(packet.sections.length - 1, index));
    if (to === found.s) return packet;
    const next = clone(packet);
    const [sec] = next.sections.splice(found.s, 1);
    next.sections.splice(to, 0, sec);
    return next;
}

// ---- Options ----

function setOption(packet, name, value) {
    if (!(name in packet.options) || name === 'paper') return packet;
    let v = value;
    if (name === 'number_position' && !NUMBER_POSITIONS.includes(v)) return packet;
    if (name === 'start_number') {
        v = parseInt(v, 10);
        if (!Number.isFinite(v) || v < 1) v = 1;
    } else if (name !== 'number_position') {
        v = !!v;
    }
    if (packet.options[name] === v) return packet;
    const next = clone(packet);
    next.options[name] = v;
    return next;
}

function toggleOption(packet, name) {
    return setOption(packet, name, !packet.options[name]);
}

// Change the paper: one undo step on options alone (§3.1). Rows that no
// longer match read as stale (rowStatus) and packet.js stages them again.
function setPaper(packet, paper) {
    if (!PAPERS.includes(paper) || packet.options.paper === paper) return packet;
    const next = clone(packet);
    next.options.paper = paper;
    return next;
}

// ============================ Pages =======================================

// The refs an item gives now: its explicit refs, or all pages of a ready
// stage for pages: null, or none. `counts` maps hash to the page count of a
// ready row (readyCounts).
function itemRefs(item, counts) {
    if (item.kind !== 'file') return [];
    const n = counts[item.hash];
    if (n == null) return [];
    if (item.pages) return item.pages;
    return Array.from({ length: n }, (_, i) => ({ src: i, rot: 0 }));
}

// Page-level edits need explicit refs: write them with the bound stage's key
// and page count (§3.1). `rows` is the table.
function materialize(item, rows) {
    if (item.kind !== 'file' || item.pages) return item;
    const row = rows[item.hash];
    if (!row || !row.stage_id || row.pages == null) return item;
    return Object.assign({}, item, {
        key: row.key, src_pages: row.pages,
        pages: Array.from({ length: row.pages }, (_, i) => ({ src: i, rot: 0 })),
    });
}

// Normalise page targets [{item, index}] to a map itemId -> Set(index),
// dropping targets that don't exist.
function targetMap(packet, table, targets) {
    const counts = readyCounts(table, packet);
    const map = new Map();
    (targets || []).forEach(t => {
        const found = findItem(packet, t.item);
        if (!found) return;
        const n = found.item.kind === 'blank' ? 1 : itemRefs(found.item, counts).length;
        if (!(t.index >= 0 && t.index < n)) return;
        if (!map.has(t.item)) map.set(t.item, new Set());
        map.get(t.item).add(t.index);
    });
    return map;
}

// Apply `fn(refs, indexes) -> refs` to every targeted file item, after
// materialising it. Items left with no refs are removed. Targets on a blank
// item act on the whole item when `blankFn` returns a list in its place.
function editPages(packet, table, targets, fn, blankFn) {
    const map = targetMap(packet, table, targets);
    if (!map.size) return packet;
    const next = clone(packet);
    const id = idMaker(next, 'i');
    next.sections.forEach(sec => {
        const out = [];
        sec.items.forEach(item => {
            const idx = map.get(item.id);
            if (!idx) { out.push(item); return; }
            if (item.kind === 'blank') {
                out.push(...(blankFn ? blankFn(item, id) : [item]));
                return;
            }
            const m = materialize(item, table);
            const pages = fn(m.pages.slice(), idx);
            if (pages.length) out.push(Object.assign({}, m, { pages }));
        });
        sec.items = out;
    });
    return next;
}

function rotatePages(packet, table, targets, delta) {
    const d = ((Number(delta) % 360) + 360) % 360;
    if (!ROTATIONS.includes(d) || d === 0) return packet;
    return editPages(packet, table, targets,
        (refs, idx) => refs.map((r, i) => idx.has(i) ? { src: r.src, rot: (r.rot + d) % 360 } : r));
}

function deletePages(packet, table, targets) {
    return editPages(packet, table, targets,
        (refs, idx) => refs.filter((_, i) => !idx.has(i)),
        () => []);
}

// Each targeted page is copied right after itself.
function duplicatePages(packet, table, targets) {
    return editPages(packet, table, targets,
        (refs, idx) => refs.flatMap((r, i) => idx.has(i) ? [r, { src: r.src, rot: r.rot }] : [r]),
        (item, id) => [item, { id: id(), kind: 'blank' }]);
}

// Rotate every page of whole items (bulk rotate on selected cards).
function rotateItems(packet, table, itemIds, delta) {
    const counts = readyCounts(table, packet);
    const targets = [];
    itemOrder(packet).forEach(({ item }) => {
        if (!itemIds.includes(item.id) || item.kind !== 'file') return;
        itemRefs(item, counts).forEach((_, index) => targets.push({ item: item.id, index }));
    });
    return rotatePages(packet, table, targets, delta);
}

// Adjacent explicit runs of one file with the same label, key and page
// count read as one item again after a move puts them side by side.
// The joined item keeps an id from before the move (`known`) when one of
// the two had it, so focus and expanded state follow it.
function mergeRuns(items, known) {
    const out = [];
    items.forEach(item => {
        const prev = out[out.length - 1];
        if (prev && prev.kind === 'file' && item.kind === 'file' && prev.pages && item.pages
            && prev.hash === item.hash && prev.label === item.label
            && prev.key === item.key && prev.src_pages === item.src_pages) {
            const id = known && !known.has(prev.id) && known.has(item.id) ? item.id : prev.id;
            out[out.length - 1] = Object.assign({}, prev, { id, pages: prev.pages.concat(item.pages) });
        } else {
            out.push(item);
        }
    });
    return out;
}

// Move pages, keeping their packet order. `dest` is one of:
//   {item, index}     into that item's run, before its index-th page counted
//                     without the moved pages. A page of another file splits
//                     the run into two items with the moved run between.
//   {section, index}  a gap between items: the moved pages become runs of
//                     their own there (index counts items as resolveSlot).
// Items emptied by the move are removed; the destination survives even if
// every one of its own pages moved.
function movePages(packet, table, targets, dest) {
    const map = targetMap(packet, table, targets);
    if (!map.size || !dest) return packet;
    const next = clone(packet);
    const id = idMaker(next, 'i');
    const MARK = { mark: true };

    // 1. Lift the moved pages out, in packet order, each with its source
    //    item's hash, label, key and page count.
    const moved = [];
    next.sections.forEach(sec => {
        sec.items = sec.items.map(item => {
            const idx = map.get(item.id);
            if (!idx) return item;
            if (item.kind === 'blank') {
                moved.push({ blank: true });
                return Object.assign({}, item, { gone: true });
            }
            const m = materialize(item, table);
            const keep = [];
            m.pages.forEach((ref, i) => {
                if (idx.has(i)) moved.push({ hash: m.hash, label: m.label, key: m.key, src_pages: m.src_pages, ref });
                else keep.push(ref);
            });
            return Object.assign({}, m, { pages: keep });
        });
    });

    // 2. Runs of consecutive moved pages from one file become one item.
    const runs = [];
    moved.forEach(p => {
        const last = runs[runs.length - 1];
        if (p.blank) { runs.push({ id: id(), kind: 'blank' }); return; }
        if (last && last.kind === 'file' && last.hash === p.hash && last.label === p.label
            && last.key === p.key && last.src_pages === p.src_pages) {
            last.pages.push(p.ref);
            return;
        }
        runs.push({ id: id(), kind: 'file', hash: p.hash, label: p.label, key: p.key, src_pages: p.src_pages, pages: [p.ref] });
    });

    // 3. Place them.
    let placed = false;
    if (dest.item != null) {
        next.sections.forEach(sec => {
            const out = [];
            sec.items.forEach(item => {
                if (item.id !== dest.item || placed) { out.push(item); return; }
                placed = true;
                if (item.kind === 'blank' || item.gone || !materialize(item, table).pages) {
                    // A blank (or a card with no pages yet) takes the moved
                    // pages before it at index 0, else after it.
                    out.push(...(dest.index ? [item, ...runs] : [...runs, item]));
                    return;
                }
                const m = materialize(item, table);
                const refs = m.pages;
                const k = Math.max(0, Math.min(refs.length, dest.index == null ? refs.length : dest.index));
                // The part with pages keeps the item's id where it can.
                const before = Object.assign({}, m, { id: k > 0 ? m.id : id(), pages: refs.slice(0, k) });
                const after = Object.assign({}, m, { id: k > 0 ? id() : m.id, pages: refs.slice(k) });
                out.push(before, ...runs, after);
            });
            sec.items = out;
        });
    }
    if (!placed) {
        // A gap between items. Resolve it before emptied items are dropped,
        // counting only the items that keep pages, as the caller saw them.
        const slot = resolveSlot(next, dest.item != null ? null : dest);
        const sec = next.sections[slot.s];
        let seen = 0;
        let at = sec.items.length;
        for (let i = 0; i < sec.items.length; i++) {
            const item = sec.items[i];
            const empty = item.gone || (item.kind === 'file' && item.pages && !item.pages.length);
            if (empty) continue;
            if (seen === slot.index) { at = i; break; }
            seen++;
        }
        sec.items.splice(at, 0, MARK);
        sec.items = sec.items.flatMap(item => item === MARK ? runs : [item]);
    }

    // 4. Drop emptied items and rejoin runs that ended up side by side.
    const known = new Set(itemOrder(packet).map(e => e.item.id));
    next.sections.forEach(sec => {
        sec.items = mergeRuns(sec.items.filter(item =>
            !item.gone && !(item.kind === 'file' && item.pages && !item.pages.length)), known);
    });
    return JSON.stringify(next) === JSON.stringify(packet) ? packet : next;
}

// ---- Page order and step targets for the Move menus (§2.2) ----

// Every page the packet's items give, in order: [{item, index, section}].
// Blank items give one page. Generated flags (cover, dividers, padding) are
// not movable, so they are not here; pagePlan has them.
function pageOrder(packet, counts) {
    const out = [];
    sectionsOf(packet).forEach(sec => {
        sec.items.forEach(item => {
            const n = item.kind === 'blank' ? 1 : itemRefs(item, counts).length;
            for (let index = 0; index < n; index++) out.push({ item: item.id, index, section: sec.id });
        });
    });
    return out;
}

// Destination for an item's Move menu step: 'start', 'earlier', 'later',
// 'end'. Steps cross section boundaries. null when it can't move that way.
function itemStepDest(packet, itemId, where) {
    const found = findItem(packet, itemId);
    if (!found) return null;
    const sections = packet.sections;
    const { s, i } = found;
    const n = sections[s].items.length;
    if (where === 'start') {
        return s === 0 && i === 0 ? null : { section: sections[0].id, index: 0 };
    }
    if (where === 'end') {
        const last = sections.length - 1;
        return s === last && i === n - 1 ? null : { section: sections[last].id, index: null };
    }
    if (where === 'earlier') {
        if (i > 0) return { section: sections[s].id, index: i - 1 };
        return s > 0 ? { section: sections[s - 1].id, index: null } : null;
    }
    if (where === 'later') {
        if (i < n - 1) return { section: sections[s].id, index: i + 1 };
        return s < sections.length - 1 ? { section: sections[s + 1].id, index: 0 } : null;
    }
    return null;
}

// Destination for a page's Move menu step. Earlier and Later swap it with
// the page before or after it in the packet, even across files; Start and
// End go to the start or end of its own run.
function pageStepDest(packet, table, page, where) {
    const counts = readyCounts(table, packet);
    const order = pageOrder(packet, counts);
    const k = order.findIndex(p => p.item === page.item && p.index === page.index);
    if (k === -1) return null;
    const found = findItem(packet, page.item);
    if (found.item.kind === 'blank') {
        // A blank page is a whole item: move it as one.
        return null;
    }
    const n = itemRefs(found.item, counts).length;
    if (where === 'start') return page.index === 0 ? null : { item: page.item, index: 0 };
    if (where === 'end') return page.index === n - 1 ? null : { item: page.item, index: n - 1 };
    const other = order[where === 'earlier' ? k - 1 : k + 1];
    if (!other) return null;
    const target = findItem(packet, other.item).item;
    if (target.kind === 'blank') {
        // Hop over a blank: land in the gap on its far side. Gap indexes
        // count the items that keep pages, so a one-page source item before
        // the blank in its section no longer counts.
        const at = findItem(packet, other.item);
        if (where === 'earlier') return { section: at.section.id, index: at.i };
        const src = findItem(packet, page.item);
        const emptied = n === 1 && src.s === at.s && src.i < at.i;
        return { section: at.section.id, index: at.i + 1 - (emptied ? 1 : 0) };
    }
    // Index counted without the moved page: before `other` for earlier,
    // after it for later.
    let index = where === 'earlier' ? other.index : other.index + 1;
    if (other.item === page.item && page.index < index) index -= 1;
    return { item: other.item, index };
}

// Destination for "Move to position…" next to an item: before or after it,
// as a slot counted without the moved items. null when the target is moved.
function itemDestNear(packet, movedIds, targetId, after) {
    const moved = new Set(movedIds);
    const found = findItem(packet, targetId);
    if (!found || moved.has(targetId)) return null;
    const index = found.section.items.slice(0, found.i).filter(item => !moved.has(item.id)).length;
    return { section: found.section.id, index: index + (after ? 1 : 0) };
}

// Destination for "Move to position…" next to a page: before or after it in
// its run, counted without the moved pages. A blank target is one page.
function pageDestNear(packet, table, targets, target, after) {
    const map = targetMap(packet, table, targets);
    const own = map.get(target.item);
    if (own && own.has(target.index)) return null;
    const found = findItem(packet, target.item);
    if (!found) return null;
    if (found.item.kind === 'blank') return { item: target.item, index: after ? 1 : 0 };
    let index = target.index;
    if (own) own.forEach(i => { if (i < target.index) index--; });
    return { item: target.item, index: index + (after ? 1 : 0) };
}

// Where the first moved page lands in pageOrder after movePages, so the page
// script can keep focus on it. Counts the pages that stay, before the dest.
function movedFirstIndex(packet, table, targets, dest) {
    const counts = readyCounts(table, packet);
    const map = targetMap(packet, table, targets);
    const stays = p => !(map.get(p.item) && map.get(p.item).has(p.index));
    const order = pageOrder(packet, counts);
    if (dest.item != null) {
        const found = findItem(packet, dest.item);
        if (found) {
            const items = itemOrder(packet);
            const stop = items.findIndex(e => e.item.id === dest.item);
            const ahead = new Set(items.slice(0, stop).map(e => e.item.id));
            const before = order.filter(p => ahead.has(p.item) && stays(p)).length;
            if (found.item.kind === 'blank') return before + (dest.index ? 1 : 0);
            const own = order.filter(p => p.item === dest.item && stays(p)).length;
            return before + Math.max(0, Math.min(own, dest.index == null ? own : dest.index));
        }
    }
    const slot = resolveSlot(packet, dest.item != null ? null : dest);
    const sec = packet.sections[slot.s];
    // Items the move doesn't empty, as movePages counts the slot.
    const keeps = item => {
        const own = order.filter(p => p.item === item.id);
        return !own.length || own.some(stays);
    };
    const anchor = sec.items.filter(keeps)[slot.index];
    const items = itemOrder(packet);
    const stop = anchor ? items.findIndex(e => e.item.id === anchor.id)
        : items.filter(e => e.s <= slot.s).length;
    const ahead = new Set(items.slice(0, stop).map(e => e.item.id));
    return order.filter(p => ahead.has(p.item) && stays(p)).length;
}

// ============================ Binding table ===============================

function keyParts(key) {
    const [hash = '', engine = '', paper = ''] = String(key || '').split(':');
    return { hash, engine, paper };
}

function isPdfKey(key) {
    const p = keyParts(key);
    return p.engine === '' && p.paper === '';
}

// A row's status as shown: the stored one, except that a bound non-PDF row
// converted on another paper than the packet's reads 'stale'.
function rowStatus(row, packet) {
    if (!row) return 'expired';
    if (row.status === 'ready' && row.key && !isPdfKey(row.key)
        && keyParts(row.key).paper !== packet.options.paper) return 'stale';
    return row.status;
}

// hash -> page count for every row that is ready now.
function readyCounts(table, packet) {
    const counts = {};
    Object.keys(table || {}).forEach(hash => {
        const row = table[hash];
        if (rowStatus(row, packet) === 'ready' && row.pages != null) counts[hash] = row.pages;
    });
    return counts;
}

// Set fields on one row (creating it), leaving the packet alone.
function setRow(table, hash, patch) {
    return Object.assign({}, table, { [hash]: Object.assign({}, table[hash] || {}, patch) });
}

// Hashes the packet's file items name.
function packetHashes(packet) {
    const out = new Set();
    itemOrder(packet).forEach(({ item }) => { if (item.kind === 'file') out.add(item.hash); });
    return out;
}

function namedHashes(state) {
    const out = packetHashes(state.packet);
    state.past.concat(state.future).forEach(p => packetHashes(p).forEach(h => out.add(h)));
    return out;
}

// Can the item's refs be used with this stage? (§3.1 Fit)
function refsFit(item, stage) {
    return !item.pages || (item.key === stage.key && item.src_pages === stage.pages);
}

// Reset every item of `hash` whose refs don't fit `stage`. Returns the
// packet unchanged when nothing needed it, plus the labels reset.
function fitPacket(packet, hash, stage) {
    const bad = itemOrder(packet).filter(({ item }) =>
        item.kind === 'file' && item.hash === hash && !refsFit(item, stage));
    if (!bad.length) return { packet, reset: [] };
    const next = clone(packet);
    const ids = new Set(bad.map(e => e.item.id));
    next.sections.forEach(sec => {
        sec.items = sec.items.map(item => {
            if (!ids.has(item.id)) return item;
            const copy = Object.assign({}, item, { pages: null });
            delete copy.key;
            delete copy.src_pages;
            return copy;
        });
    });
    return { packet: next, reset: bad.map(e => e.item.label) };
}

// Bind `hash` to a stage the server returned ({stage_id, pages, key, bytes,
// name, type}). Applies the fit rule to the packet and to every snapshot in
// past and future, so no reachable state holds refs that don't fit. Not an
// undo step. Returns {state, release, reset}: release is the old stage id
// if the hash was bound to another stage, reset the labels whose edits were
// dropped (for one toast).
function bindStage(state, hash, stage) {
    const old = state.table[hash];
    const release = old && old.stage_id && old.stage_id !== stage.stage_id ? [old.stage_id] : [];
    const table = setRow(state.table, hash, {
        stage_id: stage.stage_id, key: stage.key, pages: stage.pages, bytes: stage.bytes,
        name: stage.name || (old && old.name) || '', type: stage.type || (old && old.type) || '',
        status: 'ready', reason: '',
    });
    const now = fitPacket(state.packet, hash, stage);
    const reset = now.reset.slice();
    const fitAll = list => list.map(p => {
        const r = fitPacket(p, hash, stage);
        return r.packet;
    });
    return {
        state: { packet: now.packet, past: fitAll(state.past), future: fitAll(state.future), table },
        release,
        reset,
    };
}

// Stage ids the server says are gone: their rows turn expired. Items keep
// their places, labels and refs (§3.1 Restore).
function expireStages(state, stageIds) {
    const gone = new Set(stageIds);
    let table = state.table;
    Object.keys(table).forEach(hash => {
        if (table[hash].stage_id && gone.has(table[hash].stage_id)) {
            table = setRow(table, hash, { stage_id: null, status: 'expired', reason: '' });
        }
    });
    return table === state.table ? state : Object.assign({}, state, { table });
}

// ============================ Undo ========================================

// Record a change: push the current packet onto past (capped), clear
// future. A packet identical to the current one is no step at all.
function commit(state, packet) {
    if (packet === state.packet) return state;
    const past = state.past.concat([state.packet]);
    return Object.assign({}, state, {
        packet,
        past: past.length > UNDO_CAP ? past.slice(past.length - UNDO_CAP) : past,
        future: [],
    });
}

function undo(state) {
    if (!state.past.length) return state;
    return Object.assign({}, state, {
        packet: state.past[state.past.length - 1],
        past: state.past.slice(0, -1),
        future: [state.packet].concat(state.future),
    });
}

function redo(state) {
    if (!state.future.length) return state;
    return Object.assign({}, state, {
        packet: state.future[0],
        past: state.past.concat([state.packet]),
        future: state.future.slice(1),
    });
}

// Replace the packet without an undo step (live moves in grab mode; the
// drop commits the snapshot taken at pick-up).
function replacePacket(state, packet) {
    return packet === state.packet ? state : Object.assign({}, state, { packet });
}

// Grab-mode drop: one undo step from the packet at pick-up to now.
function commitFrom(state, snapshot) {
    if (snapshot === state.packet || JSON.stringify(snapshot) === JSON.stringify(state.packet)) {
        return replacePacket(state, snapshot);
    }
    return commit(Object.assign({}, state, { packet: snapshot }), state.packet);
}

// ============================ Holds (§3.2) ================================

// After a change: rows whose hash no packet, past or future names any more
// are dropped, and their stages released. Returns {state, release, hashes}.
function releaseUnnamed(state) {
    const named = namedHashes(state);
    const release = [];
    const hashes = [];
    const table = {};
    Object.keys(state.table).forEach(hash => {
        if (named.has(hash)) { table[hash] = state.table[hash]; return; }
        hashes.push(hash);
        if (state.table[hash].stage_id) release.push(state.table[hash].stage_id);
    });
    if (!hashes.length) return { state, release, hashes };
    return { state: Object.assign({}, state, { table }), release, hashes };
}

// Stage ids this tab holds: the bound stage of every hash the packet, past
// or future names.
function keepIds(state) {
    const named = namedHashes(state);
    return Object.keys(state.table)
        .filter(hash => named.has(hash) && state.table[hash].stage_id)
        .map(hash => state.table[hash].stage_id);
}

// Quota relief: let go of every stage only the undo history names. Their
// rows turn expired; the snapshots stay, so undoing a removal brings back an
// expired card that restores by hash. Returns {state, release}.
function releaseUndoOnly(state) {
    const live = packetHashes(state.packet);
    const release = [];
    let table = state.table;
    Object.keys(table).forEach(hash => {
        if (live.has(hash) || !table[hash].stage_id) return;
        release.push(table[hash].stage_id);
        table = setRow(table, hash, { stage_id: null, status: 'expired', reason: '' });
    });
    return { state: release.length ? Object.assign({}, state, { table }) : state, release };
}

// Bytes and files this packet holds, for the quota message.
function heldBytes(state) {
    const named = namedHashes(state);
    let bytes = 0;
    let files = 0;
    Object.keys(state.table).forEach(hash => {
        const row = state.table[hash];
        if (named.has(hash) && row.stage_id) { bytes += row.bytes || 0; files++; }
    });
    return { bytes, files };
}

// Apply a /packet/hold answer: ids in gone turn expired.
function applyHold(state, answer) {
    return expireStages(state, (answer && answer.gone) || []);
}

// ============================ Paper (§3.1) ================================

// Hashes whose bound row reads stale under the packet's paper.
function staleHashes(state) {
    return Object.keys(state.table).filter(hash =>
        rowStatus(state.table[hash], state.packet) === 'stale' && packetHashes(state.packet).has(hash));
}

// What changing to `paper` would do, for the options panel: how many files
// convert again and how many items with page edits on them may reset.
function paperImpact(state, paper) {
    const trial = Object.assign({}, state, { packet: setPaper(state.packet, paper) });
    const hashes = new Set(staleHashes(trial));
    const edited = itemOrder(state.packet).filter(({ item }) =>
        item.kind === 'file' && item.pages && hashes.has(item.hash)).length;
    return { files: hashes.size, edited };
}

// ============================ Page plan (§3.3) ============================

// The page plan: every physical page in order with its printed label, in
// the shape of the shared vectors (tests/fixtures/packet-plan.json), which
// pdf_ops/packet.py page_plan also runs. `counts` maps hash -> page count for
// ready rows (readyCounts); a file item whose hash has none gives no pages.
// `contentsPages` is the contents length when options.contents is on ([B5]
// measures it from the entry count; 1 until then). Returns
//   pages:    [{kind, label, print, section?, item?, hash?, src?, rot?, index?}]
//             kind is cover | contents | pad | divider | file | blank; label
//             is the number or null for front matter. File and blank pages
//             also carry `index`, the page's place in its item, for the page
//             script (the vectors don't list it).
//   entries:  [{kind: 'section', id, title, label, index}
//              | {kind: 'item', id, section, title, label, index}], each
//             planned section and each file item that gives a page, index
//             being the 0-based physical page it links to.
//   total:    physical page count.
//   sections: {id: {first, label}} and items: {id: {first, last, label,
//             count}}, with 1-based physical pages, for the outline.
function pagePlan(packet, counts, contentsPages) {
    const o = Object.assign(defaultOptions(), packet.options || {});
    const pages = [];
    const pad = () => pages.push({ kind: 'pad' });
    const toOdd = () => { if (o.recto_starts && pages.length % 2 === 1) pad(); };

    // Sections in the plan, each with its pages, before anything is placed.
    const planned = [];
    sectionsOf(packet).forEach(sec => {
        const body = [];
        sec.items.forEach(item => {
            if (item.kind === 'blank') {
                body.push({ kind: 'blank', section: sec.id, item: item.id, index: 0 });
                return;
            }
            itemRefs(item, counts || {}).forEach((ref, index) => body.push({
                kind: 'file', section: sec.id, item: item.id,
                hash: item.hash, src: ref.src, rot: ref.rot || 0, index,
            }));
        });
        if (body.length) planned.push({ sec, body });
    });

    if (packet.cover) pages.push({ kind: 'cover' });
    if (o.contents) {
        toOdd();
        const n = contentsPages == null ? 1 : Math.max(1, contentsPages);
        for (let i = 0; i < n; i++) pages.push({ kind: 'contents' });
    }

    let bodyStart = null;
    const entries = [];
    planned.forEach(({ sec, body }) => {
        toOdd();
        if (bodyStart === null) bodyStart = pages.length;
        entries.push({ kind: 'section', id: sec.id, title: sec.title || '', at: pages.length });
        if (sec.divider) pages.push({ kind: 'divider', section: sec.id });
        const seen = new Set();
        body.forEach(p => {
            const item = sec.items.find(it => it.id === p.item);
            if (p.kind === 'file' && !seen.has(p.item)) {
                seen.add(p.item);
                entries.push({ kind: 'item', id: p.item, section: sec.id, title: item.label, at: pages.length });
            }
            pages.push(p);
        });
    });

    const start = Number.isFinite(Number(o.start_number)) && Number(o.start_number) >= 1 ? Number(o.start_number) : 1;
    pages.forEach((p, i) => {
        p.label = bodyStart !== null && i >= bodyStart ? start + (i - bodyStart) : null;
        const generated = p.kind === 'divider' || p.kind === 'pad';
        p.print = !!(o.page_numbers && p.label !== null && (!generated || o.number_generated));
    });
    const outEntries = entries.map(e => {
        const out = e.kind === 'section'
            ? { kind: 'section', id: e.id, title: e.title }
            : { kind: 'item', id: e.id, section: e.section, title: e.title };
        out.label = pages[e.at].label;
        out.index = e.at;
        return out;
    });

    const sections = {};
    const items = {};
    pages.forEach((p, i) => {
        const physical = i + 1;
        if (p.section && !sections[p.section]) sections[p.section] = { first: physical, label: p.label };
        if (p.item) {
            if (!items[p.item]) items[p.item] = { first: physical, last: physical, label: p.label, count: 0 };
            items[p.item].last = physical;
            items[p.item].count++;
        }
    });
    return { pages, entries: outEntries, total: pages.length, sections, items };
}

// ============================ Selection ===================================

// Shift-click: every key from the anchor to the target in view order,
// inclusive. Unknown anchor means just the target.
function rangeKeys(order, anchor, target) {
    const a = order.indexOf(anchor);
    const b = order.indexOf(target);
    if (b === -1) return [];
    if (a === -1) return [target];
    return order.slice(Math.min(a, b), Math.max(a, b) + 1);
}

// ============================ Identity ====================================

// SHA-256 of a file's bytes as lowercase hex. `subtle` is crypto.subtle; it
// needs a secure context (HTTPS or localhost).
async function sha256Hex(buffer, subtle) {
    const digest = await subtle.digest('SHA-256', buffer);
    return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
}

// ============================ Drafts (§3.2) ===============================

function draftKey(tabId) {
    return DRAFT_PREFIX + tabId;
}

// What a draft keeps of the table: bound rows as they are; rows with no
// stage (waiting, uploading, failed) have lost their file with the page, so
// they come back expired and restore by hash.
function draftTable(table) {
    const out = {};
    Object.keys(table).forEach(hash => {
        const row = table[hash];
        out[hash] = row.stage_id
            ? { stage_id: row.stage_id, key: row.key, pages: row.pages, bytes: row.bytes, name: row.name, type: row.type, status: 'ready' }
            : { stage_id: null, key: row.key || '', pages: row.pages == null ? null : row.pages, bytes: row.bytes || 0, name: row.name || '', type: row.type || '', status: 'expired' };
    });
    return out;
}

function serializeDraft(packet, table, now) {
    return JSON.stringify({ v: 1, saved: now, packet, table: draftTable(table) });
}

function parseDraft(text) {
    try {
        const d = JSON.parse(text);
        if (!d || d.v !== 1 || !d.packet || !Array.isArray(d.packet.sections) || typeof d.saved !== 'number') return null;
        return { saved: d.saved, packet: d.packet, table: draftTable(d.table || {}) };
    } catch (e) {
        return null;
    }
}

function saveDraft(storage, tabId, packet, table, now) {
    try {
        storage.setItem(draftKey(tabId), serializeDraft(packet, table, now));
        return true;
    } catch (e) {
        return false;
    }
}

function loadDraft(storage, tabId) {
    try {
        const text = storage.getItem(draftKey(tabId));
        return text == null ? null : parseDraft(text);
    } catch (e) {
        return null;
    }
}

// Every draft in storage, newest first: [{tabId, saved, draft}].
function listDrafts(storage) {
    const out = [];
    try {
        for (let i = 0; i < storage.length; i++) {
            const key = storage.key(i);
            if (!key || !key.startsWith(DRAFT_PREFIX)) continue;
            const draft = parseDraft(storage.getItem(key));
            out.push({ tabId: key.slice(DRAFT_PREFIX.length), saved: draft ? draft.saved : -1, draft });
        }
    } catch (e) {
        return [];
    }
    return out.sort((a, b) => b.saved - a.saved);
}

// Delete drafts last saved longer ago than maxAge, unreadable ones, and all
// but the `keep` newest. Returns the tab ids removed.
function pruneDrafts(storage, now, maxAge, keep) {
    const age = maxAge == null ? DRAFT_MAX_AGE_MS : maxAge;
    const limit = keep == null ? DRAFTS_KEPT : keep;
    const removed = [];
    listDrafts(storage).forEach((d, i) => {
        if (d.draft && now - d.saved <= age && i < limit) return;
        try {
            storage.removeItem(draftKey(d.tabId));
            removed.push(d.tabId);
        } catch (e) {
            // Storage refused; the next load tries again.
        }
    });
    return removed;
}

// The most recent draft of another tab, for "Restore your last packet?".
function latestDraft(storage, exceptTabId) {
    const found = listDrafts(storage).find(d => d.draft && d.tabId !== exceptTabId);
    return found || null;
}

// Restore a draft into a fresh state. Returns {state, keep}: keep is the
// stage ids to hold under this tab (POST /packet/hold), every stage the
// draft's packet names; the answer's gone list then turns rows expired.
function restoreDraft(draft) {
    const named = packetHashes(draft.packet);
    const table = {};
    Object.keys(draft.table).forEach(hash => { if (named.has(hash)) table[hash] = draft.table[hash]; });
    const state = { packet: draft.packet, past: [], future: [], table };
    return { state, keep: keepIds(state) };
}

// A tab id: 22 characters of the urlsafe alphabet (the holder, §4.2).
function makeTabId(randomBytes) {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    return Array.from(randomBytes.slice(0, 22), b => alphabet[b & 63]).join('');
}

function isTabId(value) {
    return typeof value === 'string' && /^[A-Za-z0-9_-]{22,32}$/.test(value);
}

// ============================ Exports =====================================
// The page script reads window.PacketModel; node:test requires the file.
const PacketModelApi = {
    UNDO_CAP, PAPERS, NUMBER_POSITIONS, STATUSES, DRAFT_PREFIX, DRAFTS_KEPT,
    DRAFT_MAX_AGE_MS,
    clone, itemOrder, findItem, findSection, newItemIds, fileType,
    defaultOptions, emptyPacket, emptyState,
    addFiles, insertGenerated, removeItems, clearPacket, duplicateItems, setLabel,
    moveItems, moveItem, addSection, renameSection, deleteSection, moveSection,
    setOption, toggleOption, setPaper,
    itemRefs, materialize, rotatePages, deletePages, duplicatePages, rotateItems, movePages,
    pageOrder, itemStepDest, pageStepDest, itemDestNear, pageDestNear, movedFirstIndex,
    keyParts, isPdfKey, rowStatus, readyCounts, setRow, packetHashes, namedHashes,
    refsFit, bindStage, expireStages,
    commit, undo, redo, replacePacket, commitFrom,
    releaseUnnamed, keepIds, releaseUndoOnly, heldBytes, applyHold,
    staleHashes, paperImpact,
    pagePlan, rangeKeys, sha256Hex,
    draftKey, serializeDraft, parseDraft, saveDraft, loadDraft, listDrafts, pruneDrafts,
    latestDraft, restoreDraft, makeTabId, isTabId,
};

if (typeof window !== 'undefined') window.PacketModel = PacketModelApi;

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = PacketModelApi;
}
