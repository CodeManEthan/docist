// ---------------------------------------------------------------------------
// Docist packet builder: the /packet page (design.md §2, §3, §4.3)
//
// Three parts, as in the other static scripts:
//   1. Client functions with no DOM: response classification, the request
//      helpers (each takes the fetch function, so node:test can hand them a
//      fake server), and the stage flow with quota relief (§3.2).
//   2. DOM wiring: one model state changed only through static/packet-model.js,
//      a full re-render after each change, SortableJS when it loaded and the
//      Move menus always.
//   3. The CommonJS export guard.
//
// The rule from §2.2 holds throughout: a key or click handler acts only when
// its own element is the event's target, and stops the event there.
// ---------------------------------------------------------------------------

/* global Sortable */
const PM = (typeof module !== 'undefined' && module.exports)
    ? require('./packet-model.js')
    : window.PacketModel;

const STAGE_URL = '/packet/stage';
const HOLD_URL = '/packet/hold';
const BUILD_URL = '/packet/build';
const HOLD_RENEW_MS = 5 * 60 * 1000;
const HOLD_MAX_IDS = 500;
const BUSY_RETRIES = 30;
const TOAST_MS = 8000;
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP = 8;
const CSRF_MESSAGE = 'Reload the page. Your arrangement is saved.';
const PK_MIB = 1024 * 1024;

// ============================ Client functions ============================

function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function formatMb(bytes) {
    const mb = bytes / PK_MIB;
    if (mb < 0.1) return Math.max(1, Math.round(bytes / 1024)) + ' KB';
    return (mb < 10 ? mb.toFixed(1) : String(Math.round(mb))) + ' MB';
}

function plural(n, one, many) {
    return n + ' ' + (n === 1 ? one : (many || one + 's'));
}

// Retry-After in milliseconds, or the fallback.
function retryAfterMs(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) && n >= 0 ? n * 1000 : fallback;
}

// What a response means for the page (packet-api.json). `body` is the parsed
// JSON or null. Kinds: ok, csrf, expired (any 404 on a staged id), busy (a
// 429 to retry: the lane's `busy` or the POST limit, which has no code),
// daily_limit, quota, full, too_large, bad_items, upload_too_large, rejected,
// error.
function classify(status, body) {
    const b = body || {};
    if (status >= 200 && status < 300) return { kind: 'ok' };
    if (status === 404) return { kind: 'expired' };
    if (status === 429) return { kind: b.code === 'daily_limit' ? 'daily_limit' : 'busy', error: b.error || '' };
    if (status === 413) return { kind: 'upload_too_large', error: b.error || 'This file is over the upload limit.' };
    if (status === 400) {
        if (!b.code && /^Session expired/.test(b.error || '')) return { kind: 'csrf' };
        // A build refusal that names items marks each card (packet-api.json
        // build_refusal); the rest are packet-wide or about one upload.
        if (Array.isArray(b.items) && b.items.length) return { kind: 'bad_items', error: b.error || '', code: b.code };
        const map = { staging_quota: 'quota', staging_full: 'full', too_large: 'too_large' };
        return { kind: map[b.code] || 'rejected', error: b.error || '', code: b.code || '' };
    }
    return { kind: 'error', error: b.error || '' };
}

// The card's text for a build refusal's item code.
const ITEM_CODE_TEXT = {
    expired: 'This file expired. Re-add it, then build again.',
    stale: 'Its page edits no longer fit the file. Re-add it.',
    out_of_range: 'A page of this file is missing. Re-add it.',
    unreadable: "Docist couldn't read this file. Re-add it, or remove it.",
    encrypted: 'This PDF needs a password. Remove the password, then re-add it.',
    invalid: "This item couldn't be used. Remove it and add it again.",
    too_large: 'This file is too large for a packet.',
};

function itemCodeText(code) {
    return ITEM_CODE_TEXT[code] || "This file couldn't be used.";
}

// The card's text for a quota refusal that remains after relief (§3.2).
function quotaMessage(body) {
    const b = body || {};
    const capMb = formatMb(b.cap_bytes || 0);
    const mine = b.holder_bytes || 0;
    const others = Math.max(0, (b.subject_bytes || 0) - mine);
    const files = b.cap_files && b.subject_files >= b.cap_files
        ? ' You can stage at most ' + b.cap_files + ' files at once.' : '';
    return 'Over your staging limit of ' + capMb + '. This packet holds ' + formatMb(mine)
        + '. Your other tabs, or other people on your network, hold ' + formatMb(others)
        + '.' + files + ' Close other Docist tabs or wait a while, then retry.';
}

async function readJson(response) {
    try {
        return await response.json();
    } catch (e) {
        return null;
    }
}

// POST one file to /packet/stage. Resolves {status, body, retryAfter};
// rejects only when the network fails.
async function stageFile(fetchFn, file, paper, holder) {
    const form = new FormData();
    form.append('file', file, file.name);
    form.append('paper', paper);
    form.append('holder', holder);
    const response = await fetchFn(STAGE_URL, { method: 'POST', body: form });
    return {
        status: response.status,
        body: await readJson(response),
        retryAfter: response.headers.get('Retry-After'),
    };
}

// POST /packet/hold, split so no request carries more than 500 ids. Resolves
// the merged {live, gone}, or null when a request failed.
async function postHold(fetchFn, holder, keep, release) {
    const ids = keep.map(id => ['keep', id]).concat(release.map(id => ['release', id]));
    if (!ids.length) return { live: {}, gone: [] };
    const answer = { live: {}, gone: [] };
    for (let i = 0; i < ids.length; i += HOLD_MAX_IDS) {
        const chunk = ids.slice(i, i + HOLD_MAX_IDS);
        const body = {
            holder,
            keep: chunk.filter(c => c[0] === 'keep').map(c => c[1]),
            release: chunk.filter(c => c[0] === 'release').map(c => c[1]),
        };
        let response;
        try {
            response = await fetchFn(HOLD_URL, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
        } catch (e) {
            return null;
        }
        const json = await readJson(response);
        if (!response.ok || !json) return null;
        Object.assign(answer.live, json.live || {});
        answer.gone.push(...(json.gone || []));
    }
    return answer;
}

// The binding table's ready rows that the packet names, as /packet/build
// wants them: {hash: stage_id}.
function buildStages(state) {
    const named = PM.packetHashes(state.packet);
    const out = {};
    Object.keys(state.table).forEach(hash => {
        if (named.has(hash) && PM.rowStatus(state.table[hash], state.packet) === 'ready') {
            out[hash] = state.table[hash].stage_id;
        }
    });
    return out;
}

// Can the packet be built now? {ok, reason}.
function buildReadiness(state, opsLeft) {
    const files = PM.itemOrder(state.packet).filter(e => e.item.kind === 'file');
    const notReady = files.filter(e => PM.rowStatus(state.table[e.item.hash], state.packet) !== 'ready');
    const plan = PM.pagePlan(state.packet, PM.readyCounts(state.table, state.packet));
    if (!files.length && !plan.total) return { ok: false, reason: 'Add files to build a packet.' };
    if (notReady.length) {
        return { ok: false, reason: plural(notReady.length, 'file') + ' not ready yet.' };
    }
    if (!plan.total) return { ok: false, reason: 'The packet has no pages.' };
    if (opsLeft === 0) return { ok: false, reason: 'No operations left today. Your arrangement is saved here.' };
    return { ok: true, reason: '' };
}

async function postBuild(fetchFn, state, holder) {
    const response = await fetchFn(BUILD_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ packet: state.packet, stages: buildStages(state), holder }),
    });
    return {
        status: response.status,
        body: await readJson(response),
        remaining: response.headers.get('X-Docist-Usage-Remaining'),
        retryAfter: response.headers.get('Retry-After'),
    };
}

// Stage one file and bind it, with quota relief (§3.2). `io` gives:
//   fetch, holder, update(fn) to change the page state, get() to read it,
//   release(ids) to POST the releases, toast(text), sleep(ms).
// Resolves the final classification kind ('ok', 'quota', ...).
async function stageWithRelief(io, hash, file) {
    let relieved = false;
    let busy = 0;
    for (;;) {
        io.update(s => Object.assign({}, s, { table: PM.setRow(s.table, hash, { status: 'uploading', reason: '' }) }));
        let res;
        try {
            res = await stageFile(io.fetch, file, io.get().packet.options.paper, io.holder);
        } catch (e) {
            io.update(s => Object.assign({}, s, {
                table: PM.setRow(s.table, hash, { status: 'failed', reason: 'network', message: 'Network lost while uploading.' }),
            }));
            return 'network';
        }
        const c = classify(res.status, res.body);
        if (c.kind === 'ok') {
            const stage = res.body || {};
            if (PM.keyParts(stage.key).hash !== hash) {
                io.update(s => Object.assign({}, s, {
                    table: PM.setRow(s.table, hash, { status: 'failed', reason: 'mismatch', message: 'The server read a different file. Try adding it again.' }),
                }));
                if (stage.stage_id) await io.release([stage.stage_id]);
                return 'mismatch';
            }
            // The tab may have dropped the file while it uploaded.
            if (!PM.namedHashes(io.get()).has(hash)) {
                await io.release([stage.stage_id]);
                return 'ok';
            }
            let result;
            io.update(s => {
                result = PM.bindStage(s, hash, stage);
                return result.state;
            });
            if (result.release.length) await io.release(result.release);
            if (result.reset.length) {
                io.toast(result.reset[0] + ' now has ' + plural(stage.pages, 'page')
                    + ', so its page edits were reset.');
            }
            return 'ok';
        }
        if (c.kind === 'quota' && !relieved) {
            relieved = true;
            let relief;
            io.update(s => {
                relief = PM.releaseUndoOnly(s);
                return relief.state;
            });
            if (relief.release.length) {
                await io.release(relief.release);
                io.toast('Made room by letting go of removed files. Undoing their removal will ask you to re-add them.');
                continue;
            }
        }
        if (c.kind === 'busy' && busy < BUSY_RETRIES) {
            busy++;
            io.update(s => Object.assign({}, s, { table: PM.setRow(s.table, hash, { status: 'waiting', reason: 'busy' }) }));
            await io.sleep(retryAfterMs(res.retryAfter, 2000));
            continue;
        }
        let message;
        if (c.kind === 'quota') message = quotaMessage(res.body);
        else if (c.kind === 'full') message = 'Docist is busy, try again later.';
        else if (c.kind === 'csrf') message = CSRF_MESSAGE;
        else if (c.kind === 'upload_too_large' || c.code === 'encrypted') message = c.error;
        else message = "Couldn't convert this file" + (c.error ? ': ' + c.error : '.');
        io.update(s => Object.assign({}, s, {
            table: PM.setRow(s.table, hash, { status: 'failed', reason: c.kind, message }),
        }));
        return c.kind;
    }
}

