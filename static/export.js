// ---------------------------------------------------------------------------
// PDF Export — frontend logic
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

// Accepted formats / DPI window — must match pdf_ops/export.py.
const VALID_FORMATS = ['png', 'jpg'];
const MIN_DPI = 30;
const MAX_DPI = 600;

// Validate the export options in `state` without building a payload.
// `state` shape: { operation, fmt, dpi }. Returns { valid: true } or
// { valid: false, error: '...' }.
function validateExportOptions(state) {
    const op = state.operation;
    if (op !== 'images' && op !== 'text') {
        return { valid: false, error: 'Choose an operation: images or text.' };
    }
    if (op === 'images') {
        const fmt = String(state.fmt || '').toLowerCase();
        if (!VALID_FORMATS.includes(fmt)) {
            return { valid: false, error: "Pick an image format (PNG or JPEG)." };
        }
        if (state.dpi === null || state.dpi === undefined ||
                String(state.dpi).trim() === '') {
            return { valid: false, error: `Enter a DPI between ${MIN_DPI} and ${MAX_DPI}.` };
        }
        if (!/^\d+$/.test(String(state.dpi).trim())) {
            return { valid: false, error: 'DPI must be a whole number.' };
        }
        const dpi = parseInt(state.dpi, 10);
        if (dpi < MIN_DPI || dpi > MAX_DPI) {
            return { valid: false, error: `DPI must be between ${MIN_DPI} and ${MAX_DPI}.` };
        }
    }
    return { valid: true };
}

// Given the current UI state, validate it and build the FormData field map
// that POST /export/run expects. Returns { ok: true, fields: {...} } or
// { ok: false, error: '...' }. `state` shape: { operation, fmt, dpi }.
// (file is appended separately by the caller.)
function buildExportPayload(state) {
    const check = validateExportOptions(state);
    if (!check.valid) return { ok: false, error: check.error };

    const op = state.operation;
    const fields = { operation: op };
    if (op === 'images') {
        fields.fmt = String(state.fmt).toLowerCase();
        fields.dpi = String(parseInt(state.dpi, 10));
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

function initExportApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileName = document.getElementById('fileName');
    const runBtn = document.getElementById('runBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');
    const opRadios = Array.from(document.querySelectorAll('input[name="operation"]'));
    const panels = {
        images: document.getElementById('panel-images'),
        text: document.getElementById('panel-text'),
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

        const fmtEl = document.getElementById('imageFormat');
        const dpiEl = document.getElementById('imageDpi');
        const state = {
            operation: currentOperation(),
            fmt: fmtEl ? fmtEl.value : '',
            dpi: dpiEl ? dpiEl.value : '',
        };

        const payload = buildExportPayload(state);
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
            const response = await fetch('/export/run', { method: 'POST', body: formData });
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
        document.addEventListener('DOMContentLoaded', initExportApp);
    } else {
        initExportApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getExt,
        isPdf,
        validateExportOptions,
        buildExportPayload,
        escapeHtml,
    };
}
