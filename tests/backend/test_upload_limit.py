"""The tiered upload limit, utils/uploads.py (round prelaunch-fixes B2, design §7.2, §8).

Free and anonymous requests get DOCIST_MAX_UPLOAD_MB (50 MiB), paid ones
DOCIST_MAX_UPLOAD_MB_PAID (90 MiB). Bodies are streamed from a generator, so
the 91 MiB cases don't hold 91 MiB in memory. The chunked case calls the WSGI
app directly: Werkzeug's test client always sets a length and never sets
``wsgi.input_terminated`` (verification MINOR 4).
"""
import io
import re

import pytest
from werkzeug.test import EnvironBuilder

import app as flask_app_module
from models import ApiKey, User, db
from utils import uploads

app = flask_app_module.app
MIB = 1024 * 1024
BOUNDARY = 'docistlimitboundary'


class ZeroFileBody(io.RawIOBase):
    """A multipart body with one ``files[]`` part of ``size`` zero bytes.

    Counts what was read. ``fail_on_read`` makes any read an error, to prove
    that nothing read the body.
    """

    def __init__(self, size, field='files[]', filename='big.bin', fail_on_read=False,
                 rewindable=True):
        self.head = (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{field}"; '
                     f'filename="{filename}"\r\nContent-Type: application/octet-stream'
                     '\r\n\r\n').encode()
        self.tail = f'\r\n--{BOUNDARY}--\r\n'.encode()
        self.size = size
        self.length = len(self.head) + size + len(self.tail)
        self.pos = 0
        self.read_bytes = 0
        self.fail_on_read = fail_on_read
        self.rewindable = rewindable

    def readable(self):
        return True

    def seekable(self):
        return self.rewindable

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        # The test client measures its input (end, then back); nothing else seeks.
        if not self.rewindable:
            raise io.UnsupportedOperation('seek')
        self.pos = offset if whence == 0 else self.length + offset
        return self.pos

    def readinto(self, buf):
        if self.fail_on_read:
            raise AssertionError('the body was read')
        n = min(len(buf), self.length - self.pos)
        if n <= 0:
            return 0
        out = bytearray()
        start = self.pos
        for seg_start, seg in ((0, self.head), (len(self.head), None),
                               (len(self.head) + self.size, self.tail)):
            seg_len = self.size if seg is None else len(seg)
            lo = max(start, seg_start)
            hi = min(start + n, seg_start + seg_len)
            if lo < hi:
                out += (bytes(hi - lo) if seg is None
                        else seg[lo - seg_start:hi - seg_start])
        buf[:n] = out
        self.pos += n
        self.read_bytes += n
        return n


CONTENT_TYPE = f'multipart/form-data; boundary={BOUNDARY}'


def post(client, path, size, headers=None, **body_kw):
    body = ZeroFileBody(size, **body_kw)
    response = client.post(path, input_stream=body, content_length=body.length,
                           content_type=CONTENT_TYPE, headers=headers or {})
    return response, body


def assert_413(response, limit_mb):
    assert response.status_code == 413
    assert response.is_json
    assert response.get_json() == {
        'error': f'This upload is over the {limit_mb} MB limit.',
        'code': 'upload_too_large',
        'limit_mb': limit_mb,
    }


@pytest.fixture
def paid(client, make_user, login):
    user = make_user(email='paid@x.io', plan='monthly')
    login(client, user)
    return user


@pytest.fixture
def free(client, make_user, login):
    user = make_user(email='free@x.io', plan='free')
    login(client, user)
    return user


def paid_api_key(make_user):
    user = make_user(email='api-paid@x.io', plan='monthly')
    with app.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), 'test')
        db.session.commit()
    return raw


# --------------------------------------------------------------------------
# Limits by tier
# --------------------------------------------------------------------------
def test_defaults():
    assert app.config['MAX_CONTENT_LENGTH'] == 50 * MIB == 52_428_800
    assert app.config['MAX_CONTENT_LENGTH_PAID'] == 90 * MIB == 94_371_840