// The staging queue: one stage request in flight, matching the work lane
// (§4.3). `files` maps hash -> File for the files this page still has.
// `hooks`: blocked() is true once the session is lost; done(hash, kind)
// runs after each request.
//
// The rule (§3.1 Paper): when a stage request finishes, every row is checked
// against the paper the packet has now, and a row that reads stale is queued
// again while the page has its file. A request carries the paper of the
// moment it was sent, so a paper change while it is open binds the old
// paper; this check is what converts it again. A hash is staged at most once
// per paper, so a server that answers with the wrong paper can't loop.
function createStageQueue(io, files, hooks) {
    const waiting = [];
    const stagedFor = new Map();        // hash -> paper last staged for
    const h = hooks || {};
    let inFlight = false;
    function add(hash) {
        if (!waiting.includes(hash)) waiting.push(hash);
        return next();
    }
    async function next() {
        if (inFlight || (h.blocked && h.blocked())) return;
        const hash = waiting.shift();
        if (!hash) return;
        const file = files.get(hash);
        if (!file || !PM.namedHashes(io.get()).has(hash)) return next();
        inFlight = true;
        stagedFor.set(hash, io.get().packet.options.paper);
        let kind;
        try {
            kind = await stageWithRelief(io, hash, file);
        } finally {
            inFlight = false;
        }
        if (h.done) h.done(hash, kind);
        return restageStale();
    }
    // Queue every row that reads stale under the current paper.
    function restageStale() {
        const paper = io.get().packet.options.paper;
        PM.staleHashes(io.get()).forEach(hash => {
            if (files.has(hash) && stagedFor.get(hash) !== paper && !waiting.includes(hash)) waiting.push(hash);
        });
        return next();
    }
    return {
        enqueue: add,
        restageStale,
        // A hash the user re-added or retried may be staged again.
        forget: hash => { stagedFor.delete(hash); },
    };
}

// The text a card shows for its row (empty when ready).
function statusText(status, row, paper, haveFile) {
    switch (status) {
    case 'waiting': return row && row.reason === 'busy' ? 'Waiting…' : 'Waiting to upload…';
    case 'uploading': return 'Uploading and converting…';
    case 'failed': return (row && row.message) || "Couldn't convert this file.";
    case 'expired': return 'Expired, re-add this file';
    case 'stale': return haveFile ? 'Converting again for ' + (paper === 'a4' ? 'A4' : 'Letter') + '…'
        : 'Re-add to use the new paper size';
    default: return '';
    }
}

// The file name for the result: the cover title, else packet.pdf (§2.1).
function resultName(packet) {
    const title = packet.cover && packet.cover.title ? packet.cover.title : '';
    const safe = title.replace(/[^\w .-]+/g, '').trim().slice(0, 80);
    return (safe || 'packet') + '.pdf';
}

// ============================ Browser wiring ==============================

