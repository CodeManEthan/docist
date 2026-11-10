// ---------------------------------------------------------------------------
// Nav usage counter for fetch() — loaded in <head> on every page, last
//
// The nav's "N of L left today" (templates/_account_nav.html, .nav-usage) is
// rendered when the page loads. Every metered request answers with
// X-Docist-Usage-Remaining and X-Docist-Usage-Limit (utils/metering.py
// record()), and the tool pages run by fetch() without reloading, so this file
// wraps window.fetch once and, after each same-origin response that carries
// both headers as whole numbers, rewrites the span from them. "unlimited", a
// missing header or a page with no span leaves the nav as it is. Only the
// headers are read: the response comes back untouched, its body unread, and a
// rejected fetch rejects as before. The tool scripts are unchanged.
//
// Pure helpers first (unit-tested via node:test), then the browser wiring,
// then the CommonJS export guard for Node, as in the other static scripts.
// ---------------------------------------------------------------------------

const REMAINING_HEADER = 'X-Docist-Usage-Remaining';
const LIMIT_HEADER = 'X-Docist-Usage-Limit';

// ============================ Pure functions ==============================

// A header value as a whole number, or null when it is missing, "unlimited"
// or anything else.
function parseCount(value) {
    const text = String(value === null || value === undefined ? '' : value).trim();
    if (!/^[0-9]{1,15}$/.test(text)) return null;
    return Number(text);
}

// The usage a response reports, as {remaining, limit}, or null unless both
// headers are whole numbers.
function usageFromHeaders(headers) {
    if (!headers || typeof headers.get !== 'function') return null;
    const remaining = parseCount(headers.get(REMAINING_HEADER));
    const limit = parseCount(headers.get(LIMIT_HEADER));
    if (remaining === null || limit === null) return null;
    return { remaining, limit };
}

// The nav's text, as the template prints it.
function usageText(usage) {
    return usage.remaining + ' of ' + usage.limit + ' left today';
}

function isSameOrigin(url, pageOrigin) {
    try {
        return new URL(String(url), pageOrigin).origin === pageOrigin;
    } catch (e) {
        return false;
    }
}

// Rewrite `span` from `response` when it reports usage; otherwise do nothing.
function applyUsage(span, response) {
    if (!span || !response) return;
    const usage = usageFromHeaders(response.headers);
    if (usage) span.textContent = usageText(usage);
}

// Wrap a fetch function. `getSpan` and `pageOrigin` are read per response, so
// a span rendered after this script loads is still found.
function wrapFetch(origFetch, getSpan, pageOrigin) {
    const wrapped = function (input, init) {
        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        const url = isRequest ? input.url : input;
        return origFetch.call(this, input, init).then((response) => {
            try {
                if (isSameOrigin(url, pageOrigin())) applyUsage(getSpan(), response);
            } catch (e) {
                // The counter is a courtesy; never fail the caller's fetch over it.
            }
            return response;
        });
    };
    wrapped.__docistUsage = true;
    return wrapped;
}

// ============================ Browser wiring ==============================

function installUsageFetch(win) {
    const origFetch = win.fetch;
    if (!origFetch || origFetch.__docistUsage) return;
    win.fetch = wrapFetch(
        origFetch,
        () => win.document.querySelector('.nav-usage'),
        () => win.location.origin,
    );
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    installUsageFetch(window);
}

// ======================= CommonJS export guard ============================
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        REMAINING_HEADER,
        LIMIT_HEADER,
        parseCount,
        usageFromHeaders,
        usageText,
        applyUsage,
        wrapFetch,
        installUsageFetch,
    };
}