def test_anonymous_gets_413_json_at_51_mib(client):
    response, _ = post(client, '/upload', 51 * MIB)
    assert_413(response, 50)


def test_free_gets_413_json_at_51_mib(client, free):
    response, _ = post(client, '/upload', 51 * MIB)
    assert_413(response, 50)


def test_unverified_paid_plan_gets_the_free_limit(client, make_user, login):
    login(client, make_user(email='u@x.io', plan='monthly', verified=False))
    response, _ = post(client, '/upload', 51 * MIB)
    assert_413(response, 50)


def test_paid_passes_51_mib(client, paid):
    response, body = post(client, '/upload', 51 * MIB)
    # The body was read in full and the route answered; big.bin isn't convertible.
    assert response.status_code == 400
    assert response.get_json()['error'] == 'No supported files provided'
    assert body.read_bytes == body.length


def test_paid_gets_413_at_91_mib(client, paid):
    response, _ = post(client, '/upload', 91 * MIB)
    assert_413(response, 90)


def test_paid_api_key_gets_the_paid_limit(client, make_user):
    raw = paid_api_key(make_user)
    auth = {'Authorization': f'Bearer {raw}'}
    response, _ = post(client, '/api/v1/merge', 51 * MIB, headers=auth)
    assert response.status_code == 400   # passed the limit; nothing to merge
    response, _ = post(client, '/api/v1/merge', 91 * MIB, headers=auth)
    assert_413(response, 90)


def test_html_accepting_caller_still_gets_json(client):
    response, _ = post(client, '/upload', 51 * MIB, headers={'Accept': 'text/html'})
    assert_413(response, 50)


def test_messages_follow_config(client, monkeypatch):
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 1 * MIB)
    response, _ = post(client, '/upload', 2 * MIB)
    assert_413(response, 1)


# --------------------------------------------------------------------------
# Before the body: the early refusal, and failing closed
# --------------------------------------------------------------------------
def test_declared_length_over_the_limit_reads_no_body(client):
    body = ZeroFileBody(51 * MIB, fail_on_read=True)
    response = client.post('/upload', input_stream=body, content_length=body.length,
                           content_type=CONTENT_TYPE)
    assert_413(response, 50)
    assert body.read_bytes == 0


def test_limit_is_set_before_csrf_parses_the_form(csrf_on, paid):
    """CSRF is the first step that may parse the form; with the token in
    neither header nor form it parses the 51 MiB body under the paid limit and
    refuses with its own 400, not a 413."""
    response, body = post(csrf_on, '/upload', 51 * MIB)
    assert response.status_code == 400
    assert 'Session expired' in response.get_json()['error']
    assert body.read_bytes == body.length


def test_without_the_tier_step_a_paid_user_gets_the_free_limit(client, paid, monkeypatch):
    calls = []
    monkeypatch.setattr(uploads, 'apply_limit', lambda: calls.append(1))
    response, _ = post(client, '/upload', 51 * MIB)
    assert calls == [1]
    assert_413(response, 50)


def test_request_started_is_stamped_by_the_tier_step(client, monkeypatch):
    from flask import g
    seen = []
    real = uploads.apply_limit

    def spy():
        result = real()
        seen.append(g.get('request_started'))
        return result

    monkeypatch.setattr(uploads, 'apply_limit', spy)
    client.post('/upload')
    assert len(seen) == 1 and isinstance(seen[0], float)


# --------------------------------------------------------------------------
# The page and the server read the same number
# --------------------------------------------------------------------------
def meta_limit(html):
    match = re.search(r'<meta name="docist-upload-limit" content="(\d+)">', html)
    assert match, 'no upload-limit meta'
    return int(match.group(1))


@pytest.mark.parametrize('page', ['/', '/convert', '/pages', '/print', '/export',
                                  '/security', '/api'])
