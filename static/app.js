// ---------------------------------------------------------------------------
// Docist — merge & convert page frontend logic
//
// This file is split into two halves:
//   1. Pure functions (no DOM access) — unit-tested via node:test.
//   2. DOM wiring — runs in the browser on DOMContentLoaded and USES the pure
//      functions rather than duplicating their logic.
// A CommonJS export guard at the bottom exposes the pure functions to Node.
// ---------------------------------------------------------------------------

// ============================ Pure functions ==============================

// Human-readable file size. Bytes below 1 KiB, KiB below 1 MiB, else MiB.
function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Lowercased extension (including the dot), or '' when there is none.
function getExt(name) {
    const i = name.lastIndexOf('.');
    return i === -1 ? '' : name.slice(i).toLowerCase();
}

// Is a filename accepted given the list of accepted extensions?
// Extension comparison is case-insensitive; only the final dot matters.
function isAccepted(filename, acceptedExtensions) {
    return acceptedExtensions.includes(getExt(filename));
}

// "Supported: PDF, MD, ..." hint string. Dedups, strips the leading dot and
// uppercases each label, preserving first-seen order.
function buildFormatHint(extensions) {
    const exts = [...new Set(extensions)];
    const labels = exts.map(e => e.replace(/^\./, '').toUpperCase()).join(', ');
    return 'Supported: ' + labels;
}

// Deduped extension list (used for the file input's accept attribute).
function dedupeExtensions(extensions) {
    return [...new Set(extensions)];
}

// Move an item from index `from` to index `to`, returning a NEW array.
// No-ops (out-of-range target or from === to) return the SAME array reference,
// matching the original early-return semantics.
function moveItem(array, from, to) {
    if (to < 0 || to >= array.length || from === to) return array;
    const next = array.slice();
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    return next;
}

// Remove the item at `index`, returning a NEW array. Out-of-range indices
// leave the array's contents unchanged (matching Array.splice semantics).
function removeItem(array, index) {
    const next = array.slice();
    next.splice(index, 1);
    return next;
}

// Drag-drop target index math. Dropping below a row targets the next slot;
// dropping above targets the row itself. Because the dragged source is first
// removed, indices after it shift left by one, so we compensate.
function computeDropIndex(srcIndex, targetIndex, dropBelow) {
    let target = dropBelow ? targetIndex + 1 : targetIndex;
    if (srcIndex < target) target -= 1;
    return target;
}

