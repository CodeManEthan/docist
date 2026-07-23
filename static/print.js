// ---------------------------------------------------------------------------
// PDF Print Prep — frontend logic
//
// Mirrors static/pages.js: pure helpers first (unit-tested via node:test), then
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

// Saddle-stitch sheet sequence — a faithful mirror of pdf_ops.imposition
// .booklet_page_order. `pageCount` is the number of real source pages; it is
// padded up to the next multiple of 4 internally. Returns an array of
// [left, right] pairs (one per 2-up landscape sheet) using 0-based source
// indices, with `null` where a slot falls on a padding (blank) page.
function bookletPageOrder(pageCount) {
    if (pageCount === null || pageCount === undefined || pageCount < 1) {
        throw new Error('A booklet needs at least one page.');
    }
    const padded = pageCount + ((4 - (pageCount % 4)) % 4);
    const idx = (oneBased) => (oneBased <= pageCount ? oneBased - 1 : null);

    const pairs = [];
    let lo = 1;
    let hi = padded;
    let flip = false;
    while (lo < hi) {
        const [left, right] = flip ? [lo, hi] : [hi, lo];
        pairs.push([idx(left), idx(right)]);
        lo += 1;
        hi -= 1;
        flip = !flip;
    }
    return pairs;
}

// Validate the Print-Prep UI state. Returns { valid: true } or
// { valid: false, error: '...' }. `state` shape: { operation, n }.
function validatePrintOptions(state) {
    const op = state.operation;
    if (op !== 'nup' && op !== 'booklet') {
        return { valid: false, error: 'Choose an operation: N-up or booklet.' };
    }
    if (op === 'nup') {
        const n = parseInt(state.n, 10);
        if (n !== 2 && n !== 4) {
            return { valid: false, error: 'Pages per sheet must be 2 or 4.' };
        }
    }
    return { valid: true };
}

// Given the current UI state, validate it and build the FormData field map that
// POST /print/run expects. Returns { ok: true, fields: {...} } or
// { ok: false, error: '...' }. (file is appended separately by the caller.)
function buildPrintPayload(state) {
    const check = validatePrintOptions(state);
    if (!check.valid) return { ok: false, error: check.error };

    const fields = { operation: state.operation };
    if (state.operation === 'nup') {
        fields.n = String(parseInt(state.n, 10));
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

function initPrintApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileName = document.getElementById('fileName');
    const runBtn = document.getElementById('runBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');
    const opRadios = Array.from(document.querySelectorAll('input[name="operation"]'));
    const panels = {
        nup: document.getElementById('panel-nup'),
        booklet: document.getElementById('panel-booklet'),
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
            n: document.getElementById('nupN')
                ? document.getElementById('nupN').value : '',
        };

        const payload = buildPrintPayload(state);
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
            const response = await fetch('/print/run', { method: 'POST', body: formData });
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
        document.addEventListener('DOMContentLoaded', initPrintApp);
    } else {
        initPrintApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getExt,
        isPdf,
        bookletPageOrder,
        validatePrintOptions,
        buildPrintPayload,
        escapeHtml,
    };
}
