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

function buildRunPayload(state) {
    const op = state.operation;
    if (!['extract', 'remove', 'rotate', 'split', 'compress'].includes(op)) {
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
        buildRunPayload,
        escapeHtml,
    };
}
