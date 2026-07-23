// ---------------------------------------------------------------------------
// Convert Files — frontend logic
//
// Mirrors static/export.js: pure helpers first (unit-tested via node:test), then
// DOM wiring that consumes them, then a CommonJS export guard for Node.
// ---------------------------------------------------------------------------

// ============================ Pure functions ==============================

// Lowercased extension (including the dot), or '' when there is none.
function getExt(name) {
    const i = String(name).lastIndexOf('.');
    return i === -1 ? '' : String(name).slice(i).toLowerCase();
}

// A human label for a target extension: '.png' -> 'PNG', '.xlsx' -> 'XLSX'.
function buildTargetLabel(ext) {
    const clean = String(ext || '').replace(/^\./, '');
    return clean.toUpperCase();
}

// Escape a string for safe interpolation into HTML.
function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Turn a matrix object { '.csv': ['.xlsx', '.json'], ... } into sorted display
// lines like 'CSV → XLSX, JSON'. Sources are sorted; empty targets are skipped.
function formatMatrix(matrixObj) {
    if (!matrixObj || typeof matrixObj !== 'object') return [];
    return Object.keys(matrixObj)
        .sort()
        .filter(src => Array.isArray(matrixObj[src]) && matrixObj[src].length)
        .map(src => {
            const targets = matrixObj[src].map(buildTargetLabel).join(', ');
            return `${buildTargetLabel(src)} → ${targets}`;
        });
}

// Validate the current UI state before POSTing. `state` shape:
// { ext, target, targets }. Returns { valid: true } or { valid: false, error }.
function validateConvertState(state) {
    const ext = getExt(state.ext || '');
    if (!ext) {
        return { valid: false, error: 'Select a file to convert.' };
    }
    const targets = Array.isArray(state.targets) ? state.targets : [];
    if (!targets.length) {
        return { valid: false, error: `Nothing to convert ${ext} into.` };
    }
    const target = String(state.target || '').toLowerCase();
    if (!target) {
        return { valid: false, error: 'Choose a target format.' };
    }
    if (!targets.includes(target)) {
        return { valid: false, error: `Can't convert ${ext} to ${target}.` };
    }
    return { valid: true };
}

// ============================== DOM wiring =================================

function initConvertApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileName = document.getElementById('fileName');
    const targetSelect = document.getElementById('targetSelect');
    const targetHint = document.getElementById('targetHint');
    const runBtn = document.getElementById('runBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');
    const matrixToggle = document.getElementById('matrixToggle');
    const matrixBody = document.getElementById('matrixBody');
    const matrixList = document.getElementById('matrixList');

    let selectedFile = null;
    let currentTargets = [];

    function showMessage(kind, text) {
        message.className = 'message ' + kind;
        message.textContent = text;
        message.style.display = 'block';
    }

    function updateRunBtn() {
        runBtn.disabled = !(selectedFile && targetSelect.value);
    }

    function resetTargets(placeholder) {
        currentTargets = [];
        targetSelect.innerHTML = '';
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = placeholder;
        targetSelect.appendChild(opt);
        targetSelect.disabled = true;
        updateRunBtn();
    }

    async function loadTargets(ext) {
        resetTargets('Loading…');
        targetHint.textContent = '';
        try {
            const resp = await fetch('/convert/targets?ext=' + encodeURIComponent(ext));
            const data = await resp.json();
            if (!resp.ok) {
                resetTargets('Unsupported format');
                targetHint.textContent = data.error || `Can't convert ${ext} files.`;
                return;
            }
            currentTargets = Array.isArray(data.targets) ? data.targets : [];
            if (!currentTargets.length) {
                resetTargets('No targets available');
                targetHint.textContent =
                    `${buildTargetLabel(ext)} files can't be converted (yet).`;
                return;
            }
            targetSelect.innerHTML = '';
            const head = document.createElement('option');
            head.value = '';
            head.textContent = 'Choose a format…';
            targetSelect.appendChild(head);
            currentTargets.forEach(t => {
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = buildTargetLabel(t);
                targetSelect.appendChild(opt);
            });
            targetSelect.disabled = false;
            targetHint.textContent =
                `${currentTargets.length} format(s) available for ${buildTargetLabel(ext)}.`;
            updateRunBtn();
        } catch (err) {
            resetTargets('Error');
            targetHint.textContent = 'Failed to load conversion options.';
        }
    }

    function acceptFile(file) {
        selectedFile = file;
        fileName.textContent = file.name;
        fileName.style.display = 'block';
        message.style.display = 'none';
        clearDownload();
        const ext = getExt(file.name);
        if (!ext) {
            resetTargets('No file extension');
            targetHint.textContent = 'This file has no extension to convert from.';
            return;
        }
        loadTargets(ext);
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

    targetSelect.addEventListener('change', updateRunBtn);

    // ---- Run ----
    runBtn.addEventListener('click', async () => {
        if (!selectedFile) return;
        const state = {
            ext: selectedFile.name,
            target: targetSelect.value,
            targets: currentTargets,
        };
        const check = validateConvertState(state);
        if (!check.valid) {
            showMessage('error', check.error);
            return;
        }

        const formData = new FormData();
        formData.append('file', selectedFile);
        formData.append('target', targetSelect.value);

        runBtn.disabled = true;
        loading.style.display = 'block';
        message.style.display = 'none';
        clearDownload();

        try {
            const response = await fetch('/convert/run', { method: 'POST', body: formData });
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

    // ---- "What can I convert?" matrix ----
    let matrixLoaded = false;
    async function loadMatrix() {
        try {
            const resp = await fetch('/convert/matrix');
            const data = await resp.json();
            const lines = formatMatrix(data.matrix);
            matrixList.innerHTML = '';
            if (!lines.length) {
                const li = document.createElement('li');
                li.textContent = 'No conversions are available yet.';
                matrixList.appendChild(li);
            } else {
                lines.forEach(line => {
                    const li = document.createElement('li');
                    li.style.padding = '2px 0';
                    li.textContent = line;
                    matrixList.appendChild(li);
                });
            }
        } catch (err) {
            matrixList.innerHTML = '';
            const li = document.createElement('li');
            li.textContent = 'Failed to load the conversion map.';
            matrixList.appendChild(li);
        }
    }

    matrixToggle.addEventListener('click', () => {
        const open = matrixBody.style.display !== 'none';
        matrixBody.style.display = open ? 'none' : 'block';
        if (!open && !matrixLoaded) {
            matrixLoaded = true;
            loadMatrix();
        }
    });

    resetTargets('Select a file first…');
    updateRunBtn();
}

// Wire up once the DOM is ready (guarded for Node, where document is undefined).
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initConvertApp);
    } else {
        initConvertApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getExt,
        buildTargetLabel,
        formatMatrix,
        validateConvertState,
        escapeHtml,
    };
}
