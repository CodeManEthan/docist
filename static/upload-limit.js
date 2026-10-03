// ---------------------------------------------------------------------------
// Upload limit check for fetch() — loaded in <head> on every page
//
// The server refuses a request body over the caller's upload limit with a
// JSON 413 (utils/uploads.py). Behind Cloudflare a body over 100 MB never
// reaches Docist and the user gets Cloudflare's own HTML page instead, so this
// file checks first: it wraps window.fetch once, and for a same-origin POST
// whose body is a FormData it compares an upper bound on the body's size with
// <meta name="docist-upload-limit">, the number the server uses. Over the
// limit it returns the same 413 JSON the server would and sends nothing. The
// tool scripts are unchanged. The server check still runs for everything else.
//
// Pure helpers first (unit-tested via node:test), then the browser wiring,
// then the CommonJS export guard for Node, as in the other static scripts.
// ---------------------------------------------------------------------------

// Boundary line, part headers and CRLFs for one multipart part come to about
// 150 to 250 bytes; 1 KiB per part keeps the bound above the real body.
const PART_ALLOWANCE = 1024;
const MIB = 1024 * 1024;

// ============================ Pure functions ==============================

// UTF-8 bytes of a text value as the multipart encoder sends it: it turns
// every lone CR or LF into CRLF, so each of those counts as two bytes.
function utf8Length(text) {
    const s = String(text);
    const breaks = (s.match(/[\r\n]/g) || []).length;
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(s).length + breaks;
    return unescape(encodeURIComponent(s)).length + breaks;
}

function isBlob(value) {
    return value !== null && typeof value === 'object'
        && typeof value.size === 'number' && typeof value.arrayBuffer === 'function';
}

// An upper bound on the multipart body fetch() sends for `formData`: every
// file's size, the UTF-8 length of every text value, field name and file name
// (line breaks counted twice), plus PART_ALLOWANCE per part.
function formDataBytes(formData) {
    let total = 0;
    for (const [name, value] of formData.entries()) {
        total += PART_ALLOWANCE + utf8Length(name);
        if (isBlob(value)) {
            total += value.size;
            if (typeof value.name === 'string') total += utf8Length(value.name);
        } else {
            total += utf8Length(value);
        }
    }
    return total;
}

// The MB number users see, as the server prints it: 52428800 -> 50.
function limitMb(limit) {
    const value = limit / MIB;
    return Number.isInteger(value) ? value : Math.round(value * 10) / 10;
}

// The JSON body of the server's 413 (utils/uploads.py too_large).
function tooLargeBody(limit) {
    const mb = limitMb(limit);
    return {
        error: 'This upload is over the ' + mb + ' MB limit.',
        code: 'upload_too_large',
        limit_mb: mb,
    };
}

// The limit from the meta tag's content, or 0 (no check) when it isn't a
// positive whole number.
function parseLimit(content) {
    const text = String(content || '').trim();
    if (!/^[0-9]{1,15}$/.test(text)) return 0;
    const n = Number(text);
    return n > 0 ? n : 0;
}

function isSameOrigin(url, pageOrigin) {
    try {
        return new URL(String(url), pageOrigin).origin === pageOrigin;
    } catch (e) {
        return false;
    }
}

// Is this fetch() an upload over `limit`? Only same-origin POSTs with a
// FormData body are checked; anything else is the server's to judge.
function overLimit(method, url, body, pageOrigin, limit) {
    if (!limit) return false;
    if (String(method || 'GET').toUpperCase() !== 'POST') return false;
    if (typeof FormData === 'undefined' || !(body instanceof FormData)) return false;
    if (!isSameOrigin(url, pageOrigin)) return false;
    return formDataBytes(body) > limit;
}

// Wrap a fetch function. `getLimit` and `pageOrigin` are read per call.
function wrapFetch(origFetch, getLimit, pageOrigin) {
    const wrapped = function (input, init) {
        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        const opts = init || {};
        const method = opts.method || (isRequest ? input.method : 'GET');
        const url = isRequest ? input.url : input;
        const limit = getLimit();
        if (overLimit(method, url, opts.body, pageOrigin(), limit)) {
            return Promise.resolve(new Response(JSON.stringify(tooLargeBody(limit)), {
                status: 413,
                headers: { 'Content-Type': 'application/json' },
            }));
        }
        return origFetch.call(this, input, init);
    };
    wrapped.__docistUploadLimit = true;
    return wrapped;
}

// ============================ Browser wiring ==============================

function pageUploadLimit() {
    const meta = document.querySelector('meta[name="docist-upload-limit"]');
    return parseLimit(meta ? meta.getAttribute('content') : '');
}

function installUploadLimitFetch() {
    const origFetch = window.fetch;
    if (!origFetch || origFetch.__docistUploadLimit) return;
    window.fetch = wrapFetch(origFetch, pageUploadLimit, () => window.location.origin);
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    installUploadLimitFetch();
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        PART_ALLOWANCE,
        formDataBytes,
        limitMb,
        tooLargeBody,
        parseLimit,
        overLimit,
        wrapFetch,
    };
}
