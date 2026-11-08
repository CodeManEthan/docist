"""The one answer for a failure the visitor can't fix.

No response carries an exception's text or a server path. A route's own
``except Exception`` branch logs the exception and returns
:func:`server_error`; anything no route catches reaches the app-wide 500
handler (app.py), which answers by :func:`wants_json`: JSON for every request
under ``/api/v1/`` and for every request that isn't a page load (the tool
pages' ``fetch`` calls and the packet routes are POSTs), the HTML message page
otherwise. The method and path decide, not ``Accept``: the tool scripts send
none.

This module defines no ``bp``, so app.py's blueprint discovery skips it.
"""
from flask import current_app, jsonify, render_template, request

SERVER_ERROR_MESSAGE = "Docist couldn't finish this. Try again, or try another file."
SERVER_ERROR_TITLE = 'Something went wrong'

# The page shown when the message page itself can't be rendered (say the
# failure was the database, which the nav's account link reads).
_PLAIN_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<title>Docist &mdash; ' + SERVER_ERROR_TITLE + '</title></head><body>'
    '<h1>' + SERVER_ERROR_TITLE + '</h1>'
    '<p>Docist couldn&#39;t finish this. Try again, or try another file.</p>'
    '<p><a href="/">Back to Docist</a></p></body></html>'
)


def server_error_json():
    """The fixed 500 as JSON, the shape the packet routes' refusals use."""
    response = jsonify({'error': SERVER_ERROR_MESSAGE, 'code': 'server_error'})
    response.status_code = 500
    return response


def server_error(what='request failed'):
    """Log the exception being handled and answer the fixed 500 as JSON.

    Call it from inside an ``except`` block: the traceback goes to the log,
    never to the client."""
    current_app.logger.exception('%s: %s %s', what, request.method, request.path)
    return server_error_json()


def wants_json():
    """True when the failing request should get JSON rather than a page."""
    return request.path.startswith('/api/v1/') or request.method not in ('GET', 'HEAD')


def server_error_page():
    """The fixed 500 as the HTML message page."""
    try:
        body = render_template('message.html', title=SERVER_ERROR_TITLE,
                               body=SERVER_ERROR_MESSAGE, link_href='/',
                               link_text='Back to Docist')
    except Exception:   # noqa: BLE001 - the page must not fail the error answer
        current_app.logger.exception('error page failed to render')
        body = _PLAIN_PAGE
    return body, 500, {'Content-Type': 'text/html; charset=utf-8'}
