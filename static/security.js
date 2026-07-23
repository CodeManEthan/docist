// ---------------------------------------------------------------------------
// Watermark & Security — frontend logic
//
// Split like app.js:
//   1. Pure functions (no DOM access) — unit-tested via node:test.
//   2. DOM wiring — runs in the browser on DOMContentLoaded, using the pure
//      functions rather than duplicating their logic.
// A CommonJS export guard at the bottom exposes the pure functions to Node.
// ---------------------------------------------------------------------------

// ============================ Pure functions ==============================

const OPERATIONS = ['watermark', 'protect', 'unlock'];
// The stamp operations added to this page. Kept separate from OPERATIONS so the
// original export stays byte-for-byte stable for existing tests; ALL_OPERATIONS
// is the full set the form actually accepts.
const STAMP_OPERATIONS = ['headerfooter', 'bates'];
const ALL_OPERATIONS = [...OPERATIONS, ...STAMP_OPERATIONS];
const POSITIONS = ['center', 'header', 'footer'];
const BATES_POSITIONS = ['bottom-right', 'bottom-left', 'top-right', 'top-left'];
const HEADERFOOTER_SLOTS = [
    'headerLeft', 'headerCenter', 'headerRight',
    'footerLeft', 'footerCenter', 'footerRight',
];

// Lowercased extension (including the dot), or '' when there is none.
function getExt(name) {
    const i = name.lastIndexOf('.');
    return i === -1 ? '' : name.slice(i).toLowerCase();
}

// Is this a PDF filename? (case-insensitive)
function isPdf(filename) {
    return getExt(filename) === '.pdf';
}

// Validate the watermark-specific options in `state`.
// Returns { valid, error } — error is null when valid.
function validateWatermarkOptions(state) {
    const text = (state.text || '').trim();
    if (!text) return { valid: false, error: 'Watermark text is required' };

    if (!POSITIONS.includes(state.position)) {
        return { valid: false, error: 'Invalid position' };
    }

    const opacity = Number(state.opacity);
    if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) {
        return { valid: false, error: 'Opacity must be between 0 and 1' };
    }

    const fontSize = Number(state.fontSize);
    if (!Number.isFinite(fontSize) || fontSize <= 0) {
        return { valid: false, error: 'Font size must be a positive number' };
    }

    const rotation = Number(state.rotation);
    if (!Number.isFinite(rotation)) {
        return { valid: false, error: 'Rotation must be a number' };
    }

    return { valid: true, error: null };
}

// Validate the header/footer-specific options in `state`.
// Returns { valid, error } — error is null when valid.
function validateHeaderFooterOptions(state) {
    const anyText = HEADERFOOTER_SLOTS.some(slot => (state[slot] || '').trim());
    if (!anyText) {
        return { valid: false, error: 'Fill at least one header/footer slot' };
    }
    const fontSize = Number(state.fontSize);
    if (!Number.isFinite(fontSize) || fontSize <= 0) {
        return { valid: false, error: 'Font size must be a positive number' };
    }
    return { valid: true, error: null };
}

// Validate the Bates-numbering options in `state`.
// Returns { valid, error } — error is null when valid.
function validateBatesOptions(state) {
    const start = Number(state.start);
    if (!Number.isInteger(start) || start < 0) {
        return { valid: false, error: 'Start must be a whole number ≥ 0' };
    }
    const digits = Number(state.digits);
    if (!Number.isInteger(digits) || digits < 3 || digits > 10) {
        return { valid: false, error: 'Digits must be between 3 and 10' };
    }
    if (!BATES_POSITIONS.includes(state.position)) {
        return { valid: false, error: 'Invalid position' };
    }
    return { valid: true, error: null };
}

// Validate the whole form for the chosen operation.
// Returns { valid, error }.
function validateSecurityState(state) {
    if (!state.file) return { valid: false, error: 'Select a PDF first' };
    if (!ALL_OPERATIONS.includes(state.operation)) {
        return { valid: false, error: 'Choose an operation' };
    }
    if (state.operation === 'watermark') {
        return validateWatermarkOptions(state);
    }
    if (state.operation === 'headerfooter') {
        return validateHeaderFooterOptions(state);
    }
    if (state.operation === 'bates') {
        return validateBatesOptions(state);
    }
    // protect / unlock
    if (!state.password) {
        return { valid: false, error: 'Password is required' };
    }
    return { valid: true, error: null };
}

// Build the form-field payload (excluding the file itself) for the chosen
// operation. Returns a plain object of string values ready for FormData.
function buildSecurityPayload(state) {
    const payload = { operation: state.operation };
    if (state.operation === 'watermark') {
        payload.text = (state.text || '').trim();
        payload.position = state.position;
        payload.opacity = String(state.opacity);
        payload.font_size = String(state.fontSize);
        payload.rotation = String(state.rotation);
    } else if (state.operation === 'headerfooter') {
        payload.header_left = state.headerLeft || '';
        payload.header_center = state.headerCenter || '';
        payload.header_right = state.headerRight || '';
        payload.footer_left = state.footerLeft || '';
        payload.footer_center = state.footerCenter || '';
        payload.footer_right = state.footerRight || '';
        payload.font_size = String(state.fontSize);
    } else if (state.operation === 'bates') {
        payload.prefix = state.prefix || '';
        payload.start = String(state.start);
        payload.digits = String(state.digits);
        payload.position = state.position;
    } else {
        payload.password = state.password || '';
    }
    return payload;
}

// Escape a string for safe interpolation into HTML.
function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ============================== DOM wiring =================================

function initSecurityApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const operationSel = document.getElementById('operation');
    const watermarkOptions = document.getElementById('watermarkOptions');
    const passwordOptions = document.getElementById('passwordOptions');
    const headerFooterOptions = document.getElementById('headerFooterOptions');
    const batesOptions = document.getElementById('batesOptions');
    const stampCommonOptions = document.getElementById('stampCommonOptions');
    const runBtn = document.getElementById('runBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');

    let selectedFile = null;

    function readState() {
        const op = operationSel.value;
        return {
            file: selectedFile,
            operation: op,
            text: document.getElementById('text').value,
            // `position` is shared between watermark and bates panels — read
            // whichever select belongs to the active operation.
            position: op === 'bates'
                ? document.getElementById('batesPosition').value
                : document.getElementById('position').value,
            opacity: document.getElementById('opacity').value,
            fontSize: op === 'bates' || op === 'headerfooter'
                ? document.getElementById('stampFontSize').value
                : document.getElementById('fontSize').value,
            rotation: document.getElementById('rotation').value,
            password: document.getElementById('password').value,
            headerLeft: document.getElementById('headerLeft').value,
            headerCenter: document.getElementById('headerCenter').value,
            headerRight: document.getElementById('headerRight').value,
            footerLeft: document.getElementById('footerLeft').value,
            footerCenter: document.getElementById('footerCenter').value,
            footerRight: document.getElementById('footerRight').value,
            prefix: document.getElementById('prefix').value,
            start: document.getElementById('start').value,
            digits: document.getElementById('digits').value,
        };
    }

    function syncOptionPanels() {
        const op = operationSel.value;
        watermarkOptions.style.display = op === 'watermark' ? 'block' : 'none';
        headerFooterOptions.style.display = op === 'headerfooter' ? 'block' : 'none';
        batesOptions.style.display = op === 'bates' ? 'block' : 'none';
        stampCommonOptions.style.display =
            (op === 'headerfooter' || op === 'bates') ? 'block' : 'none';
        passwordOptions.style.display =
            (op === 'protect' || op === 'unlock') ? 'block' : 'none';
        runBtn.textContent = {
            watermark: 'Apply Watermark',
            protect: 'Protect PDF',
            unlock: 'Unlock PDF',
            headerfooter: 'Apply Header/Footer',
            bates: 'Apply Bates Numbers',
        }[op] || 'Apply';
    }

    function updateRunBtn() {
        runBtn.disabled = !selectedFile;
    }

    function displayFile() {
        fileList.innerHTML = '';
        if (!selectedFile) return;
        const div = document.createElement('div');
        div.className = 'file-item';
        div.innerHTML = `
            <span class="file-info">${escapeHtml(selectedFile.name)}</span>
            <span class="file-actions">
                <button class="icon-btn remove-btn" title="Remove">✕</button>
            </span>
        `;
        div.querySelector('.remove-btn').addEventListener('click', () => {
            selectedFile = null;
            displayFile();
            updateRunBtn();
        });
        fileList.appendChild(div);
    }

    function handleFile(file) {
        if (!file) return;
        if (!isPdf(file.name)) {
            message.className = 'message error';
            message.textContent = 'Only PDF files are supported';
            message.style.display = 'block';
            return;
        }
        selectedFile = file;
        message.style.display = 'none';
        displayFile();
        updateRunBtn();
        fileInput.value = '';
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
        const file = e.dataTransfer.files && e.dataTransfer.files[0];
        handleFile(file);
    });

    fileInput.addEventListener('change', (e) => {
        handleFile(e.target.files && e.target.files[0]);
    });

    operationSel.addEventListener('change', () => {
        syncOptionPanels();
        message.style.display = 'none';
    });

    // ---- Run ----
    runBtn.addEventListener('click', async () => {
        const state = readState();
        const check = validateSecurityState(state);
        if (!check.valid) {
            message.className = 'message error';
            message.textContent = check.error;
            message.style.display = 'block';
            return;
        }

        const formData = new FormData();
        formData.append('file', state.file);
        const payload = buildSecurityPayload(state);
        Object.keys(payload).forEach(k => formData.append(k, payload[k]));

        runBtn.disabled = true;
        loading.style.display = 'block';
        message.style.display = 'none';

        try {
            const response = await fetch('/security/run', {
                method: 'POST',
                body: formData,
            });
            const result = await response.json();
            loading.style.display = 'none';

            if (response.ok) {
                message.className = 'message success';
                message.textContent = result.message;
                message.style.display = 'block';

                const downloadBtn = document.createElement('button');
                downloadBtn.className = 'btn download-btn';
                downloadBtn.textContent = '⬇️ Download PDF';
                downloadBtn.onclick = () => {
                    window.location.href = result.download_url;
                    setTimeout(() => {
                        downloadBtn.textContent = '✓ Downloaded! Click to download again';
                    }, 1000);
                };
                runBtn.parentNode.insertBefore(downloadBtn, message);
                runBtn.disabled = false;
            } else {
                message.className = 'message error';
                message.textContent = result.error || 'An error occurred';
                message.style.display = 'block';
                runBtn.disabled = false;
            }
        } catch (err) {
            loading.style.display = 'none';
            message.className = 'message error';
            message.textContent = 'Failed to connect to server';
            message.style.display = 'block';
            runBtn.disabled = false;
        }
    });

    syncOptionPanels();
    updateRunBtn();
}

// Wire up once the DOM is ready (guarded for Node, where `document` is undefined).
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSecurityApp);
    } else {
        initSecurityApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        OPERATIONS,
        STAMP_OPERATIONS,
        ALL_OPERATIONS,
        POSITIONS,
        BATES_POSITIONS,
        HEADERFOOTER_SLOTS,
        getExt,
        isPdf,
        validateWatermarkOptions,
        validateHeaderFooterOptions,
        validateBatesOptions,
        validateSecurityState,
        buildSecurityPayload,
        escapeHtml,
    };
}