def test_every_tool_page_carries_the_limit_and_script(client, page):
    html = client.get(page).get_data(as_text=True)
    assert meta_limit(html) == app.config['MAX_CONTENT_LENGTH']
    assert '/static/upload-limit.js' in html


def test_page_limit_equals_limit_bytes(client, make_user, login):
    assert meta_limit(client.get('/').get_data(as_text=True)) == 50 * MIB
    free_user = make_user(email='f@x.io')
    login(client, free_user)
    assert meta_limit(client.get('/').get_data(as_text=True)) == 50 * MIB
    paid_user = make_user(email='p@x.io', plan='lifetime')
    login(client, paid_user)
    html = client.get('/').get_data(as_text=True)
    with app.test_request_context('/'):
        expected = uploads.limit_bytes(db.session.get(User, paid_user.id))
    assert meta_limit(html) == expected == 90 * MIB


# --------------------------------------------------------------------------
# A chunked body: the WSGI app called directly
# --------------------------------------------------------------------------
def call_chunked(path, body, headers=None):
    builder = EnvironBuilder(path=path, method='POST', headers=headers or {},
                             content_type=CONTENT_TYPE)
    environ = builder.get_environ()
    environ.pop('CONTENT_LENGTH', None)
    environ['CONTENT_TYPE'] = CONTENT_TYPE   # the builder made up its own boundary
    environ['HTTP_TRANSFER_ENCODING'] = 'chunked'
    environ['wsgi.input'] = body
    environ['wsgi.input_terminated'] = True
    status = {}

    def start_response(code, response_headers, exc_info=None):
        status['code'] = int(code.split()[0])
        status['headers'] = dict(response_headers)

    chunks = app.wsgi_app(environ, start_response)
    data = b''.join(chunks)
    if hasattr(chunks, 'close'):
        chunks.close()
    return status['code'], data


def test_free_chunked_body_is_cut_at_the_free_limit(client):
    body = ZeroFileBody(51 * MIB, rewindable=False)
    assert not body.seekable()
    code, data = call_chunked('/upload', body)
    assert code == 413
    assert b'"code":"upload_too_large"' in data.replace(b' ', b'')
    assert b'"limit_mb":50' in data.replace(b' ', b'')
    # Werkzeug stopped reading at the limit, not at the end of the body.
    assert body.read_bytes <= 50 * MIB + 64 * 1024
    assert body.read_bytes < body.length


def test_paid_chunked_body_passes_51_mib(client, make_user):
    raw = paid_api_key(make_user)
    body = ZeroFileBody(51 * MIB, rewindable=False)
    code, data = call_chunked('/api/v1/merge', body,
                              headers={'Authorization': f'Bearer {raw}'})
    assert code == 400 and b'No supported files' in data
    assert body.read_bytes == body.length


def test_paid_chunked_body_is_cut_at_the_paid_limit(client, make_user):
    raw = paid_api_key(make_user)
    body = ZeroFileBody(91 * MIB, rewindable=False)
    code, data = call_chunked('/api/v1/merge', body,
                              headers={'Authorization': f'Bearer {raw}'})
    assert code == 413 and b'"limit_mb":90' in data.replace(b' ', b'')
    assert body.read_bytes <= 90 * MIB + 64 * 1024


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize('limit,shown', [(50 * MIB, 50), (90 * MIB, 90),
                                         (int(95.5 * MIB), 95.5), (MIB // 2, 0.5)])
def test_limit_mb(limit, shown):
    assert uploads.limit_mb(limit) == shown


def test_a_client_cannot_set_the_environ_key(client):
    """``docist.max_body`` is a WSGI environ key; no header maps to it."""
    response, _ = post(client, '/upload', 51 * MIB,
                       headers={'docist.max_body': str(200 * MIB),
                                'Docist-Max-Body': str(200 * MIB)})
    assert_413(response, 50)
