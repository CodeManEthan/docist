// ---------------------------------------------------------------------------
// CSRF header for fetch() — loaded in <head> on every page
//
// The server checks a per-session token on every POST/PUT/PATCH/DELETE outside
// /api/v1. Server-rendered forms carry it as a hidden field; the tool scripts
// post with fetch(), so this file wraps window.fetch once and adds the token
// from <meta name="csrf-token"> as an X-CSRF-Token header on every same-origin
// request that is not a GET or HEAD. The tool scripts themselves are unchanged.
//
// Pure helper first (unit-tested via node:test), then the browser wiring, then
// the CommonJS export guard for Node, as in the other static scripts.
// ---------------------------------------------------------------------------

const CSRF_HEADER = 'X-CSRF-Token';

// ============================ Pure functions ==============================

// Does a request with this method to this url need the CSRF header?
// Only unsafe methods to the page's own origin; a url that cannot be parsed is
// treated as foreign, so the token never leaks off-site.
function needsCsrf(method, url, pageOrigin) {
    const m = String(method || 'GET').toUpperCase();
    if (m === 'GET' || m === 'HEAD') return false;
    try {
        return new URL(String(url), pageOrigin).origin === pageOrigin;
    } catch (e) {
        return false;
    }
}

// The headers to send: `headers` plus X-CSRF-Token when the request needs it
// and a token is known. Returns `headers` untouched otherwise. Accepts what
// fetch() accepts (undefined, a plain object, an array of pairs, or a Headers)
// and never mutates the caller's value.
function withCsrf(method, url, pageOrigin, token, headers) {
    if (!token || !needsCsrf(method, url, pageOrigin)) return headers;
    if (typeof Headers !== 'undefined' && headers instanceof Headers) {
        const copy = new Headers(headers);
        copy.set(CSRF_HEADER, token);
        return copy;
    }
    if (Array.isArray(headers)) {
        return headers.concat([[CSRF_HEADER, token]]);
    }
    return Object.assign({}, headers || {}, { [CSRF_HEADER]: token });
}

// ============================ Browser wiring ==============================

function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

function installCsrfFetch() {
    const origFetch = window.fetch;
    if (!origFetch || origFetch.__docistCsrf) return;
    const wrapped = function (input, init) {
        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        const opts = init || {};
        const method = opts.method || (isRequest ? input.method : 'GET');
        const url = isRequest ? input.url : input;
        // A Request's own headers are replaced by init.headers, so start from them.
        const base = opts.headers !== undefined ? opts.headers
            : (isRequest ? new Headers(input.headers) : undefined);
        const headers = withCsrf(method, url, window.location.origin, csrfToken(), base);
        if (headers === base) return origFetch.call(this, input, init);
        return origFetch.call(this, input, Object.assign({}, opts, { headers }));
    };
    wrapped.__docistCsrf = true;
    window.fetch = wrapped;
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    installCsrfFetch();
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        CSRF_HEADER,
        needsCsrf,
        withCsrf,
    };
}