// Escape a string for safe interpolation into HTML.
function escapeHtml(str) {
    return str.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Valid page-number positions (mirrors the backend's allowed values).
const NUMBER_POSITIONS = ['bottom-right', 'bottom-center', 'bottom-left'];

// Build the merge-option form fields (all strings, ready for FormData) from the
// panel's UI state. Booleans become "true"/"false"; an unknown position falls
// back to the default; start number is clamped to an integer >= 1. When page
// numbers are off, position/start still serialize (harmless — the backend
// ignores them) so the object shape stays stable.
function buildMergeOptions(formState) {
    const state = formState || {};
    const asBool = (v, dflt) => (v === undefined || v === null ? dflt : !!v);

    const pageNumbers = asBool(state.pageNumbers, true);
    const blankPages = asBool(state.blankPages, true);
    const bookmarks = asBool(state.bookmarks, true);

    let position = state.numberPosition;
    if (!NUMBER_POSITIONS.includes(position)) position = 'bottom-right';

    let start = parseInt(state.startNumber, 10);
    if (!Number.isFinite(start) || start < 1) start = 1;

    return {
        page_numbers: pageNumbers ? 'true' : 'false',
        blank_pages: blankPages ? 'true' : 'false',
        bookmarks: bookmarks ? 'true' : 'false',
        number_position: position,
        start_number: String(start),
    };
}

// Valid merge modes (mirrors the backend's allowed values).
const MERGE_MODES = ['standard', 'interleave'];

// Build the mode-related form fields (kept separate from buildMergeOptions so
// that helper's shape stays stable). An unknown mode falls back to 'standard';
// reverse_second defaults to true (flatbed/ADF back stacks scan in reverse) and
// only matters in interleave mode. Values are FormData-ready strings.
function buildModeOptions(formState) {
    const state = formState || {};
    let mode = state.mode;
    if (!MERGE_MODES.includes(mode)) mode = 'standard';

    const reverseSecond =
        state.reverseSecond === undefined || state.reverseSecond === null
            ? true
            : !!state.reverseSecond;

    return {
        mode: mode,
        reverse_second: reverseSecond ? 'true' : 'false',
    };
}

// ---- First-page thumbnails (POST /preview/thumbs) ----

// The preview endpoint. Only PDFs are rendered server-side.
const THUMB_ENDPOINT = '/preview/thumbs';

// Should this file get a thumbnail fetched for it? Non-PDFs keep the plain
// row appearance — they aren't PDFs yet, so there is nothing to render.
function wantsThumb(filename) {
    return getExt(filename) === '.pdf';
}

// Pull the first-page thumbnail out of a /preview/thumbs payload.
// Returns a data URL, or '' when the payload is missing/unusable (which the
// caller treats exactly like a failed request: fall back to the plain row).
function firstThumb(payload) {
    if (!payload || !Array.isArray(payload.thumbs) || payload.thumbs.length === 0) {
        return '';
    }
    const first = payload.thumbs[0];
    if (typeof first !== 'string' || !first.startsWith('data:image/')) return '';
    return first;
}

// Markup for the thumbnail cell at the left of a merge file row.
//   'loading' -> a shimmering placeholder of the right size
//   'ready'   -> the <img> (needs a data URL)
//   anything else ('error', 'none', undefined) -> '' so the row renders
//                exactly as it did before thumbnails existed.
function thumbCellHtml(state, dataUrl) {
    if (state === 'loading') {
        return '<span class="file-thumb thumb-loading" aria-hidden="true"></span>';
    }
    if (state === 'ready' && dataUrl) {
        return '<span class="file-thumb"><img class="thumb-img" src="'
            + escapeHtml(dataUrl) + '" alt=""></span>';
    }
    return '';
}

// Can the merge button be enabled for this UI state? Standard mode needs at
// least one file; interleave mode needs EXACTLY two (fronts + backs).
function canMerge(state) {
    const s = state || {};
    const count = Number.isFinite(s.fileCount) ? s.fileCount : 0;
    if (s.mode === 'interleave') return count === 2;
    return count >= 1;
}

// ============================== DOM wiring =================================

function initApp() {
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const mergeBtn = document.getElementById('mergeBtn');
    const message = document.getElementById('message');
    const loading = document.getElementById('loading');
    const uploadHint = document.getElementById('uploadHint');

    // ---- Merge options panel controls ----
    const optPageNumbers = document.getElementById('optPageNumbers');
    const optBlankPages = document.getElementById('optBlankPages');
    const optBookmarks = document.getElementById('optBookmarks');
    const optPosition = document.getElementById('optNumberPosition');
    const optStartNumber = document.getElementById('optStartNumber');

    // ---- Merge-mode controls ----
    const optModeInterleave = document.getElementById('optModeInterleave');
    const optReverseSecond = document.getElementById('optReverseSecond');
    const optReverseSecondRow = document.getElementById('optReverseSecondRow');
    const interleaveHint = document.getElementById('interleaveHint');

    function currentMode() {
        return optModeInterleave && optModeInterleave.checked
            ? 'interleave'
            : 'standard';
    }

    // Reverse-order checkbox + hint only apply in interleave mode.
    function syncModeControls() {
        const interleave = currentMode() === 'interleave';
        if (optReverseSecondRow) {
            optReverseSecondRow.style.display = interleave ? '' : 'none';
        }
        if (interleaveHint) {
            interleaveHint.style.display = interleave ? '' : 'none';
        }
        updateMergeBtn();
    }

    // Position + start number only apply when page numbers are enabled.
    function syncNumberControls() {
        const on = optPageNumbers.checked;
        optPosition.disabled = !on;
        optStartNumber.disabled = !on;
    }
    optPageNumbers.addEventListener('change', syncNumberControls);
    syncNumberControls();

    document.querySelectorAll('input[name="mergeMode"]').forEach((el) => {
        el.addEventListener('change', syncModeControls);
    });

    // Read the option panel into the shape buildMergeOptions expects.
    function currentMergeOptions() {
        return buildMergeOptions({
            pageNumbers: optPageNumbers.checked,
            blankPages: optBlankPages.checked,
            bookmarks: optBookmarks.checked,
            numberPosition: optPosition.value,
            startNumber: optStartNumber.value,
        });
    }

    // Read the mode controls into the shape buildModeOptions expects.
    function currentModeOptions() {
        return buildModeOptions({
            mode: currentMode(),
            reverseSecond: optReverseSecond ? optReverseSecond.checked : true,
        });
    }

    let selectedFiles = [];
    let acceptedExtensions = ['.pdf'];
    let dragSrcIndex = null;
    // File object -> { state: 'loading'|'ready'|'error', url: dataUrl }.
    // Keyed by the File itself so reordering/removal can't desync it.
    const thumbCache = new Map();

    // ---- Accepted formats (driven by the /formats endpoint) ----
    async function loadFormats() {
        try {
            const res = await fetch('/formats');
            if (!res.ok) throw new Error('bad status');
            const data = await res.json();
            if (data && Array.isArray(data.extensions) && data.extensions.length) {
                acceptedExtensions = data.extensions
                    .filter(e => typeof e === 'string')
                    .map(e => e.toLowerCase());
            }
        } catch (err) {
            // Fall back to PDF only if the endpoint is unavailable
            acceptedExtensions = ['.pdf'];
        }
        applyAcceptedFormats();
    }

    function applyAcceptedFormats() {
        fileInput.accept = dedupeExtensions(acceptedExtensions).join(',');
        uploadHint.textContent = buildFormatHint(acceptedExtensions);
    }

    function fileAccepted(file) {
        return isAccepted(file.name, acceptedExtensions);
    }

    // ---- Upload via click or drag-drop ----
    uploadArea.addEventListener('click', () => {
        fileInput.click();
    });

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
        // Only OS files carry data; internal row reordering has none.
        const files = Array.from(e.dataTransfer.files).filter(fileAccepted);
        handleFiles(files);
    });

    fileInput.addEventListener('change', (e) => {
        const files = Array.from(e.target.files).filter(fileAccepted);
        handleFiles(files);
    });

    function handleFiles(files) {
        if (files.length === 0) return;
        // Append new files to existing list instead of replacing
        selectedFiles = [...selectedFiles, ...files];
        files.forEach(queueThumb);
        displayFiles();
        updateMergeBtn();
        message.style.display = 'none';
        // Clear the file input so the same files can be selected again if needed
        fileInput.value = '';
    }

    // ---- Thumbnails ----
    // Mark a PDF as pending and fetch its first-page preview in the
    // background. Non-PDFs are skipped entirely (no request is made).
    function queueThumb(file) {
        if (!wantsThumb(file.name) || thumbCache.has(file)) return;
        thumbCache.set(file, { state: 'loading', url: '' });
        fetchThumb(file);
    }

    async function fetchThumb(file) {
        let entry = { state: 'error', url: '' };
        try {
            const formData = new FormData();
            formData.append('file', file);
            const response = await fetch(THUMB_ENDPOINT, {
                method: 'POST',
                body: formData,
            });
            if (response.ok) {
                const url = firstThumb(await response.json());
                if (url) entry = { state: 'ready', url: url };
            }
        } catch (err) {
            // Previews are a nicety: any failure silently degrades the row to
            // its plain appearance. Deliberately no console noise.
        }
        // The file may have been removed while the request was in flight.
        if (!thumbCache.has(file)) return;
        thumbCache.set(file, entry);
        displayFiles();
    }

    function thumbFor(file) {
        const entry = thumbCache.get(file);
        return entry ? thumbCellHtml(entry.state, entry.url) : '';
    }

    function updateMergeBtn() {
        mergeBtn.disabled = !canMerge({
            mode: currentMode(),
            fileCount: selectedFiles.length,
        });
    }

    // ---- Reorder / remove ----
    function moveFile(from, to) {
        const next = moveItem(selectedFiles, from, to);
        if (next === selectedFiles) return; // no-op, keep the current render
        selectedFiles = next;
        displayFiles();
    }

    function removeFile(index) {
        const dropped = selectedFiles[index];
        selectedFiles = removeItem(selectedFiles, index);
        // Drop the cached preview unless the same File is still in the list
        // (the user can add the very same file twice).
        if (dropped && !selectedFiles.includes(dropped)) thumbCache.delete(dropped);
        displayFiles();
        updateMergeBtn();
    }

    function displayFiles() {
        fileList.innerHTML = '';
        selectedFiles.forEach((file, index) => {
            const div = document.createElement('div');
            div.className = 'file-item';
            div.draggable = true;
            div.dataset.index = index;
            div.innerHTML = `
                <span class="drag-handle" title="Drag to reorder">⠿</span>
                ${thumbFor(file)}
                <span class="file-info"><span class="file-number">${index + 1}.</span>${escapeHtml(file.name)}</span>
                <span class="file-size">${formatFileSize(file.size)}</span>
                <span class="file-actions">
                    <button class="icon-btn move-up" title="Move up" ${index === 0 ? 'disabled' : ''}>▲</button>
                    <button class="icon-btn move-down" title="Move down" ${index === selectedFiles.length - 1 ? 'disabled' : ''}>▼</button>
                    <button class="icon-btn remove-btn" title="Remove">✕</button>
                </span>
            `;

            div.querySelector('.move-up').addEventListener('click', () => moveFile(index, index - 1));
            div.querySelector('.move-down').addEventListener('click', () => moveFile(index, index + 1));
            div.querySelector('.remove-btn').addEventListener('click', () => removeFile(index));

            // HTML5 drag-and-drop reordering
            div.addEventListener('dragstart', (e) => {
                dragSrcIndex = index;
                div.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                // Set data so Firefox initiates the drag; value unused.
                e.dataTransfer.setData('text/plain', String(index));
            });

            div.addEventListener('dragover', (e) => {
                if (dragSrcIndex === null) return; // OS-file drag, let upload area handle it
                e.preventDefault();
                e.stopPropagation();
                e.dataTransfer.dropEffect = 'move';
                clearDropIndicators();
                // Show indicator above or below depending on pointer position
                const rect = div.getBoundingClientRect();
                const after = (e.clientY - rect.top) > rect.height / 2;
                div.classList.add(after ? 'drag-over-bottom' : 'drag-over-top');
            });

            div.addEventListener('dragleave', () => {
                div.classList.remove('drag-over-top', 'drag-over-bottom');
            });

            div.addEventListener('drop', (e) => {
                if (dragSrcIndex === null) return;
                e.preventDefault();
                e.stopPropagation();
                const rect = div.getBoundingClientRect();
                const after = (e.clientY - rect.top) > rect.height / 2;
                const target = computeDropIndex(dragSrcIndex, index, after);
                moveFile(dragSrcIndex, target);
            });

            div.addEventListener('dragend', () => {
                dragSrcIndex = null;
                clearDropIndicators();
                document.querySelectorAll('.file-item.dragging')
                    .forEach(el => el.classList.remove('dragging'));
            });

            fileList.appendChild(div);
        });
    }

    function clearDropIndicators() {
        document.querySelectorAll('.file-item')
            .forEach(el => el.classList.remove('drag-over-top', 'drag-over-bottom'));
    }

    // ---- Merge ----
    mergeBtn.addEventListener('click', async () => {
        if (!canMerge({ mode: currentMode(), fileCount: selectedFiles.length })) {
            return;
        }

        const formData = new FormData();
        selectedFiles.forEach(file => {
            formData.append('files[]', file);
        });
        // Attach merge + mode options from the panel.
        const options = Object.assign(
            {}, currentMergeOptions(), currentModeOptions()
        );
        Object.keys(options).forEach(key => {
            formData.append(key, options[key]);
        });

        mergeBtn.disabled = true;
        loading.style.display = 'block';
        message.style.display = 'none';

        try {
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();

            loading.style.display = 'none';

            if (response.ok) {
                message.className = 'message success';
                message.textContent = result.message;
                message.style.display = 'block';

                const outputFilename = result.filename;

                const downloadBtn = document.createElement('button');
                downloadBtn.className = 'btn download-btn';
                downloadBtn.textContent = '⬇️ Download Merged PDF';
                downloadBtn.onclick = () => {
                    window.location.href = '/download?filename=' + encodeURIComponent(outputFilename);
                    setTimeout(() => {
                        downloadBtn.textContent = '✓ Downloaded! Click to download again';
                    }, 1000);
                };

                const btnContainer = mergeBtn.parentNode;
                btnContainer.insertBefore(downloadBtn, message);

                mergeBtn.style.display = 'none';

                const resetBtn = document.createElement('button');
                resetBtn.className = 'btn';
                resetBtn.textContent = '🔄 Start Over';
                resetBtn.style.marginTop = '10px';
                resetBtn.onclick = () => {
                    downloadBtn.remove();
                    resetBtn.remove();
                    mergeBtn.style.display = 'block';
                    message.style.display = 'none';
                    selectedFiles = [];
                    thumbCache.clear();
                    displayFiles();
                    updateMergeBtn();
                    fileInput.value = '';
                };
                btnContainer.insertBefore(resetBtn, message);
            } else {
                message.className = 'message error';
                message.textContent = result.error || 'An error occurred';
                message.style.display = 'block';
                updateMergeBtn();
            }
        } catch (error) {
            loading.style.display = 'none';
            message.className = 'message error';
            message.textContent = 'Failed to connect to server';
            message.style.display = 'block';
            updateMergeBtn();
        }
    });

    // Reflect the initial mode (standard) in the panel + merge button.
    syncModeControls();

    // Kick off formats fetch on load
    loadFormats();
}

// Wire up once the DOM is ready (guarded so requiring this file in Node,
// where `document` is undefined, does not throw).
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initApp);
    } else {
        initApp();
    }
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        formatFileSize,
        getExt,
        isAccepted,
        buildFormatHint,
        dedupeExtensions,
        moveItem,
        removeItem,
        computeDropIndex,
        escapeHtml,
        buildMergeOptions,
        NUMBER_POSITIONS,
        buildModeOptions,
        canMerge,
        MERGE_MODES,
        wantsThumb,
        firstThumb,
        thumbCellHtml,
        THUMB_ENDPOINT,
    };
}