function initPacket() {
    const $ = sel => document.querySelector(sel);
    const app = $('#pkApp');
    if (!app) return;
    const reduceMotion = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    const coarse = window.matchMedia && matchMedia('(pointer: coarse)').matches;

    const cfg = {
        opsLeft: app.dataset.opsLeft === undefined ? null : parseInt(app.dataset.opsLeft, 10),
        signedIn: app.dataset.signedIn === '1',
        fileLimitMb: app.dataset.fileLimitMb,
        packetLimitMb: app.dataset.packetLimitMb,
        ttlMinutes: parseInt(app.dataset.ttlMinutes, 10) || 120,
        maxHours: parseInt(app.dataset.maxHours, 10) || 24,
    };

    // ---- Storage, guarded: private windows and blocked site data throw ----
    const store = (() => { try { return window.localStorage; } catch (e) { return null; } })();
    const session = (() => { try { return window.sessionStorage; } catch (e) { return null; } })();
    const tabId = (() => {
        let id = null;
        try { id = session && session.getItem('packet-tab'); } catch (e) { id = null; }
        if (!PM.isTabId(id)) {
            id = PM.makeTabId(crypto.getRandomValues(new Uint8Array(22)));
            try { if (session) session.setItem('packet-tab', id); } catch (e) { /* tab id lives for this load only */ }
        }
        return id;
    })();
    const initialPaper = (() => {
        try { return store && store.getItem('docist.paper') === 'a4' ? 'a4' : 'letter'; } catch (e) { return 'letter'; }
    })();

    // ---- State ----
    let S = PM.emptyState(initialPaper);
    const files = new Map();            // hash -> File, while this page has it
    let sessionLost = false;
    let accepted = null;                // extensions from /formats
    const ui = {
        expanded: new Set(),
        allPages: false,
        sel: new Set(),                 // 'c:<item>' or 'p:<item>:<index>'
        anchor: null,
        selecting: false,               // touch selection mode
        grab: null,                     // {key, snapshot}
        preview: null,                  // {pos, item, opener}
        buildErrors: new Map(),         // item id -> code from a refused build
        building: false,
        result: null,
        focusKey: null,
        flashKey: null,
        insertAt: null,                 // slot for files chosen from a "+" menu
        reAdd: null,                    // item id whose Re-add button opened the picker
    };

    // ---- Live regions and toast ----
    const live = $('#pkLive');
    const liveA = $('#pkLiveAssertive');
    let assertTimer = null;
    function say(text, assertive) {
        if (assertive) {
            clearTimeout(assertTimer);
            assertTimer = setTimeout(() => { liveA.textContent = text; }, 100);
            return;
        }
        live.textContent = '';
        setTimeout(() => { live.textContent = text; }, 30);
    }
    const toast = $('#pkToast');
    let toastTimer = null;
    function showToast(text, withUndo) {
        $('#pkToastText').textContent = text;
        $('#pkToastUndo').hidden = !withUndo;
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
        say(text);
    }
    $('#pkToastUndo').addEventListener('click', () => { toast.hidden = true; doUndo(); });

    // ---- Changes ----
    // Every arrangement change goes through change(): one undo step, then
    // holds released for hashes nothing names, a draft save and a render.
    function change(packet, message) {
        if (packet === S.packet) return false;
        S = PM.commit(S, packet);
        afterChange();
        if (message) say(message);
        return true;
    }
    function afterChange() {
        const r = PM.releaseUnnamed(S);
        S = r.state;
        r.hashes.forEach(h => { files.delete(h); stager.forget(h); });
        if (r.release.length) releaseStages(r.release);
        ui.buildErrors.clear();
        pruneSelection();
        scheduleDraft();
        render();
        stager.restageStale();
    }
    function update(fn) {
        S = fn(S);
        scheduleDraft();
        render();
    }
    function doUndo() {
        if (ui.grab || !S.past.length) return;
        S = PM.undo(S);
        ui.sel.clear();
        afterChange();
        say('Undone.');
    }
    function doRedo() {
        if (ui.grab || !S.future.length) return;
        S = PM.redo(S);
        ui.sel.clear();
        afterChange();
        say('Redone.');
    }

    // ---- Server holds ----
    function releaseStages(ids) {
        if (ids.length) postHold(fetch, tabId, [], ids);
    }
    async function renewHolds() {
        const keep = PM.keepIds(S);
        if (!keep.length) return;
        const answer = await postHold(fetch, tabId, keep, []);
        if (answer && answer.gone.length) update(s => PM.applyHold(s, answer));
    }
    let lastRenew = Date.now();
    setInterval(() => {
        if (document.visibilityState === 'visible') { lastRenew = Date.now(); renewHolds(); }
    }, HOLD_RENEW_MS);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && Date.now() - lastRenew > HOLD_RENEW_MS) {
            lastRenew = Date.now();
            renewHolds();
        }
    });

    // ---- Drafts ----
    let draftTimer = null;
    function scheduleDraft() {
        clearTimeout(draftTimer);
        draftTimer = setTimeout(() => { if (store) PM.saveDraft(store, tabId, S.packet, S.table, Date.now()); }, 300);
    }
    async function restoreFrom(draft) {
        const r = PM.restoreDraft(draft);
        S = r.state;
        render();
        if (r.keep.length || r.release.length) {
            const answer = await postHold(fetch, tabId, r.keep, r.release);
            if (answer) update(s => PM.applyHold(s, answer));
        }
        scheduleDraft();
    }
    function hasItems(draft) {
        return draft && PM.itemOrder(draft.packet).length > 0;
    }

    // ---- Staging queue: one request in flight (§4.3 lanes) ----
    const io = {
        fetch: (...args) => fetch(...args),
        holder: tabId,
        get: () => S,
        update: fn => update(fn),
        release: async ids => { await postHold(fetch, tabId, [], ids); },
        toast: text => showToast(text, false),
        sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
    };
    const stager = createStageQueue(io, files, {
        blocked: () => sessionLost,
        done(hash, kind) {
            if (kind === 'csrf') loseSession();
            const row = S.table[hash];
            if (kind === 'ok' && row && row.status === 'ready') say(row.name + ' is ready.');
            else if (row && row.status === 'failed') say((row.name || 'A file') + ': ' + row.message);
        },
    });
    const enqueue = hash => stager.enqueue(hash);
    function loseSession() {
        sessionLost = true;
        const box = $('#pkSession');
        box.textContent = CSRF_MESSAGE;
        box.hidden = false;
    }

    // ---- Adding files ----
    function extOf(name) {
        const i = name.lastIndexOf('.');
        return i === -1 ? '' : name.slice(i).toLowerCase();
    }
    async function addFileList(list, slot, reAddItem) {
        const chosen = Array.from(list || []);
        if (!chosen.length) return;
        if (!(window.crypto && crypto.subtle)) {
            showToast('This page needs a secure connection (HTTPS) to read files.', false);
            return;
        }
        say('Reading ' + plural(chosen.length, 'file') + '…');
        const hashed = [];
        for (const file of chosen) {
            hashed.push({ file, hash: await PM.sha256Hex(await file.arrayBuffer(), crypto.subtle) });
        }
        let packet = S.packet;
        let table = S.table;
        const added = [];
        let restored = 0;
        let slotAt = slot;
        if (reAddItem) {
            const found = PM.findItem(packet, reAddItem);
            if (found) slotAt = { section: found.section.id, index: found.i + 1 };
        }
        hashed.forEach(({ file, hash }) => {
            const row = table[hash];
            files.set(hash, file);
            if (row) {
                // Bytes this tab already knows (§3.1 Restore). An expired,
                // failed or stale row binds again by hash, so every card of
                // this file comes back. A plain drop also adds a card here,
                // unless one of this file's cards is expired and is the one
                // coming back; Re-add never adds a card for the same file.
                const status = PM.rowStatus(row, packet);
                const inPacket = PM.packetHashes(packet).has(hash);
                if (status === 'expired' || status === 'failed' || status === 'stale') {
                    table = PM.setRow(table, hash, { status: 'waiting', reason: '', message: '' });
                    stager.forget(hash);
                    restored++;
                    if (!inPacket && !reAddItem) added.push({ hash, label: file.name });
                } else if (!reAddItem) {
                    added.push({ hash, label: file.name });
                }
                return;
            }
            added.push({ hash, label: file.name });
            const ok = !accepted || accepted.includes(extOf(file.name));
            table = PM.setRow(table, hash, ok
                ? { stage_id: null, key: '', pages: null, bytes: file.size, name: file.name, type: PM.fileType(file.name), status: 'waiting', reason: '', message: '' }
                : { stage_id: null, key: '', pages: null, bytes: file.size, name: file.name, type: PM.fileType(file.name), status: 'failed', reason: 'type', message: "This file type isn't supported." });
        });
        S = Object.assign({}, S, { table });
        if (added.length) {
            const before = S.packet;
            const next = PM.addFiles(S.packet, added, slotAt);
            const ids = PM.newItemIds(before, next);
            change(next, plural(added.length, 'file') + ' added.');
            if (ids.length) ui.focusKey = 'c:' + ids[0] + ':open';
            if (reAddItem && added.some(a => !PM.packetHashes(before).has(a.hash))) {
                showToast('That is a different file from the one this card had, so it was added after it.', false);
            }
        } else {
            scheduleDraft();
        }
        if (restored) say(plural(restored, 'file') + ' re-added.');
        render();
        hashed.forEach(({ hash }) => {
            const row = S.table[hash];
            if (row && row.status === 'waiting') enqueue(hash);
        });
    }

    const fileInput = $('#pkFileInput');
    function pickFiles(slot, reAddItem) {
        ui.insertAt = slot || null;
        ui.reAdd = reAddItem || null;
        fileInput.click();
    }
    fileInput.addEventListener('change', () => {
        const slot = ui.insertAt;
        const reAdd = ui.reAdd;
        ui.insertAt = null;
        ui.reAdd = null;
        addFileList(fileInput.files, slot, reAdd).finally(() => { fileInput.value = ''; });
    });
    $('#pkAddFiles').addEventListener('click', () => pickFiles(null));
    $('#pkEmptyAdd').addEventListener('click', () => pickFiles(null));

    // Native file drops on the workspace, with an insertion line. Sortable
    // runs with forceFallback, so the two never collide (§3.4).
    const main = $('#pkMain');
    const dropline = $('#pkDropline');
    const hasFiles = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    function dropSlot(y) {
        const cards = Array.from(document.querySelectorAll('#pkList .pk-card'));
        for (const card of cards) {
            const r = card.getBoundingClientRect();
            if (y < r.top + r.height / 2) {
                const found = PM.findItem(S.packet, card.dataset.item);
                return { slot: { section: found.section.id, index: found.i }, el: card, before: true };
            }
        }
        return { slot: null, el: cards[cards.length - 1] || null, before: false };
    }
    document.addEventListener('dragover', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        main.classList.add('pk-dragover');
        const { el, before } = dropSlot(e.clientY);
        const mr = main.getBoundingClientRect();
        let top;
        if (!el) top = $('#pkList').getBoundingClientRect().top - mr.top;
        else {
            const r = el.getBoundingClientRect();
            top = (before ? r.top - 12 : r.bottom + 10) - mr.top;
        }
        dropline.style.top = top + 'px';
        dropline.hidden = false;
    });
    document.addEventListener('dragleave', e => {
        if (!e.relatedTarget) { dropline.hidden = true; main.classList.remove('pk-dragover'); }
    });
    document.addEventListener('drop', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        dropline.hidden = true;
        main.classList.remove('pk-dragover');
        addFileList(e.dataTransfer.files, dropSlot(e.clientY).slot);
    });

    // ---- Thumbnails: lazy, one render request in flight, retry on busy ----
    const thumbUrls = new Map();        // url -> object URL
    const thumbQueue = [];
    let thumbBusy = false;
    const observer = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            observer.unobserve(entry.target);
            wantThumb(entry.target.dataset.src, false);
        });
    }, { rootMargin: '200px' }) : null;
    function wantThumb(url, urgent) {
        if (!url || thumbUrls.has(url)) return;
        const i = thumbQueue.indexOf(url);
        if (i !== -1) thumbQueue.splice(i, 1);
        if (urgent) thumbQueue.unshift(url); else thumbQueue.push(url);
        pumpThumbs();
    }
    async function pumpThumbs() {
        if (thumbBusy) return;
        const url = thumbQueue.shift();
        if (!url) return;
        thumbBusy = true;
        let wait = 0;
        try {
            const response = await fetch(url);
            const c = classify(response.status, null);
            if (c.kind === 'ok') {
                thumbUrls.set(url, URL.createObjectURL(await response.blob()));
                document.querySelectorAll('img[data-src="' + url + '"]').forEach(img => { img.src = thumbUrls.get(url); });
            } else if (c.kind === 'expired') {
                const sid = url.split('/')[3];
                update(s => PM.expireStages(s, [sid]));
            } else if (c.kind === 'busy') {
                thumbQueue.unshift(url);
                wait = retryAfterMs(response.headers.get('Retry-After'), 2000);
            }
        } catch (e) {
            // Network trouble: leave the placeholder; scrolling asks again.
        }
        setTimeout(() => { thumbBusy = false; pumpThumbs(); }, wait);
    }
    function thumbUrl(row, src, w) {
        return '/packet/stage/' + encodeURIComponent(row.stage_id) + '/' + (w > 320 ? 'page' : 'thumb') + '/' + src + '?w=' + w;
    }
    function loadImages(root) {
        root.querySelectorAll('img[data-src]').forEach(img => {
            const url = img.dataset.src;
            if (thumbUrls.has(url)) img.src = thumbUrls.get(url);
            else if (observer) observer.observe(img);
            else wantThumb(url, false);
        });
    }

    // ---- Rendering ----
    const list = $('#pkList');
    function counts() {
        return PM.readyCounts(S.table, S.packet);
    }
    function rowOf(item) {
        return item.kind === 'file' ? S.table[item.hash] : null;
    }
    function statusOf(item) {
        return item.kind === 'file' ? PM.rowStatus(S.table[item.hash], S.packet) : 'ready';
    }
    function pageName(item, ref) {
        return item.label + ' p' + (ref.src + 1);
    }
    function rangeText(info) {
        if (!info || info.label == null) return '';
        const last = info.label + (info.last - info.first);
        return info.first === info.last ? 'p. ' + info.label : 'pp. ' + info.label + '–' + last;
    }
    function sheet(rot, img, extraClass) {
        const turn = rot ? ' style="transform:rotate(' + rot + 'deg)' + (rot % 180 ? ' scale(.77)' : '') + '"' : '';
        return '<span class="pk-sheet ' + (extraClass || '') + ' pk-' + S.packet.options.paper + '"' + turn + '>' + (img || '') + '</span>';
    }
    function imgTag(row, src, w, alt) {
        const url = thumbUrl(row, src, w);
        return '<img alt="' + escapeHtml(alt) + '" data-src="' + escapeHtml(url) + '"'
            + (thumbUrls.has(url) ? ' src="' + thumbUrls.get(url) + '"' : '') + ' draggable="false">';
    }
    function selected(key) {
        return ui.sel.has(key);
    }

    function cardHtml(entry, filePos, fileCount, plan, cnt) {
        const { item, section, i } = entry;
        const key = 'c:' + item.id;
        const info = plan.items[item.id];
        const isBlank = item.kind === 'blank';
        const row = rowOf(item);
        const status = statusOf(item);
        const name = isBlank ? 'Blank page' : item.label;
        const refs = isBlank ? [] : PM.itemRefs(item, cnt);
        const n = isBlank ? 1 : refs.length;
        const expanded = !isBlank && status === 'ready' && (ui.allPages || ui.expanded.has(item.id));
        const held = ui.grab && ui.grab.key === key;
        const err = ui.buildErrors.get(item.id);
        const text = statusText(status, row, S.packet.options.paper, !isBlank && files.has(item.hash));
        const ctx = isBlank ? 'blank page' : name;
        const type = isBlank ? 'NEW' : ((row && row.type) || PM.fileType(item.label) || 'file').toUpperCase();
        let thumb;
        if (isBlank) thumb = sheet(0, '', 'pk-sheet-blank');
        else if (status === 'ready' && refs.length) thumb = sheet(refs[0].rot, imgTag(row, refs[0].src, 160, pageName(item, refs[0])));
        else thumb = '<span class="pk-sheet pk-sheet-wait pk-' + S.packet.options.paper + '"></span>';
        const sub = [
            '<span class="pk-badge">' + escapeHtml(type) + '</span>',
            isBlank ? '<span class="pk-gen">generated</span>' : '',
            status === 'ready' || isBlank ? '<span>' + plural(n, 'page') + '</span>' : '',
            info ? '<span>' + rangeText(info) + '</span>' : '',
            row && row.bytes && status === 'ready' ? '<span>' + formatMb(row.bytes) + '</span>' : '',
        ].join('');
        const errText = err ? itemCodeText(err) : '';
        const msgButtons = [];
        if (!isBlank && (status === 'failed') && files.has(item.hash) && row && row.reason !== 'type' && row.reason !== 'csrf') {
            msgButtons.push('<button type="button" class="pk-btn pk-small" data-act="retry">Retry</button>');
        }
        if (!isBlank && (status === 'expired' || (status === 'stale' && !files.has(item.hash)) || ['expired', 'stale', 'out_of_range', 'unreadable'].includes(err))) {
            msgButtons.push('<button type="button" class="pk-btn pk-small" data-act="readd" aria-label="Re-add ' + escapeHtml(name) + '">Re-add</button>');
        }
        if (status === 'failed' || status === 'expired') {
            msgButtons.push('<button type="button" class="pk-btn pk-small" data-act="remove">Remove</button>');
        }
        const msg = text || errText
            ? '<div class="pk-cardmsg' + (status === 'failed' || status === 'expired' || err ? ' pk-bad' : '') + '"><span class="pk-cardmsg-text">'
                + escapeHtml(errText || text) + '</span>' + msgButtons.join('') + '</div>'
            : '';
        let pages = '';
        if (expanded) {
            pages = '<div class="pk-pages" data-item="' + item.id + '" role="group" aria-label="Pages of ' + escapeHtml(name) + '">'
                + refs.map((ref, index) => tileHtml(item, row, ref, index, plan)).join('') + '</div>';
        }
        const position = 'file ' + filePos + ' of ' + fileCount;
        return '<div class="pk-card' + (isBlank ? ' pk-card-gen' : '') + (selected(key) ? ' pk-sel' : '')
            + (held ? ' pk-held' : '') + '" data-item="' + item.id + '" data-key="' + key + '">'
            + '<div class="pk-card-head">'
            + '<span class="pk-handle" role="application" tabindex="0" data-fkey="' + key + ':handle" aria-roledescription="drag handle" aria-label="Move ' + escapeHtml(ctx) + ', ' + position
            + '. Space picks it up, arrow keys move it, Space drops."><svg viewBox="0 0 10 16" width="10" height="16" aria-hidden="true" focusable="false"><g fill="currentColor"><circle cx="2.5" cy="3" r="1.4"/><circle cx="7.5" cy="3" r="1.4"/><circle cx="2.5" cy="8" r="1.4"/><circle cx="7.5" cy="8" r="1.4"/><circle cx="2.5" cy="13" r="1.4"/><circle cx="7.5" cy="13" r="1.4"/></g></svg></span>'
            + '<input type="checkbox" class="pk-check" data-fkey="' + key + ':check"' + (selected(key) ? ' checked' : '') + ' aria-label="Select ' + escapeHtml(ctx) + '">'
            + '<button type="button" class="pk-open" data-act="open" data-fkey="' + key + ':open" aria-label="Preview ' + escapeHtml(ctx) + ', '
            + (isBlank || status === 'ready' ? plural(n, 'page') : escapeHtml(text || status)) + ', ' + position + '">'
            + '<span class="pk-thumb">' + thumb + '</span>'
            + '<span class="pk-meta"><span class="pk-name">' + escapeHtml(name) + '</span><span class="pk-sub">' + sub + '</span></span></button>'
            + '<span class="pk-actions">'
            + (!isBlank && status === 'ready' && !ui.allPages
                ? '<button type="button" class="pk-ibtn" data-act="expand" data-fkey="' + key + ':expand" aria-expanded="' + expanded + '" aria-label="' + (expanded ? 'Hide' : 'Show') + ' pages of ' + escapeHtml(name) + '" title="' + (expanded ? 'Hide pages' : 'Show pages') + '">'
                    + '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false" class="' + (expanded ? 'pk-up' : '') + '"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
                : '')
            + '<button type="button" class="pk-ibtn" data-act="menu" data-fkey="' + key + ':menu" aria-haspopup="menu" aria-label="Move ' + escapeHtml(ctx) + '" title="Move">'
            + '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><g fill="currentColor"><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/></g></svg></button>'
            + '<button type="button" class="pk-ibtn pk-remove" data-act="remove" data-fkey="' + key + ':remove" aria-label="Remove ' + escapeHtml(ctx) + '" title="Remove">'
            + '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>'
            + '</span></div>' + msg + pages
            + '<button type="button" class="pk-plus" data-act="plus" data-section="' + section.id + '" data-index="' + (i + 1) + '" data-fkey="' + key + ':plus" aria-haspopup="menu" aria-label="Insert after ' + escapeHtml(ctx) + '">+</button>'
            + '</div>';
    }

    function tileHtml(item, row, ref, index, plan) {
        const key = 'p:' + item.id + ':' + index;
        const physical = plan.pages.findIndex(p => p.item === item.id && p.index === index);
        const label = physical === -1 ? null : plan.pages[physical].label;
        const src = pageName(item, ref);
        const ctx = 'page ' + (ref.src + 1) + ' of ' + item.label;
        const held = ui.grab && ui.grab.key === key;
        return '<div class="pk-tile' + (selected(key) ? ' pk-sel' : '') + (held ? ' pk-held' : '') + '" data-item="' + item.id + '" data-index="' + index + '" data-key="' + key + '">'
            + '<button type="button" class="pk-tile-open" data-act="open" data-fkey="' + key + ':open" aria-label="Preview ' + escapeHtml(ctx)
            + (label != null ? ', packet page ' + label : '') + (ref.rot ? ', rotated ' + ref.rot + ' degrees' : '') + '">'
            + sheet(ref.rot, imgTag(row, ref.src, 160, src)) + '</button>'
            + '<input type="checkbox" class="pk-check" data-fkey="' + key + ':check"' + (selected(key) ? ' checked' : '') + ' aria-label="Select ' + escapeHtml(ctx) + '">'
            + '<span class="pk-tile-foot">'
            + '<span class="pk-handle" role="application" tabindex="0" data-fkey="' + key + ':handle" aria-roledescription="drag handle" aria-label="Move ' + escapeHtml(ctx) + '. Space picks it up, arrow keys move it, Space drops.">'
            + '<svg viewBox="0 0 10 16" width="8" height="13" aria-hidden="true" focusable="false"><g fill="currentColor"><circle cx="2.5" cy="3" r="1.4"/><circle cx="7.5" cy="3" r="1.4"/><circle cx="2.5" cy="8" r="1.4"/><circle cx="7.5" cy="8" r="1.4"/><circle cx="2.5" cy="13" r="1.4"/><circle cx="7.5" cy="13" r="1.4"/></g></svg></span>'
            + '<span class="pk-tile-cap">' + (label != null ? 'p. ' + label : '') + '<span class="pk-src">' + escapeHtml(src) + '</span></span>'
            + '<button type="button" class="pk-ibtn pk-tiny" data-act="menu" data-fkey="' + key + ':menu" aria-haspopup="menu" aria-label="Move ' + escapeHtml(ctx) + '" title="Move">'
            + '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><g fill="currentColor"><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/></g></svg></button>'
            + '</span></div>';
    }

    function render() {
        const active = document.activeElement;
        const keepFocus = ui.focusKey || (active && list.contains(active) && active.dataset.fkey) || null;
        ui.focusKey = null;
        clearSortableSelection();

        const cnt = counts();
        const plan = PM.pagePlan(S.packet, cnt);
        const order = PM.itemOrder(S.packet);
        const showSections = S.packet.sections.length > 1;
        const fileCount = order.length;
        let filePos = 0;

        app.classList.toggle('pk-selecting', ui.selecting || ui.sel.size > 0);
        $('#pkEmpty').hidden = order.length > 0;
        $('#pkWork').hidden = order.length === 0;

        list.innerHTML = S.packet.sections.map(section => {
            const cards = order.filter(e => e.section === section)
                .map(e => cardHtml(e, ++filePos, fileCount, plan, cnt)).join('');
            const heading = showSections
                ? '<h2 class="pk-sec">' + escapeHtml(section.title || 'Untitled section') + '</h2>' : '';
            return '<section class="pk-section" data-section="' + section.id + '" aria-label="' + escapeHtml(section.title || 'Packet') + '">'
                + heading
                + '<button type="button" class="pk-plus pk-plus-top" data-act="plus" data-section="' + section.id + '" data-index="0" data-fkey="s:' + section.id + ':plus" aria-haspopup="menu" aria-label="Insert at the start' + (showSections ? ' of ' + escapeHtml(section.title || 'this section') : ' of the packet') + '">+</button>'
                + '<div class="pk-items" data-section="' + section.id + '">' + cards + '</div></section>';
        }).join('');

        renderOutline(plan, order);
        renderFooter(plan);
        renderBulk();
        renderPaperNote();
        $('#pkUndo').disabled = !S.past.length;
        $('#pkRedo').disabled = !S.future.length;
        $('#pkClear').disabled = !order.length;
        $('#pkAllPages').setAttribute('aria-pressed', String(ui.allPages));

        loadImages(list);
        bindSortables();
        if (keepFocus) {
            const el = list.querySelector('[data-fkey="' + keepFocus + '"]');
            if (el) {
                el.focus({ preventScroll: true });
                el.scrollIntoView({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
            }
        }
        if (ui.flashKey) {
            const el = list.querySelector('[data-key="' + ui.flashKey + '"]');
            if (el) {
                el.classList.add('pk-flash');
                el.scrollIntoView({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
            }
            ui.flashKey = null;
        }
        if (ui.preview) renderPreview();
    }

    function renderOutline(plan, order) {
        const out = [];
        const showSections = S.packet.sections.length > 1;
        let lastSection = null;
        let padBefore = 0;
        plan.pages.forEach(p => { if (p.kind === 'pad') padBefore++; });
        order.forEach(({ item, section }) => {
            if (showSections && section !== lastSection) {
                const info = plan.sections[section.id];
                out.push('<li class="pk-o-sec"><span class="pk-o-name">' + escapeHtml(section.title || 'Untitled section') + '</span>'
                    + '<span class="pk-o-pp">' + (info && info.label != null ? info.label : '') + '</span></li>');
                lastSection = section;
            }
            const info = plan.items[item.id];
            const status = statusOf(item);
            const name = item.kind === 'blank' ? 'Blank page' : item.label;
            const pp = info ? (info.first === info.last ? String(info.label) : info.label + '–' + (info.label + info.last - info.first))
                : '<span class="pk-o-wait">' + escapeHtml({ waiting: 'waiting', uploading: 'converting', failed: 'failed', expired: 'expired', stale: 'converting' }[status] || '') + '</span>';
            out.push('<li><button type="button" class="pk-o-link" data-item="' + item.id + '"><span class="pk-o-name">' + escapeHtml(name) + '</span>'
                + '<span class="pk-o-pp">' + pp + '</span></button></li>');
        });
        if (padBefore) {
            out.push('<li class="pk-o-pad">' + plural(padBefore, 'blank page') + ' added for double-sided printing</li>');
        }
        $('#pkOutline').innerHTML = out.join('') || '<li class="pk-o-empty">Nothing yet.</li>';
    }

    function opsText() {
        if (cfg.opsLeft == null) return '';
        return plural(cfg.opsLeft, 'operation') + ' left today';
    }

    function renderFooter(plan) {
        const total = plan.total;
        $('#pkTotal').textContent = plural(total, 'page');
        $('#pkOps').textContent = opsText();
        const ready = buildReadiness(S, cfg.opsLeft);
        const build = $('#pkBuild');
        build.disabled = !ready.ok || ui.building;
        build.textContent = ui.building ? 'Building…' : 'Build packet';
        build.setAttribute('aria-busy', String(ui.building));
        const msg = $('#pkBuildMsg');
        if (!ui.building && !msg.dataset.sticky) msg.textContent = ready.ok ? '' : ready.reason;
        const ops = $('#pkEmptyOps');
        if (cfg.opsLeft === 0) {
            ops.innerHTML = 'You have no operations left today, so a packet can be arranged but not built until tomorrow (UTC). '
                + (cfg.signedIn ? '<a href="/account">See your plan</a>.' : '<a href="/login">Sign in</a> or <a href="/signup">create a free account</a> for more.');
        } else {
            ops.textContent = cfg.opsLeft == null ? '' : 'You have ' + opsText() + '. Building a packet uses one; arranging is free.';
        }
    }

    function renderPaperNote() {
        const select = $('#pkPaper');
        select.value = S.packet.options.paper;
        const other = S.packet.options.paper === 'a4' ? 'letter' : 'a4';
        const impact = PM.paperImpact(S, other);
        $('#pkPaperNote').textContent = impact.files
            ? 'Switching to ' + (other === 'a4' ? 'A4' : 'Letter') + ' converts ' + plural(impact.files, 'file') + ' again'
                + (impact.edited ? '; page edits on ' + plural(impact.edited, 'card') + ' may reset.' : '.')
            : 'Converted files use this paper. PDFs keep their own pages.';
        const o = S.packet.options;
        $('#pkOptNumbers').checked = o.page_numbers;
        $('#pkOptPosition').value = o.number_position;
        $('#pkOptStart').value = o.start_number;
        $('#pkOptBookmarks').checked = o.bookmarks;
        $('#pkOptRecto').checked = o.recto_starts;
        $('#pkOptPosition').disabled = !o.page_numbers;
        $('#pkOptStart').disabled = !o.page_numbers;
    }

    // ---- Selection ----
    function viewKeys(kind) {
        return Array.from(list.querySelectorAll(kind === 'p' ? '.pk-tile' : '.pk-card')).map(el => el.dataset.key);
    }
    function selKind() {
        const first = ui.sel.values().next().value;
        return first ? first[0] : null;
    }
    function pruneSelection() {
        const valid = new Set(PM.itemOrder(S.packet).map(e => 'c:' + e.item.id));
        PM.pageOrder(S.packet, counts()).forEach(p => valid.add('p:' + p.item + ':' + p.index));
        Array.from(ui.sel).forEach(k => { if (!valid.has(k)) ui.sel.delete(k); });
        Array.from(ui.expanded).forEach(id => { if (!valid.has('c:' + id)) ui.expanded.delete(id); });
        if (!ui.sel.size) ui.selecting = false;
    }
    function toggleSel(key, range) {
        if (selKind() && selKind() !== key[0]) ui.sel.clear();
        if (range && ui.anchor && ui.anchor[0] === key[0]) {
            PM.rangeKeys(viewKeys(key[0]), ui.anchor, key).forEach(k => ui.sel.add(k));
        } else {
            if (ui.sel.has(key)) ui.sel.delete(key); else ui.sel.add(key);
            ui.anchor = key;
        }
        if (!ui.sel.size) ui.selecting = false;
        render();
        say(ui.sel.size ? plural(ui.sel.size, selKind() === 'p' ? 'page' : 'file') + ' selected.' : 'Selection cleared.');
    }
    function clearSel() {
        ui.sel.clear();
        ui.selecting = false;
        ui.anchor = null;
        render();
        say('Selection cleared.');
    }
    function selPages() {
        return Array.from(ui.sel).filter(k => k[0] === 'p').map(k => {
            const [, item, index] = k.split(':');
            return { item, index: Number(index) };
        });
    }
    function selItems() {
        return Array.from(ui.sel).filter(k => k[0] === 'c').map(k => k.slice(2));
    }
    function renderBulk() {
        const bar = $('#pkBulk');
        const n = ui.sel.size;
        bar.hidden = !n && !ui.selecting;
        $('#pkBulkCount').textContent = n ? plural(n, selKind() === 'p' ? 'page' : 'file') + ' selected' : 'Tap items to select';
        bar.querySelectorAll('[data-bulk]').forEach(b => { if (b.dataset.bulk !== 'done') b.disabled = !n; });
    }

    // ---- Moves ----
    function keyFor(page) {
        return 'p:' + page.item + ':' + page.index;
    }
    function describeItem(id) {
        const found = PM.findItem(S.packet, id);
        return found ? (found.item.kind === 'blank' ? 'Blank page' : found.item.label) : 'item';
    }
    function itemPosition(id) {
        const order = PM.itemOrder(S.packet);
        return 'position ' + (order.findIndex(e => e.item.id === id) + 1) + ' of ' + order.length;
    }
    // Move whole items; focus follows the first one.
    function moveItemsTo(ids, dest, live) {
        const next = PM.moveItems(S.packet, ids, dest);
        if (next === S.packet) return false;
        const text = (ids.length === 1 ? 'Moved ' + describeItem(ids[0]) : 'Moved ' + plural(ids.length, 'file'));
        if (live) S = PM.replacePacket(S, next); else change(next);
        ui.focusKey = 'c:' + ids[0] + (live ? ':handle' : ':menu');
        ui.flashKey = 'c:' + ids[0];
        if (live) render();
        say(text + ' to ' + itemPosition(ids[0]) + '.', !!live);
        return true;
    }
    function movePagesTo(targets, dest, live, focusPart) {
        const first = PM.movedFirstIndex(S.packet, S.table, targets, dest);
        const label = targets.length === 1 ? 'page ' + pageLabel(targets[0]) : plural(targets.length, 'page');
        const next = PM.movePages(S.packet, S.table, targets, dest);
        if (next === S.packet) return false;
        if (live) S = PM.replacePacket(S, next); else change(next);
        const order = PM.pageOrder(S.packet, counts());
        const landed = order[first];
        if (landed) {
            ui.expanded.add(landed.item);
            ui.focusKey = keyFor(landed) + ':' + (focusPart || 'menu');
            ui.flashKey = keyFor(landed);
        }
        ui.sel.clear();
        render();
        say('Moved ' + label + ' to position ' + (first + 1) + ' of ' + order.length + '.', !!live);
        return true;
    }
    function pageLabel(page) {
        const found = PM.findItem(S.packet, page.item);
        if (!found) return '';
        if (found.item.kind === 'blank') return 'blank page';
        const ref = PM.itemRefs(found.item, counts())[page.index];
        return ref ? (ref.src + 1) + ' of ' + found.item.label : '';
    }
    function stepMove(key, where, live) {
        if (key[0] === 'c') {
            const id = key.slice(2);
            const dest = PM.itemStepDest(S.packet, id, where);
            if (!dest) { say(describeItem(id) + ' is already at the ' + (where === 'start' || where === 'earlier' ? 'start.' : 'end.'), !!live); return null; }
            moveItemsTo([id], dest, live);
            return key;
        }
        const [, item, index] = key.split(':');
        const page = { item, index: Number(index) };
        const dest = PM.pageStepDest(S.packet, S.table, page, where);
        if (!dest) { say('Page ' + pageLabel(page) + " can't move " + (where === 'earlier' || where === 'start' ? 'earlier.' : 'later.'), !!live); return null; }
        const first = PM.movedFirstIndex(S.packet, S.table, [page], dest);
        movePagesTo([page], dest, live, live ? 'handle' : 'menu');
        const landed = PM.pageOrder(S.packet, counts())[first];
        return landed ? keyFor(landed) : null;
    }

    // ---- Menus: "+" insert and Move ----
    const menu = $('#pkMenu');
    let menuOpener = null;
    let menuTarget = null;
    function openMenu(opener, entries, label) {
        menuOpener = opener;
        menu.setAttribute('aria-label', label);
        menu.innerHTML = entries.map(e => '<button type="button" role="menuitem" data-menu="' + e.id + '"' + (e.disabled ? ' aria-disabled="true"' : '') + '>'
            + escapeHtml(e.text) + (e.note ? '<small>' + escapeHtml(e.note) + '</small>' : '') + '</button>').join('');
        menu.hidden = false;
        const r = opener.getBoundingClientRect();
        const w = menu.offsetWidth;
        const h = menu.offsetHeight;
        menu.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + 'px';
        menu.style.top = (r.bottom + h + 8 > innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + 'px';
        opener.setAttribute('aria-expanded', 'true');
        const first = menu.querySelector('[data-menu]:not([aria-disabled])');
        if (first) first.focus();
    }
    function closeMenu(returnFocus) {
        if (menu.hidden) return;
        menu.hidden = true;
        if (menuOpener) {
            menuOpener.setAttribute('aria-expanded', 'false');
            if (returnFocus && document.contains(menuOpener)) menuOpener.focus();
        }
    }
    menu.addEventListener('keydown', e => {
        const items = Array.from(menu.querySelectorAll('[data-menu]:not([aria-disabled])'));
        const i = items.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
        else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
        else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
        else if (e.key === 'Tab') closeMenu(false);
    });
    document.addEventListener('click', e => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== menuOpener) closeMenu(false);
    });
    function plusMenu(opener) {
        menuTarget = { kind: 'plus', slot: { section: opener.dataset.section, index: Number(opener.dataset.index) } };
        // Divider and cover pages are [B3]; the "+" offers them once their
        // renderers exist.
        openMenu(opener, [
            { id: 'blank', text: 'Blank page', note: 'Sized like the page before it' },
            { id: 'files', text: 'Add files here…' },
        ], 'Insert here');
    }
    function moveMenu(opener, key) {
        menuTarget = { kind: 'move', key };
        const name = key[0] === 'c' ? describeItem(key.slice(2)) : 'page ' + pageLabel({ item: key.split(':')[1], index: Number(key.split(':')[2]) });
        openMenu(opener, [
            { id: 'start', text: 'Move to start' },
            { id: 'earlier', text: 'Earlier' },
            { id: 'later', text: 'Later' },
            { id: 'end', text: 'Move to end' },
            { id: 'position', text: 'Move to position…' },
        ], 'Move ' + name);
    }
    menu.addEventListener('click', e => {
        const b = e.target.closest('[data-menu]');
        if (!b || b.getAttribute('aria-disabled')) return;
        e.stopPropagation();
        const choice = b.dataset.menu;
        const target = menuTarget;
        closeMenu(false);
        if (target.kind === 'plus') {
            if (choice === 'files') { pickFiles(target.slot); return; }
            const before = S.packet;
            const next = PM.insertGenerated(S.packet, choice, target.slot);
            const ids = PM.newItemIds(before, next);
            if (ids.length) ui.focusKey = 'c:' + ids[0] + ':open';
            change(next, 'Blank page inserted.');
            return;
        }
        if (choice === 'position') { openMoveDialog(target.key ? [target.key] : null, menuOpener); return; }
        if (target.key[0] === 'c') ui.focusKey = target.key + ':menu';
        stepMove(target.key, choice, false);
    });

    // ---- Move to position dialog ----
    const moveDialog = $('#pkMoveDialog');
    let moveKeys = null;
    let moveOpener = null;
    function openMoveDialog(keys, opener) {
        moveKeys = keys || Array.from(ui.sel);
        moveOpener = opener;
        if (!moveKeys.length) return;
        const pages = moveKeys[0][0] === 'p';
        const moving = new Set(moveKeys);
        const select = $('#pkMoveTarget');
        const cnt = counts();
        const plan = PM.pagePlan(S.packet, cnt);
        let options = [];
        if (pages) {
            PM.pageOrder(S.packet, cnt).forEach(p => {
                const key = keyFor(p);
                if (moving.has(key)) return;
                const phys = plan.pages.find(x => x.item === p.item && x.index === p.index);
                options.push('<option value="' + key + '">' + (phys && phys.label != null ? 'Page ' + phys.label + ' · ' : '') + escapeHtml(pageLabel(p) || '') + '</option>');
            });
        } else {
            options = PM.itemOrder(S.packet).filter(e => !moving.has('c:' + e.item.id)).map((e, n) =>
                '<option value="c:' + e.item.id + '">' + (n + 1) + '. ' + escapeHtml(e.item.kind === 'blank' ? 'Blank page' : e.item.label) + '</option>');
        }
        $('#pkMoveTitle').textContent = 'Move ' + (moveKeys.length === 1
            ? (pages ? 'page ' + pageLabel({ item: moveKeys[0].split(':')[1], index: Number(moveKeys[0].split(':')[2]) }) : describeItem(moveKeys[0].slice(2)))
            : plural(moveKeys.length, pages ? 'page' : 'file'));
        if (!options.length) { say('There is nowhere else to move it.'); return; }
        select.innerHTML = options.join('');
        moveDialog.showModal();
        $('#pkMoveWhere').focus();
    }
    $('#pkMoveForm').addEventListener('submit', e => {
        e.preventDefault();
        const after = $('#pkMoveWhere').value === 'after';
        const target = $('#pkMoveTarget').value;
        moveDialog.close();
        if (moveKeys[0][0] === 'c') {
            const ids = moveKeys.map(k => k.slice(2));
            const dest = PM.itemDestNear(S.packet, ids, target.slice(2), after);
            if (dest) { ui.sel.clear(); moveItemsTo(ids, dest, false); }
        } else {
            const targets = moveKeys.map(k => ({ item: k.split(':')[1], index: Number(k.split(':')[2]) }));
            const [, item, index] = target.split(':');
            const dest = PM.pageDestNear(S.packet, S.table, targets, { item, index: Number(index) }, after);
            if (dest) movePagesTo(targets, dest, false);
        }
    });
    $('#pkMoveCancel').addEventListener('click', () => moveDialog.close());
    moveDialog.addEventListener('close', () => {
        if (!ui.focusKey && moveOpener && document.contains(moveOpener)) moveOpener.focus();
        else if (ui.focusKey) {
            const el = document.querySelector('[data-fkey="' + ui.focusKey + '"]');
            if (el) el.focus();
        }
    });

    // ---- Canvas clicks ----
    let suppressClick = false;
    list.addEventListener('click', e => {
        const control = e.target.closest('[data-act]');
        if (!control || !list.contains(control)) return;
        if (suppressClick) { suppressClick = false; e.preventDefault(); return; }
        const act = control.dataset.act;
        const holder = control.closest('[data-key]');
        const key = holder ? holder.dataset.key : null;
        const itemId = holder ? holder.dataset.item : null;
        e.stopPropagation();
        if (act === 'plus') { plusMenu(control); return; }
        if (act === 'menu') { moveMenu(control, key); return; }
        if (act === 'open') {
            if (e.shiftKey) { toggleSel(key, true); return; }
            if (e.ctrlKey || e.metaKey || ui.selecting) { toggleSel(key, false); return; }
            if (key[0] === 'c') openPreviewAtItem(itemId, control);
            else openPreviewAtPage({ item: itemId, index: Number(holder.dataset.index) }, control);
            return;
        }
        if (act === 'expand') {
            if (ui.expanded.has(itemId)) ui.expanded.delete(itemId); else ui.expanded.add(itemId);
            ui.focusKey = key + ':expand';
            render();
            return;
        }
        if (act === 'remove') {
            const name = describeItem(itemId);
            change(PM.removeItems(S.packet, [itemId]));
            showToast('Removed ' + name + '.', true);
            focusNear(itemId);
            return;
        }
        if (act === 'retry') {
            const found = PM.findItem(S.packet, itemId);
            if (found) {
                update(s => Object.assign({}, s, { table: PM.setRow(s.table, found.item.hash, { status: 'waiting', reason: '', message: '' }) }));
                stager.forget(found.item.hash);
                enqueue(found.item.hash);
            }
            return;
        }
        if (act === 'readd') { pickFiles(null, itemId); }
    });
    list.addEventListener('change', e => {
        if (!e.target.classList.contains('pk-check')) return;
        const holder = e.target.closest('[data-key]');
        const key = holder.dataset.key;
        ui.focusKey = key + ':check';
        toggleSel(key, false);
    });
    function focusNear(removedId) {
        const card = list.querySelector('.pk-card');
        if (card) ui.focusKey = card.dataset.key + ':open';
        render();
        if (!card) $('#pkEmptyAdd').focus();
    }

    // ---- Keyboard on the canvas: grab mode and arrow navigation ----
    list.addEventListener('keydown', e => {
        const t = e.target;
        if (t.classList.contains('pk-handle')) { handleKey(e, t); return; }
        if ((t.classList.contains('pk-open') || t.classList.contains('pk-tile-open')) && !e.ctrlKey && !e.metaKey && !e.altKey) {
            arrowNav(e, t);
        }
    });
    function handleKey(e, handle) {
        const key = handle.closest('[data-key]').dataset.key;
        if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault();
            e.stopPropagation();
            if (!ui.grab) {
                if (e.key === 'Enter') return;
                ui.grab = { key, snapshot: S.packet };
                ui.focusKey = key + ':handle';
                render();
                say('Picked up ' + grabName(key) + '. Arrow keys move it, Space drops, Escape cancels.', true);
            } else {
                const snapshot = ui.grab.snapshot;
                ui.grab = null;
                S = PM.commitFrom(S, snapshot);
                ui.focusKey = key + ':handle';
                afterChange();
                say('Dropped ' + grabName(key) + '.', true);
            }
            return;
        }
        if (!ui.grab) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            const snapshot = ui.grab.snapshot;
            ui.grab = null;
            S = PM.replacePacket(S, snapshot);
            ui.focusKey = null;
            render();
            say('Move cancelled.', true);
            const el = list.querySelector('[data-fkey$=":handle"]');
            if (el && !document.activeElement.closest('#pkList')) el.focus();
            return;
        }
        const where = { ArrowUp: 'earlier', ArrowLeft: 'earlier', ArrowDown: 'later', ArrowRight: 'later', Home: 'start', End: 'end' }[e.key];
        if (!where) return;
        e.preventDefault();
        e.stopPropagation();
        const newKey = stepMove(ui.grab.key, where, true);
        if (newKey) {
            ui.grab.key = newKey;
            ui.focusKey = newKey + ':handle';
            render();
        }
    }
    function grabName(key) {
        return key[0] === 'c' ? describeItem(key.slice(2)) : 'page ' + pageLabel({ item: key.split(':')[1], index: Number(key.split(':')[2]) });
    }
    list.addEventListener('focusout', e => {
        // Leaving a held item drops it where it is.
        if (!ui.grab || !e.target.classList.contains('pk-handle')) return;
        setTimeout(() => {
            const a = document.activeElement;
            if (ui.grab && !(a && a.classList && a.classList.contains('pk-handle') && a.closest('[data-key]').dataset.key === ui.grab.key)) {
                const snapshot = ui.grab.snapshot;
                ui.grab = null;
                S = PM.commitFrom(S, snapshot);
                afterChange();
            }
        }, 0);
    });
    function arrowNav(e, el) {
        const tile = el.classList.contains('pk-tile-open');
        const all = Array.from(list.querySelectorAll(tile ? '.pk-tile-open' : '.pk-open'));
        const i = all.indexOf(el);
        let j = -1;
        if (e.key === 'ArrowLeft') j = i - 1;
        else if (e.key === 'ArrowRight') j = i + 1;
        else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            if (!tile) j = i + (e.key === 'ArrowUp' ? -1 : 1);
            else {
                // The tile in the row above or below, nearest by x.
                const r = el.getBoundingClientRect();
                const down = e.key === 'ArrowDown';
                let best = null;
                all.forEach((other, k) => {
                    const o = other.getBoundingClientRect();
                    if (down ? o.top <= r.top + 4 : o.top >= r.top - 4) return;
                    const dy = Math.abs(o.top - r.top);
                    const dx = Math.abs(o.left - r.left);
                    if (!best || dy < best.dy - 4 || (Math.abs(dy - best.dy) <= 4 && dx < best.dx)) best = { k, dy, dx };
                });
                j = best ? best.k : -1;
            }
        } else return;
        e.preventDefault();
        e.stopPropagation();
        if (all[j]) all[j].focus();
    }

    // ---- Touch: long press enters selection mode (§2.3) ----
    let press = null;
    list.addEventListener('pointerdown', e => {
        if (e.pointerType !== 'touch') return;
        const opener = e.target.closest('.pk-open, .pk-tile-open');
        if (!opener || e.target.closest('.pk-handle')) return;
        const key = opener.closest('[data-key]').dataset.key;
        press = { x: e.clientX, y: e.clientY, timer: setTimeout(() => {
            press = null;
            ui.selecting = true;
            suppressClick = true;
            if (!ui.sel.has(key)) toggleSel(key, false); else render();
            if (navigator.vibrate) navigator.vibrate(15);
        }, LONG_PRESS_MS) };
    });
    const cancelPress = e => {
        if (!press) return;
        if (e.type === 'pointermove' && Math.hypot(e.clientX - press.x, e.clientY - press.y) < LONG_PRESS_SLOP) return;
        clearTimeout(press.timer);
        press = null;
    };
    ['pointermove', 'pointerup', 'pointercancel'].forEach(t => list.addEventListener(t, cancelPress));
    list.addEventListener('contextmenu', e => { if (e.target.closest('.pk-open, .pk-tile-open') && coarse) e.preventDefault(); });

    // ---- Bulk bar ----
    $('#pkBulk').addEventListener('click', e => {
        const b = e.target.closest('[data-bulk]');
        if (!b || b.disabled) return;
        const act = b.dataset.bulk;
        if (act === 'done') { clearSel(); return; }
        const pages = selKind() === 'p';
        const items = selItems();
        const targets = selPages();
        const n = ui.sel.size;
        if (act === 'move') { openMoveDialog(null, b); return; }
        let next = S.packet;
        let text = '';
        if (act === 'rotl' || act === 'rotr') {
            const d = act === 'rotl' ? -90 : 90;
            next = pages ? PM.rotatePages(S.packet, S.table, targets, d) : PM.rotateItems(S.packet, S.table, items, d);
            text = 'Rotated ' + plural(n, pages ? 'page' : 'file') + (act === 'rotl' ? ' left.' : ' right.');
            change(next, text);
            return;
        }
        if (act === 'delete') {
            next = pages ? PM.deletePages(S.packet, S.table, targets) : PM.removeItems(S.packet, items);
            ui.sel.clear();
            change(next);
            showToast('Deleted ' + plural(n, pages ? 'page' : 'file') + '.', true);
            return;
        }
        if (act === 'duplicate') {
            next = pages ? PM.duplicatePages(S.packet, S.table, targets) : PM.duplicateItems(S.packet, items);
            ui.sel.clear();
            change(next, 'Duplicated ' + plural(n, pages ? 'page' : 'file') + '.');
        }
    });

    // ---- Toolbar and options ----
    $('#pkUndo').addEventListener('click', doUndo);
    $('#pkRedo').addEventListener('click', doRedo);
    $('#pkClear').addEventListener('click', () => {
        const n = PM.itemOrder(S.packet).length;
        if (change(PM.clearPacket(S.packet))) showToast('Cleared the packet (' + plural(n, 'item') + ').', true);
    });
    $('#pkAllPages').addEventListener('click', () => {
        ui.allPages = !ui.allPages;
        ui.focusKey = null;
        render();
        say(ui.allPages ? 'Showing every page.' : 'Showing files.');
    });
    $('#pkPaper').addEventListener('change', e => {
        const paper = e.target.value;
        try { if (store) store.setItem('docist.paper', paper); } catch (err) { /* remembered for this page only */ }
        change(PM.setPaper(S.packet, paper), 'Paper set to ' + (paper === 'a4' ? 'A4' : 'Letter') + '.');
    });
    $('#pkOptNumbers').addEventListener('change', e => change(PM.setOption(S.packet, 'page_numbers', e.target.checked), e.target.checked ? 'Page numbers on.' : 'Page numbers off.'));
    $('#pkOptPosition').addEventListener('change', e => change(PM.setOption(S.packet, 'number_position', e.target.value)));
    $('#pkOptStart').addEventListener('change', e => change(PM.setOption(S.packet, 'start_number', e.target.value)));
    $('#pkOptBookmarks').addEventListener('change', e => change(PM.setOption(S.packet, 'bookmarks', e.target.checked)));
    $('#pkOptRecto').addEventListener('change', e => change(PM.setOption(S.packet, 'recto_starts', e.target.checked),
        e.target.checked ? 'Each section now starts on a right-hand page.' : 'Sections no longer forced to right-hand pages.'));
    $('#pkOutline').addEventListener('click', e => {
        const b = e.target.closest('.pk-o-link');
        if (!b) return;
        const el = list.querySelector('[data-fkey="c:' + b.dataset.item + ':open"]');
        if (el) { el.focus(); el.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' }); }
    });

    // ---- Global keys (skip text inputs, selects and open dialogs) ----
    document.addEventListener('keydown', e => {
        const t = e.target;
        const typing = t.matches && t.matches('input[type="text"], input[type="number"], textarea, select, [contenteditable="true"]');
        if (typing || document.querySelector('dialog[open]')) return;
        const k = e.key.toLowerCase();
        if ((e.ctrlKey || e.metaKey) && (k === 'z' || k === 'y')) {
            e.preventDefault();
            if (k === 'y' || e.shiftKey) doRedo(); else doUndo();
            return;
        }
        if ((e.ctrlKey || e.metaKey) && k === 'a' && !t.matches('input:not([type="checkbox"])')) {
            const kind = (t.closest && t.closest('.pk-tile')) || selKind() === 'p' ? 'p' : 'c';
            const keys = viewKeys(kind);
            if (!keys.length) return;
            e.preventDefault();
            ui.sel = new Set(keys);
            render();
            say(plural(keys.length, kind === 'p' ? 'page' : 'file') + ' selected.');
            return;
        }
        if (e.key === 'Escape' && menu.hidden && !ui.grab && (ui.sel.size || ui.selecting)) clearSel();
    });

    // ---- Preview dialog (§2.1) ----
    const dialog = $('#pkPreview');
    function previewSeq() {
        return PM.pagePlan(S.packet, counts()).pages;
    }
    function openPreviewAtPage(page, opener) {
        const seq = previewSeq();
        const pos = seq.findIndex(p => p.item === page.item && p.index === page.index);
        showPreview({ pos: Math.max(0, pos), item: null, opener });
    }
    function openPreviewAtItem(itemId, opener) {
        const seq = previewSeq();
        const pos = seq.findIndex(p => p.item === itemId);
        showPreview(pos === -1 ? { pos: -1, item: itemId, opener } : { pos, item: null, opener });
    }
    function showPreview(state) {
        ui.preview = state;
        renderPreview();
        if (!dialog.open) dialog.showModal();
        $('#pkPvClose').focus();
    }
    function nearestPos(itemId, forward) {
        // From a card with no pages, the nearest page before or after it.
        const seq = previewSeq();
        const order = PM.itemOrder(S.packet).map(e => e.item.id);
        const at = order.indexOf(itemId);
        if (forward) {
            for (let k = at + 1; k < order.length; k++) { const p = seq.findIndex(x => x.item === order[k]); if (p !== -1) return p; }
            return -1;
        }
        for (let k = at - 1; k >= 0; k--) {
            let last = -1;
            seq.forEach((x, j) => { if (x.item === order[k]) last = j; });
            if (last !== -1) return last;
        }
        return -1;
    }
    function renderPreview() {
        const pv = ui.preview;
        const seq = previewSeq();
        const page = $('#pkPvPage');
        const title = $('#pkPvTitle');
        const sub = $('#pkPvSub');
        const actions = ['#pkPvRotL', '#pkPvRotR', '#pkPvDel', '#pkPvMove'].map(s => $(s));
        if (pv.item && !PM.findItem(S.packet, pv.item)) { pv.item = null; pv.pos = Math.min(pv.pos, seq.length - 1); }
        if (pv.item) {
            const found = PM.findItem(S.packet, pv.item);
            const row = S.table[found.item.hash];
            const status = statusOf(found.item);
            title.textContent = found.item.label;
            sub.textContent = 'Not ready';
            page.innerHTML = '<div class="pk-pv-status"><p>' + escapeHtml(statusText(status, row, S.packet.options.paper, files.has(found.item.hash)) || 'Not ready yet.') + '</p></div>';
            actions.forEach(b => { b.disabled = true; });
            $('#pkPvPrev').disabled = nearestPos(pv.item, false) === -1;
            $('#pkPvNext').disabled = nearestPos(pv.item, true) === -1;
            return;
        }
        if (!seq.length) { closePreview(); return; }
        pv.pos = Math.max(0, Math.min(pv.pos, seq.length - 1));
        const p = seq[pv.pos];
        const found = p.item ? PM.findItem(S.packet, p.item) : null;
        let what;
        if (p.kind === 'file') {
            const item = found.item;
            const ref = PM.itemRefs(item, counts())[p.index];
            what = pageName(item, ref);
            const row = S.table[item.hash];
            page.innerHTML = sheet(ref.rot, imgTag(row, ref.src, 1200, what), 'pk-sheet-large');
            const url = thumbUrl(row, ref.src, 1200);
            if (!thumbUrls.has(url)) {
                // Show the small render at once while the large one loads.
                const small = thumbUrl(row, ref.src, 160);
                const img = page.querySelector('img');
                if (thumbUrls.has(small)) img.src = thumbUrls.get(small);
                wantThumb(url, true);
            }
        } else {
            what = { blank: 'Blank page', pad: 'Blank page added for double-sided printing', cover: 'Cover page', contents: 'Contents page', divider: 'Divider page' }[p.kind];
            page.innerHTML = sheet(0, '<span class="pk-sheet-note">' + escapeHtml(what) + '</span>', 'pk-sheet-large pk-sheet-blank');
        }
        title.textContent = 'Page ' + (pv.pos + 1) + ' of ' + seq.length + ' · ' + what;
        sub.textContent = (p.label != null ? 'Numbered ' + p.label : 'Not numbered') + (p.rot ? ' · rotated ' + p.rot + '°' : '');
        const movable = p.kind === 'file' || p.kind === 'blank';
        $('#pkPvRotL').disabled = p.kind !== 'file';
        $('#pkPvRotR').disabled = p.kind !== 'file';
        $('#pkPvDel').disabled = !movable;
        $('#pkPvMove').disabled = !movable;
        $('#pkPvPrev').disabled = pv.pos === 0;
        $('#pkPvNext').disabled = pv.pos === seq.length - 1;
    }
    function pvStep(d) {
        const pv = ui.preview;
        if (pv.item) {
            const p = nearestPos(pv.item, d > 0);
            if (p === -1) return;
            pv.item = null;
            pv.pos = p;
        } else {
            const n = previewSeq().length;
            if (pv.pos + d < 0 || pv.pos + d >= n) return;
            pv.pos += d;
        }
        renderPreview();
        say($('#pkPvTitle').textContent);
    }
    function closePreview() {
        if (dialog.open) dialog.close();
    }
    dialog.addEventListener('close', () => {
        const pv = ui.preview;
        ui.preview = null;
        if (pv && pv.opener && document.contains(pv.opener)) pv.opener.focus();
        else {
            const first = list.querySelector('.pk-open');
            if (first) first.focus();
        }
    });
    function currentPage() {
        const pv = ui.preview;
        if (!pv || pv.item) return null;
        const p = previewSeq()[pv.pos];
        return p && (p.kind === 'file' || p.kind === 'blank') ? { item: p.item, index: p.index } : null;
    }
    $('#pkPvClose').addEventListener('click', closePreview);
    $('#pkPvPrev').addEventListener('click', () => pvStep(-1));
    $('#pkPvNext').addEventListener('click', () => pvStep(1));
    $('#pkPvRotL').addEventListener('click', () => { const p = currentPage(); if (p) change(PM.rotatePages(S.packet, S.table, [p], -90), 'Rotated left.'); });
    $('#pkPvRotR').addEventListener('click', () => { const p = currentPage(); if (p) change(PM.rotatePages(S.packet, S.table, [p], 90), 'Rotated right.'); });
    $('#pkPvDel').addEventListener('click', () => {
        const p = currentPage();
        if (!p) return;
        change(PM.deletePages(S.packet, S.table, [p]));
        showToast('Deleted 1 page.', true);
        if (dialog.open) $('#pkPvDel').disabled ? $('#pkPvClose').focus() : $('#pkPvDel').focus();
    });
    $('#pkPvMove').addEventListener('click', () => {
        const p = currentPage();
        if (!p) return;
        const found = PM.findItem(S.packet, p.item);
        const key = found.item.kind === 'blank' ? 'c:' + p.item : keyFor(p);
        closePreview();
        openMoveDialog([key], null);
    });
    dialog.addEventListener('keydown', e => {
        if (e.target !== dialog && e.target.closest('input, select')) return;
        if (e.key === 'ArrowLeft') { e.preventDefault(); pvStep(-1); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); pvStep(1); }
    });
    dialog.addEventListener('click', e => { if (e.target === dialog) closePreview(); });
    // Swipe between pages on touch.
    let swipe = null;
    dialog.addEventListener('pointerdown', e => { if (e.pointerType !== 'mouse') swipe = { x: e.clientX, y: e.clientY }; });
    dialog.addEventListener('pointerup', e => {
        if (!swipe) return;
        const dx = e.clientX - swipe.x;
        const dy = e.clientY - swipe.y;
        swipe = null;
        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) pvStep(dx < 0 ? 1 : -1);
    });

    // ---- Build ----
    const buildMsg = $('#pkBuildMsg');
    $('#pkBuild').addEventListener('click', async () => {
        if (ui.building || !buildReadiness(S, cfg.opsLeft).ok) return;
        ui.building = true;
        ui.result = null;
        $('#pkResult').hidden = true;
        delete buildMsg.dataset.sticky;
        buildMsg.textContent = 'Building your packet…';
        buildMsg.className = 'pk-build-msg';
        render();
        let res;
        try {
            res = await postBuild(fetch, S, tabId);
        } catch (e) {
            res = null;
        }
        ui.building = false;
        buildMsg.dataset.sticky = '1';
        if (!res) {
            buildMsg.textContent = 'Network lost. Your arrangement is untouched; try again.';
            buildMsg.className = 'pk-build-msg pk-bad';
            render();
            return;
        }
        if (res.remaining != null && res.remaining !== '') {
            cfg.opsLeft = res.remaining === 'unlimited' ? null : parseInt(res.remaining, 10);
        }
        const c = classify(res.status, res.body);
        if (c.kind === 'ok') {
            const b = res.body;
            const link = $('#pkDownload');
            link.href = '/download?filename=' + encodeURIComponent(b.filename);
            link.setAttribute('download', resultName(S.packet));
            $('#pkResultText').textContent = 'Your packet is ready: ' + plural(b.pages, 'page') + '.';
            $('#pkResult').hidden = false;
            buildMsg.textContent = '';
            render();
            link.focus();
            say('Your packet is ready.');
            return;
        }
        buildMsg.className = 'pk-build-msg pk-bad';
        if (c.kind === 'csrf') { loseSession(); buildMsg.textContent = CSRF_MESSAGE; }
        else if (c.kind === 'bad_items') {
            const items = (res.body && res.body.items) || [];
            items.forEach(it => ui.buildErrors.set(it.id, it.code));
            const expired = items.filter(it => it.code === 'expired').map(it => {
                const found = PM.findItem(S.packet, it.id);
                return found && found.item.kind === 'file' && S.table[found.item.hash] ? S.table[found.item.hash].stage_id : null;
            }).filter(Boolean);
            if (expired.length) S = PM.expireStages(S, expired);
            buildMsg.textContent = c.error || 'Some files can’t be used. They are marked below.';
        } else if (c.kind === 'daily_limit') {
            cfg.opsLeft = 0;
            buildMsg.textContent = c.error;
        } else if (c.kind === 'busy') {
            buildMsg.textContent = 'Docist is busy with your other files. Try again in a moment.';
        } else {
            buildMsg.textContent = c.error || 'The build failed. Your arrangement is untouched.';
        }
        say(buildMsg.textContent);
        render();
    });
    $('#pkKeepEditing').addEventListener('click', () => {
        $('#pkResult').hidden = true;
        delete buildMsg.dataset.sticky;
        render();
        $('#pkBuild').focus();
    });

    // ---- SortableJS (optional: the Move menus do everything without it) ----
    let sortables = [];
    function clearSortableSelection() {
        if (!window.Sortable || !Sortable.utils || !Sortable.utils.deselect) return;
        document.querySelectorAll('.pk-msel').forEach(el => {
            try { Sortable.utils.deselect(el); } catch (e) { /* not a sortable child any more */ }
            el.classList.remove('pk-msel');
        });
    }
    function sortOptions(extra) {
        const opts = {
            animation: reduceMotion ? 0 : 150,
            forceFallback: true,
            fallbackOnBody: true,
            fallbackTolerance: 4,
            delayOnTouchOnly: true,
            delay: 200,
            ghostClass: 'pk-ghost',
            chosenClass: 'pk-chosen',
            fallbackClass: 'pk-dragging',
            filter: 'input, .pk-ibtn, .pk-plus, .pk-cardmsg, .pk-btn',
            preventOnFilter: false,
            multiDrag: true,
            selectedClass: 'pk-msel',
            avoidImplicitDeselect: true,
            onSelect(evt) {
                // Selection lives in the model; Sortable's own click
                // selection is undone at once.
                if (!ui.sel.has(evt.item.dataset.key)) Sortable.utils.deselect(evt.item);
            },
            onChoose(evt) {
                // Carry the model's selection into this container.
                clearSortableSelection();
                const key = evt.item.dataset.key;
                if (ui.sel.has(key)) {
                    Array.from(evt.from.children).forEach(el => {
                        if (ui.sel.has(el.dataset.key)) Sortable.utils.select(el);
                    });
                }
            },
            onStart(evt) {
                closeMenu(false);
                const n = ui.sel.has(evt.item.dataset.key) ? ui.sel.size : 1;
                if (n > 1) evt.item.dataset.count = n;
            },
        };
        return Object.assign(opts, extra);
    }
    // The dragged keys and how many stay before the first of them in the
    // container it was dropped in.
    function dropInfo(evt) {
        const key = evt.item.dataset.key;
        const keys = ui.sel.has(key) ? Array.from(ui.sel) : [key];
        const moving = new Set(keys);
        let index = 0;
        for (const el of Array.from(evt.to.children)) {
            if (moving.has(el.dataset.key)) break;
            if (el.dataset.key) index++;
        }
        return { keys, index };
    }
    function bindSortables() {
        sortables.forEach(s => s.destroy());
        sortables = [];
        if (!window.Sortable) return;
        list.querySelectorAll('.pk-items').forEach(el => {
            sortables.push(new Sortable(el, sortOptions({
                group: 'pk-cards',
                draggable: '.pk-card',
                handle: coarse ? '.pk-card-head .pk-handle' : '.pk-card-head',
                onEnd(evt) {
                    const { keys, index } = dropInfo(evt);
                    const ids = keys.filter(k => k[0] === 'c').map(k => k.slice(2));
                    const dest = { section: evt.to.dataset.section, index };
                    if (!moveItemsTo(ids, dest, false)) render();
                },
            })));
        });
        list.querySelectorAll('.pk-pages').forEach(el => {
            sortables.push(new Sortable(el, sortOptions({
                group: 'pk-pages',
                draggable: '.pk-tile',
                handle: coarse ? '.pk-handle' : null,
                onEnd(evt) {
                    const { keys, index } = dropInfo(evt);
                    const targets = keys.filter(k => k[0] === 'p').map(k => ({ item: k.split(':')[1], index: Number(k.split(':')[2]) }));
                    const dest = { item: evt.to.dataset.item, index };
                    if (!movePagesTo(targets, dest, false, 'open')) render();
                },
            })));
        });
    }

    // ---- Start ----
    fetch('/formats').then(r => r.json()).then(data => {
        accepted = (data.extensions || []).map(e => e.toLowerCase());
        fileInput.setAttribute('accept', accepted.join(','));
        $('#pkFormats').textContent = 'Accepts ' + accepted.map(e => e.replace(/^\./, '').toUpperCase()).join(', ') + '.';
    }).catch(() => { $('#pkFormats').textContent = ''; });
    $('#pkRetention').textContent = 'Files stay on this server while you work and are deleted after '
        + plural(Math.max(1, Math.round(cfg.ttlMinutes / 60)), 'hour') + ' without use.';

    if (store) PM.pruneDrafts(store, Date.now(), cfg.maxHours * 3600 * 1000, PM.DRAFTS_KEPT);
    const own = store ? PM.loadDraft(store, tabId) : null;
    render();
    if (hasItems(own)) {
        restoreFrom(own);
    } else if (store) {
        const latest = PM.latestDraft(store, tabId);
        if (latest && hasItems(latest.draft)) {
            const n = PM.itemOrder(latest.draft.packet).length;
            $('#pkRestoreText').textContent = 'Restore your last packet? (' + plural(n, 'item') + ', saved '
                + new Date(latest.saved).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ')';
            $('#pkRestore').hidden = false;
            $('#pkRestoreYes').addEventListener('click', () => {
                $('#pkRestore').hidden = true;
                restoreFrom(latest.draft);
                say('Packet restored.');
            });
            $('#pkRestoreNo').addEventListener('click', () => { $('#pkRestore').hidden = true; });
        }
    }
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initPacket);
    else initPacket();
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        escapeHtml,
        formatMb,
        retryAfterMs,
        classify,
        quotaMessage,
        stageFile,
        postHold,
        buildStages,
        buildReadiness,
        postBuild,
        stageWithRelief,
        createStageQueue,
        statusText,
        resultName,
        itemCodeText,
        CSRF_MESSAGE,
    };
}
