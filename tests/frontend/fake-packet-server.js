'use strict';

// A fake of B1's packet endpoints for node:test (build note m-a). It answers
// /packet/stage, /packet/hold and /packet/build through a fetch function,
// with bodies shaped like tests/fixtures/packet-api.json, and keeps a
// per-subject staging cap so the quota-relief path can be driven end to end.
// Test-only: nothing fake ships in static/ or routes/.

const crypto = require('node:crypto');
const path = require('node:path');

const API = require(path.join(__dirname, '..', 'fixtures', 'packet-api.json'));

function json(status, body, headers) {
    return new Response(JSON.stringify(body), {
        status,
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {}),
    });
}

let counter = 0;
function stageId() {
    counter++;
    return ('S' + String(counter).padStart(4, '0') + 'x'.repeat(22)).slice(0, 22);
}

// options: capBytes, capFiles, pagesFor(name) -> page count, engine.
function createFakeServer(options) {
    const opts = Object.assign({ capBytes: Infinity, capFiles: Infinity, pagesFor: () => 2, engine: 'reflow' }, options);
    const entries = new Map();      // stage_id -> {key, pages, bytes, name, holders:Set, gone}
    const log = [];

    function live() {
        return Array.from(entries.entries()).filter(([, e]) => !e.gone && e.holders.size);
    }
    function used() {
        const l = live();
        return { bytes: l.reduce((a, [, e]) => a + e.bytes, 0), files: l.length };
    }
    function holderUse(holder) {
        const l = live().filter(([, e]) => e.holders.has(holder));
        return { bytes: l.reduce((a, [, e]) => a + e.bytes, 0), files: l.length };
    }

    async function stage(init) {
        const form = init.body;
        const file = form.get('file');
        const paper = form.get('paper');
        const holder = form.get('holder');
        const bytes = Buffer.from(await file.arrayBuffer());
        const hash = crypto.createHash('sha256').update(bytes).digest('hex');
        const type = path.extname(file.name).slice(1).toLowerCase();
        const key = type === 'pdf' ? hash + '::' : hash + ':' + opts.engine + ':' + paper;
        // Dedup: a live entry with the same key gains the holder.
        for (const [id, e] of live()) {
            if (e.key === key) {
                e.holders.add(holder);
                return json(200, { stage_id: id, pages: e.pages, name: e.name, type, key, bytes: e.bytes });
            }
        }
        const u = used();
        if (u.bytes + bytes.length > opts.capBytes || u.files + 1 > opts.capFiles) {
            const h = holderUse(holder);
            const body = Object.assign({}, API.endpoints.stage.responses.staging_quota.body, {
                cap_bytes: opts.capBytes, cap_files: opts.capFiles === Infinity ? 0 : opts.capFiles,
                subject_bytes: u.bytes, subject_files: u.files, holder_bytes: h.bytes, holder_files: h.files,
            });
            return json(400, body);
        }
        const id = stageId();
        const pages = opts.pagesFor(file.name);
        entries.set(id, { key, pages, bytes: bytes.length, name: file.name, holders: new Set([holder]), gone: false });
        return json(200, { stage_id: id, pages, name: file.name, type, key, bytes: bytes.length });
    }

    function hold(init) {
        const body = JSON.parse(init.body);
        const answer = { live: {}, gone: [] };
        (body.release || []).forEach(id => { const e = entries.get(id); if (e) e.holders.delete(body.holder); });
        (body.keep || []).forEach(id => {
            const e = entries.get(id);
            if (!e || e.gone) { answer.gone.push(id); return; }
            e.holders.add(body.holder);
            answer.live[id] = e.pages;
        });
        return json(200, answer);
    }

    function build(init) {
        const body = JSON.parse(init.body);
        const items = [];
        body.packet.sections.forEach(sec => sec.items.forEach(item => {
            if (item.kind !== 'file') return;
            const id = body.stages[item.hash];
            const e = entries.get(id);
            if (!e || e.gone || !e.holders.size) items.push({ id: item.id, code: 'expired' });
            else if (item.pages && (item.key !== e.key || item.src_pages !== e.pages)) items.push({ id: item.id, code: 'stale' });
        }));
        if (items.length) {
            return json(400, Object.assign({}, API.endpoints.build.responses.bad_items.body, { code: items[0].code, items }));
        }
        return json(200, API.endpoints.build.responses.ok.body, API.endpoints.build.responses.ok.headers);
    }

    async function fetch(url, init) {
        const u = String(url);
        log.push(u);
        if (u === '/packet/stage') return stage(init);
        if (u === '/packet/hold') return hold(init);
        if (u === '/packet/build') return build(init);
        return json(404, { error: 'Not found.', code: 'not_found' });
    }

    return {
        fetch,
        entries,
        log,
        expire(id) { const e = entries.get(id); if (e) e.gone = true; },
        heldBy(holder) { return live().filter(([, e]) => e.holders.has(holder)).map(([id]) => id); },
    };
}

module.exports = { createFakeServer, API };
