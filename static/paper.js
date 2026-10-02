// ---------------------------------------------------------------------------
// Paper choice (Letter or A4), shared by Merge, Convert and Print Prep
//
// Each of those pages has a <select class="paper-select">. This file restores
// the last choice from localStorage under 'docist.paper' and saves it on
// change; the tool scripts send the select's value as the `paper` form field.
// Storage can be missing or throw (private windows, blocked site data), so
// every read and write is guarded and the page works without it: the default
// is Letter.
//
// Pure helpers first (unit-tested via node:test), then the browser wiring,
// then the CommonJS export guard for Node, as in the other static scripts.
// ---------------------------------------------------------------------------

const PAPER_KEY = 'docist.paper';
const PAPERS = ['letter', 'a4'];
const DEFAULT_PAPER = 'letter';

// ============================ Pure functions ==============================

// The remembered paper, or 'letter' when storage is empty, unusable or holds
// something that isn't a paper.
function readPaper(storage) {
    try {
        const value = storage ? storage.getItem(PAPER_KEY) : null;
        return PAPERS.includes(value) ? value : DEFAULT_PAPER;
    } catch (e) {
        return DEFAULT_PAPER;
    }
}

// Remember a paper. Returns true when it was stored; an unknown value or a
// storage that throws stores nothing.
function writePaper(storage, value) {
    if (!PAPERS.includes(value)) return false;
    try {
        if (!storage) return false;
        storage.setItem(PAPER_KEY, value);
        return true;
    } catch (e) {
        return false;
    }
}

// ============================ Browser wiring ==============================

function pageStorage() {
    try {
        return window.localStorage;
    } catch (e) {
        return null;
    }
}

function initPaperSelects() {
    const storage = pageStorage();
    document.querySelectorAll('select.paper-select').forEach(select => {
        select.value = readPaper(storage);
        select.addEventListener('change', () => writePaper(storage, select.value));
    });
}

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', initPaperSelects);
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        PAPER_KEY,
        PAPERS,
        DEFAULT_PAPER,
        readPaper,
        writePaper,
    };
}
