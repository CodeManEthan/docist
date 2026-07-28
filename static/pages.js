// ---------------------------------------------------------------------------
// PDF Page Tools — frontend logic
//
// Mirrors static/app.js: pure helpers first (unit-tested via node:test), then
// DOM wiring that consumes them, then a CommonJS export guard for Node.
// ---------------------------------------------------------------------------

// ============================ Pure functions ==============================

// Lowercased extension (including the dot), or '' when there is none.
function getExt(name) {
    const i = name.lastIndexOf('.');
    return i === -1 ? '' : name.slice(i).toLowerCase();
}

// Is this a .pdf filename? (case-insensitive)
function isPdf(filename) {
    return getExt(filename) === '.pdf';
}

// Validate a page-range spec the way the backend does (1-based, inclusive).
// Returns { valid: true } or { valid: false, error: '...' }. Does NOT check
// against a page count (the browser doesn't know it) — only syntax.
function validateRangeSpec(spec) {
    if (spec === null || spec === undefined || String(spec).trim() === '') {
        return { valid: false, error: "Enter a page range, e.g. 1-3,5,8-10." };
    }
    const tokens = String(spec).split(',');
    for (const raw of tokens) {
        const token = raw.trim();
        if (token === '') {
            return { valid: false, error: 'Empty range segment (check your commas).' };
        }
        if (token.includes('-')) {
            const parts = token.split('-');
            if (parts.length !== 2) {
                return { valid: false, error: `Invalid page range: '${token}'.` };
            }
            const a = parts[0].trim();
            const b = parts[1].trim();
            if (!/^\d+$/.test(a) || !/^\d+$/.test(b)) {
                return { valid: false, error: `Invalid page range: '${token}'.` };
            }
            const start = parseInt(a, 10);
            const end = parseInt(b, 10);
            if (start < 1 || end < 1) {
                return { valid: false, error: 'Page numbers start at 1.' };
            }
            if (start > end) {
                return { valid: false, error: `Range '${token}' is backwards.` };
            }
        } else {
            if (!/^\d+$/.test(token)) {
                return { valid: false, error: `Invalid page number: '${token}'.` };
            }
            if (parseInt(token, 10) < 1) {
                return { valid: false, error: 'Page numbers start at 1.' };
            }
        }
    }
    return { valid: true };
}

// Given the current UI state, validate it and build the FormData field map
// that POST /pages/run expects. Returns { ok: true, fields: {...} } or
// { ok: false, error: '...' }. `state` shape:
//   { operation, ranges, angle, splitMode, splitValue }
// (file is appended separately by the caller.)
// Validate an optional integer field within [min, max]. `raw` empty/undefined
// means "use the backend default" -> returns { valid: true, omit: true }.
function validateOptionalInt(raw, min, max, label) {
    if (raw === null || raw === undefined || String(raw).trim() === '') {
        return { valid: true, omit: true };
    }
    if (!/^-?\d+$/.test(String(raw).trim())) {
        return { valid: false, error: `${label} must be a whole number.` };
    }
    const n = parseInt(raw, 10);
    if (n < min || n > max) {
        return { valid: false, error: `${label} must be between ${min} and ${max}.` };
    }
    return { valid: true, value: n };
}

// Validate/normalise an OCR language spec. The browser can't know which
// packs Tesseract has installed, so this checks only shape: one or more
// codes (letters/digits) joined with '+'. Empty -> defaults to 'eng'.
// Returns { valid: true, value: 'eng' } or { valid: false, error: '...' }.
function validateLanguage(raw) {
    if (raw === null || raw === undefined || String(raw).trim() === '') {
        return { valid: true, value: 'eng' };
    }
    const codes = String(raw).trim().split('+').map(c => c.trim());
    for (const code of codes) {
        if (!/^[A-Za-z0-9_]+$/.test(code)) {
            return {
                valid: false,
                error: "Language must be Tesseract codes like 'eng' or 'eng+deu'.",
            };
        }
    }
    return { valid: true, value: codes.join('+') };
}

function buildRunPayload(state) {
    const op = state.operation;
    if (!['extract', 'remove', 'rotate', 'split', 'compress', 'ocr'].includes(op)) {
        return { ok: false, error: 'Choose an operation.' };
    }
    const fields = { operation: op };

    if (op === 'extract' || op === 'remove') {
        const check = validateRangeSpec(state.ranges);
        if (!check.valid) return { ok: false, error: check.error };
        fields.ranges = String(state.ranges).trim();
    } else if (op === 'rotate') {
        const angle = parseInt(state.angle, 10);
        if (![90, 180, 270].includes(angle)) {
            return { ok: false, error: 'Pick a rotation angle (90, 180 or 270).' };
        }
        fields.angle = String(angle);
        // Ranges are optional for rotate; validate only if provided.
        if (state.ranges && String(state.ranges).trim() !== '') {
            const check = validateRangeSpec(state.ranges);
            if (!check.valid) return { ok: false, error: check.error };
            fields.ranges = String(state.ranges).trim();
        }
    } else if (op === 'compress') {
        const q = validateOptionalInt(state.imageQuality, 10, 95, 'Image quality');
        if (!q.valid) return { ok: false, error: q.error };
        if (!q.omit) fields.image_quality = String(q.value);
        const dpi = validateOptionalInt(state.imageMaxDpi, 72, 300, 'Max image DPI');
        if (!dpi.valid) return { ok: false, error: dpi.error };
        if (!dpi.omit) fields.image_max_dpi = String(dpi.value);
    } else if (op === 'ocr') {
        const lang = validateLanguage(state.language);
        if (!lang.valid) return { ok: false, error: lang.error };
        fields.language = lang.value;
        if (state.deskew) fields.deskew = 'true';
        if (state.force) fields.force = 'true';
    } else { // split
        const mode = state.splitMode;
        if (mode !== 'every_n' && mode !== 'ranges') {
            return { ok: false, error: 'Choose a split mode.' };
        }
        fields.split_mode = mode;
        if (mode === 'every_n') {
            const n = parseInt(state.splitValue, 10);
            if (!Number.isInteger(n) || n < 1) {
                return { ok: false, error: "'Every N pages' must be a whole number ≥ 1." };
            }
            fields.split_value = String(n);
        } else {
            const raw = state.splitValue;
            if (!raw || String(raw).trim() === '') {
                return { ok: false, error: "Enter one or more ranges (separate with ';')." };
            }
            const specs = String(raw).split(';').map(s => s.trim()).filter(Boolean);
            if (specs.length === 0) {
                return { ok: false, error: "Enter one or more ranges (separate with ';')." };
            }
            for (const spec of specs) {
                const check = validateRangeSpec(spec);
                if (!check.valid) return { ok: false, error: check.error };
            }
            fields.split_value = specs.join(';');
        }
    }
    return { ok: true, fields };
}

// Escape a string for safe interpolation into HTML.
function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ---- Page thumbnails (POST /preview/thumbs) ----

// The preview endpoint, and the server-side cap on rendered pages.
const THUMB_ENDPOINT = '/preview/thumbs';

// Append a clicked page number to a comma-separated ranges spec.
//
//   ''      + 3 -> '3'
//   '1-2'   + 3 -> '1-2,3'
//   '1,'    + 3 -> '1,3'      (a dangling comma is absorbed)
//
// A page already present as an exact token is not added twice; pages merely
// *covered* by an existing range ('1-5' + 3) are still appended, because the
// backend dedupes overlapping ranges anyway and second-guessing the user's
// intent here would be worse than a harmless duplicate.
// A non-positive / non-integer page leaves the spec untouched.
function appendPageToRanges(current, page) {
    const n = parseInt(page, 10);
    const spec = (current === null || current === undefined) ? '' : String(current);
    if (!Number.isInteger(n) || n < 1) return spec;

    // Trim whitespace and any trailing commas, then split into clean tokens.
    const tokens = spec.split(',').map(t => t.trim()).filter(Boolean);
    if (tokens.includes(String(n))) return tokens.join(',');
    tokens.push(String(n));
    return tokens.join(',');
}

// Which ranges input does a clicked thumbnail feed, for the current
// operation? Operations without a page-ranges box return null (the click is
// then a no-op rather than writing into a hidden field).
const RANGES_FIELDS = {
    extract: 'extractRanges',
    remove: 'removeRanges',
    rotate: 'rotateRanges',
};

function rangesFieldFor(operation) {
    return RANGES_FIELDS[operation] || null;
}

// The "+N more pages" note shown when the preview was capped. Returns '' when
// every page was rendered (or the numbers make no sense).
function morePagesNote(pages, rendered) {
    const total = parseInt(pages, 10);
    const shown = parseInt(rendered, 10);
    if (!Number.isInteger(total) || !Number.isInteger(shown)) return '';
    const remaining = total - shown;
    if (remaining < 1) return '';
    return '+' + remaining + ' more page' + (remaining === 1 ? '' : 's');
}

// Normalise a /preview/thumbs payload into { pages, rendered, thumbs }.
// Anything unusable comes back as an empty preview so callers have one shape
// to render and no reason to inspect the raw response.
function normalizeThumbs(payload) {
    if (!payload || !Array.isArray(payload.thumbs)) {
        return { pages: 0, rendered: 0, thumbs: [] };
    }
    const thumbs = payload.thumbs.filter(
        t => typeof t === 'string' && t.startsWith('data:image/')
    );
    const pages = parseInt(payload.pages, 10);
    return {
        pages: Number.isInteger(pages) && pages > 0 ? pages : thumbs.length,
        rendered: thumbs.length,
        thumbs: thumbs,
    };
}

// ============================== DOM wiring =================================

function initPagesApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileName = document.getElementById('fileName');
    const runBtn = document.getElementById('runBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');
    const opRadios = Array.from(document.querySelectorAll('input[name="operation"]'));
    const panels = {
        extract: document.getElementById('panel-extract'),
        remove: document.getElementById('panel-remove'),
        rotate: document.getElementById('panel-rotate'),
        split: document.getElementById('panel-split'),
        compress: document.getElementById('panel-compress'),
        ocr: document.getElementById('panel-ocr'),
    };

    let selectedFile = null;

    function currentOperation() {
        const checked = opRadios.find(r => r.checked);
        return checked ? checked.value : null;
    }

    function showActivePanel() {
        const op = currentOperation();
        Object.entries(panels).forEach(([name, el]) => {
            if (el) el.style.display = name === op ? 'block' : 'none';
        });
        updateRunBtn();
    }

    function updateRunBtn() {
        runBtn.disabled = !selectedFile;
    }

    // ---- Upload via click or drag-drop ----
    uploadArea.addEventListener('click', () => fileInput.click());

    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });
    uploadArea.addEventListener('dragleave', () => {
        uploadArea.classList.remove('dragover');
    });
    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        const files = Array.from(e.dataTransfer.files);
        if (files.length) acceptFile(files[0]);
    });
    fileInput.addEventListener('change', (e) => {
        const files = Array.from(e.target.files);
        if (files.length) acceptFile(files[0]);
        fileInput.value = '';
    });

    function acceptFile(file) {
        if (!isPdf(file.name)) {
            showMessage('error', 'Please choose a .pdf file.');
            return;
        }
        selectedFile = file;
        fileName.textContent = file.name;
        fileName.style.display = 'block';
        message.style.display = 'none';
        updateRunBtn();
        loadThumbs(file);
    }

    // ---- Page thumbnail grid ----
    const thumbGrid = document.getElementById('thumbGrid');
    const thumbNote = document.getElementById('thumbNote');
    const thumbHint = document.getElementById('thumbHint');

    function hideThumbs() {
        if (thumbGrid) {
            thumbGrid.innerHTML = '';
            thumbGrid.style.display = 'none';
        }
        if (thumbNote) thumbNote.style.display = 'none';
        if (thumbHint) thumbHint.style.display = 'none';
    }

    function showThumbSpinner() {
        if (!thumbGrid) return;
        thumbGrid.innerHTML =
            '<span class="page-thumb thumb-loading" aria-hidden="true"></span>'.repeat(4);
        thumbGrid.style.display = '';
        if (thumbNote) thumbNote.style.display = 'none';
        if (thumbHint) thumbHint.style.display = 'none';
    }

    async function loadThumbs(file) {
        if (!thumbGrid) return;
        showThumbSpinner();
        let preview = { pages: 0, rendered: 0, thumbs: [] };
        try {
            const formData = new FormData();
            formData.append('file', file);
            const response = await fetch(THUMB_ENDPOINT, {
                method: 'POST',
                body: formData,
            });
            if (response.ok) preview = normalizeThumbs(await response.json());
        } catch (err) {
            // Previews are a nicety: a failure just hides the grid. No noise.
        }
        // A different file may have been picked while this was in flight.
        if (selectedFile !== file) return;
        renderThumbs(preview);
    }

    function renderThumbs(preview) {
        if (!preview.thumbs.length) {
            hideThumbs();
            return;
        }
        thumbGrid.innerHTML = '';
        preview.thumbs.forEach((src, i) => {
            const tile = document.createElement('button');
            tile.type = 'button';
            tile.className = 'page-thumb';
            tile.title = 'Add page ' + (i + 1) + ' to the page ranges';
            tile.innerHTML =
                '<img src="' + escapeHtml(src) + '" alt="Page ' + (i + 1) + '">'
                + '<span class="page-thumb-caption">' + (i + 1) + '</span>';
            tile.addEventListener('click', () => addPageToRanges(i + 1));
            thumbGrid.appendChild(tile);
        });
        thumbGrid.style.display = '';
        if (thumbHint) thumbHint.style.display = '';

        const note = morePagesNote(preview.pages, preview.rendered);
        if (thumbNote) {
            thumbNote.textContent = note;
            thumbNote.style.display = note ? '' : 'none';
        }
    }

    function addPageToRanges(page) {
        const fieldId = rangesFieldFor(currentOperation());
        if (!fieldId) return;
        const input = document.getElementById(fieldId);
        if (!input) return;
        input.value = appendPageToRanges(input.value, page);
    }

    function showMessage(kind, text) {
        message.className = 'message ' + kind;
        message.textContent = text;
        message.style.display = 'block';
    }

    opRadios.forEach(r => r.addEventListener('change', showActivePanel));

    // ---- Run ----
    runBtn.addEventListener('click', async () => {
        if (!selectedFile) return;

        const state = {
            operation: currentOperation(),
            ranges: (document.getElementById('extractRanges') &&
                     currentOperation() === 'extract')
                ? document.getElementById('extractRanges').value
                : (currentOperation() === 'remove'
                    ? document.getElementById('removeRanges').value
                    : (currentOperation() === 'rotate'
                        ? document.getElementById('rotateRanges').value
                        : '')),
            angle: document.getElementById('rotateAngle')
                ? document.getElementById('rotateAngle').value : '',
            splitMode: (document.querySelector('input[name="split_mode"]:checked') || {}).value,
            splitValue: '',
            imageQuality: document.getElementById('compressQuality')
                ? document.getElementById('compressQuality').value : '',
            imageMaxDpi: document.getElementById('compressDpi')
                ? document.getElementById('compressDpi').value : '',
            language: document.getElementById('ocrLanguage')
                ? document.getElementById('ocrLanguage').value : '',
            deskew: !!(document.getElementById('ocrDeskew')
                && document.getElementById('ocrDeskew').checked),
            force: !!(document.getElementById('ocrForce')
                && document.getElementById('ocrForce').checked),
        };
        if (state.splitMode === 'every_n') {
            state.splitValue = document.getElementById('splitEveryN').value;
        } else if (state.splitMode === 'ranges') {
            state.splitValue = document.getElementById('splitRanges').value;
        }

        const payload = buildRunPayload(state);
        if (!payload.ok) {
            showMessage('error', payload.error);
            return;
        }

        const formData = new FormData();
        formData.append('file', selectedFile);
        Object.entries(payload.fields).forEach(([k, v]) => formData.append(k, v));

        runBtn.disabled = true;
        loading.style.display = 'block';
        message.style.display = 'none';
        clearDownload();

        try {
            const response = await fetch('/pages/run', { method: 'POST', body: formData });
            const result = await response.json();
            loading.style.display = 'none';

            if (response.ok && result.success) {
                showMessage('success', result.message);
                addDownload(result.filename);
            } else {
                showMessage('error', result.error || 'An error occurred.');
            }
        } catch (err) {
            loading.style.display = 'none';
            showMessage('error', 'Failed to connect to server.');
        } finally {
            updateRunBtn();
        }
    });

    function clearDownload() {
        const existing = document.getElementById('downloadBtn');
        if (existing) existing.remove();
    }

    function addDownload(filename) {
        clearDownload();
        const downloadBtn = document.createElement('button');
        downloadBtn.className = 'btn download-btn';
        downloadBtn.id = 'downloadBtn';
        downloadBtn.textContent = '⬇️ Download Result';
        downloadBtn.onclick = () => {
            window.location.href = '/download?filename=' + encodeURIComponent(filename);
            setTimeout(() => {
                downloadBtn.textContent = '✓ Downloaded! Click to download again';
            }, 1000);
        };
        runBtn.parentNode.insertBefore(downloadBtn, message);
    }

    showActivePanel();
    updateRunBtn();
}

// Wire up once the DOM is ready (guarded for Node, where document is undefined).
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPagesApp);
    } else {
        initPagesApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getExt,
        isPdf,
        validateRangeSpec,
        validateOptionalInt,
        validateLanguage,
        buildRunPayload,
        escapeHtml,
        appendPageToRanges,
        rangesFieldFor,
        RANGES_FIELDS,
        morePagesNote,
        normalizeThumbs,
        THUMB_ENDPOINT,
    };
}
