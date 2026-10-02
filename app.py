"""Docist — Flask bootstrap.

Configuration comes from environment variables (all optional; defaults suit
local development):

    DATABASE_URL                   SQLAlchemy URL (default: SQLite file
                                   instance/docist.db). Railway's postgres://
                                   URLs are accepted and normalized
    DOCIST_SECRET_KEY              signs sessions, anonymous IP hashes and
                                   result owner tags. REQUIRED in production;
                                   when unset it is persisted in
                                   instance/secret_key (random per process for
                                   in-memory SQLite)
    DOCIST_TRUSTED_PROXIES         reverse-proxy hops to trust for
                                   X-Forwarded-For/-Proto (default 1 when
                                   RAILWAY_ENVIRONMENT is set, else 0)
    DOCIST_LIMIT_ANON              daily operations for anonymous visitors and
                                   unverified accounts (default 10)
    DOCIST_LIMIT_FREE              daily operations for verified free accounts
                                   (default 50)
    DOCIST_LIMIT_PAID              daily operations for any other plan
                                   (default 0 = unlimited)
    DOCIST_API_ANONYMOUS           set to 1 to let /api/v1 POSTs run without an
                                   API key (metered as anonymous)
    DOCIST_HOST / DOCIST_PORT      dev-server bind (default 127.0.0.1:5010)
    DOCIST_MAX_UPLOAD_MB           request body cap in MiB for every tier but
                                   paid (default 50)
    DOCIST_MAX_UPLOAD_MB_PAID      request body cap in MiB for paid accounts
                                   on Merge and Convert, for PDFs and Word
                                   files (default 90; keep it at least 5 MB
                                   under a proxy's own cap; utils/uploads.py)
    DOCIST_RENDER_BUDGET           seconds from the start of a request by which
                                   Word-engine work must end (default 90)
    DOCIST_OFFICE_TIMEOUT          seconds one LibreOffice call may run
                                   (default 60)
    DOCIST_OFFICE_MEM_MB           address-space cap for LibreOffice, in MiB
                                   (default 1536)
    DOCIST_RATE_LIMIT              POSTs allowed per window per IP (default 30)
    DOCIST_RATE_WINDOW             rate-limit window in seconds (default 60)
    DOCIST_OUTPUT_MAX_AGE_MINUTES  results older than this are pruned (default
                                   1440 = 24h; pruning runs opportunistically)
    DOCIST_COOKIE_SECURE           set to 1 when serving over HTTPS
    FLASK_DEBUG                    set to 1 for the dev server's debugger

Email settings (DOCIST_EMAIL_*, DOCIST_SMTP_*, DOCIST_RESEND_API_KEY,
DOCIST_BASE_URL) are read by utils/mailer.py. DOCIST_PASSWORD and
DOCIST_PUBLIC_DEMO are gone: accounts replaced the shared-password gate, and
setting either only logs a warning.

Every gunicorn launcher passes --preload, so the secret-key resolution and
schema creation below run once in the master before the workers fork.

Operator commands:

    flask --app app set-plan EMAIL PLAN    set a user's plan ('free' or paid)
    flask --app app verify-user EMAIL      mark a user's email verified
"""
import importlib
import os
import pkgutil
import secrets
import time
from datetime import timedelta

import click
from flask import Flask, jsonify, request
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool
from werkzeug.middleware.proxy_fix import ProxyFix

import routes
from models import User, database_url, db, utcnow
from pdf_ops import office
from utils import csrf, identity, metering, uploads
from utils.cleanup import OutputJanitor
from utils.ratelimit import RateLimiter


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _is_memory_sqlite(url):
    parsed = make_url(url)
    return (parsed.get_backend_name() == 'sqlite'
            and parsed.database in (None, '', ':memory:'))


def _persisted_secret_key(path):
    """Read the key at ``path``, creating it first if absent.

    O_EXCL makes concurrent first starts safe: exactly one process creates the
    file and the others read the winner's key (waiting briefly for its write).
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):
            with open(path) as fh:
                key = fh.read().strip()
            if key:
                return key
            time.sleep(0.1)
        raise RuntimeError(f'{path} exists but is empty; delete it and restart')
    key = secrets.token_hex(32)
    with os.fdopen(fd, 'w') as fh:
        fh.write(key)
    return key


app = Flask(__name__)

# Startup directories first: every path written below exists before its first
# write, on every database backend (Flask-SQLAlchemy creates instance/ only
# for a SQLite file, so it is not relied on).
os.makedirs(app.instance_path, exist_ok=True)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'output'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url(os.environ)
if os.environ.get('DOCIST_SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ['DOCIST_SECRET_KEY']
elif _is_memory_sqlite(app.config['SQLALCHEMY_DATABASE_URI']):
    app.config['SECRET_KEY'] = secrets.token_hex(32)  # tests: throwaway per process
else:
    app.config['SECRET_KEY'] = _persisted_secret_key(
        os.path.join(app.instance_path, 'secret_key')
    )

app.config['MAX_CONTENT_LENGTH'] = _env_int('DOCIST_MAX_UPLOAD_MB', 50) * 1024 * 1024
app.config['MAX_CONTENT_LENGTH_PAID'] = _env_int('DOCIST_MAX_UPLOAD_MB_PAID', 90) * 1024 * 1024
app.config['RENDER_BUDGET'] = _env_int('DOCIST_RENDER_BUDGET', 90)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = _env_flag('DOCIST_COOKIE_SECURE')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['RATE_LIMIT_REQUESTS'] = _env_int('DOCIST_RATE_LIMIT', 30)
app.config['RATE_LIMIT_WINDOW'] = _env_int('DOCIST_RATE_WINDOW', 60)
app.config['RATE_LIMIT_ENABLED'] = True
app.config['OUTPUT_MAX_AGE'] = _env_int('DOCIST_OUTPUT_MAX_AGE_MINUTES', 24 * 60) * 60
app.config['CSRF_ENABLED'] = True
app.config['METERING_ENABLED'] = True
app.config['LIMIT_ANON'] = _env_int('DOCIST_LIMIT_ANON', 10)
app.config['LIMIT_FREE'] = _env_int('DOCIST_LIMIT_FREE', 50)
app.config['LIMIT_PAID'] = _env_int('DOCIST_LIMIT_PAID', 0)
app.config['API_ANONYMOUS'] = _env_flag('DOCIST_API_ANONYMOUS')

# Schema: create missing tables (existing ones are never altered). Then drop
# the master's connections so forked workers never share one -- except for
# in-memory SQLite, whose StaticPool holds the only connection: disposing it
# would delete the database.
db.init_app(app)
with app.app_context():
    db.create_all()
    if not isinstance(db.engine.pool, StaticPool):
        db.engine.dispose()

# Proxy trust: the client address is the N-th X-Forwarded-For entry from the
# right, so anything a client prepends is ignored. 0 = the socket peer.
_trusted_proxies = _env_int(
    'DOCIST_TRUSTED_PROXIES', 1 if os.environ.get('RAILWAY_ENVIRONMENT') else 0
)
if _trusted_proxies > 0:
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=_trusted_proxies, x_proto=_trusted_proxies
    )

identity.init_app(app)
uploads.init_app(app)
csrf.init_app(app)
metering.init_app(app)
# The Merge and Convert pages' Word hint (templates/_word_hint.html).
app.jinja_env.globals['word_engine_available'] = lambda: office.available()

for _removed in ('DOCIST_PASSWORD', 'DOCIST_PUBLIC_DEMO'):
    if os.environ.get(_removed):
        app.logger.warning(
            '%s is set but ignored: accounts replaced the shared-password '
            'gate. Unset it.', _removed
        )
if (os.environ.get('DATABASE_URL')
        and not _is_memory_sqlite(app.config['SQLALCHEMY_DATABASE_URI'])):
    if not os.environ.get('DOCIST_SECRET_KEY'):
        app.logger.warning(
            '!!! DOCIST_SECRET_KEY is not set. The key is kept in %s, which a '
            'redeploy on an ephemeral disk loses: everyone gets signed out '
            'and anonymous usage starts over. Set DOCIST_SECRET_KEY in '
            'production.', os.path.join(app.instance_path, 'secret_key'),
        )
    if _trusted_proxies == 0:
        app.logger.warning(
            'DOCIST_TRUSTED_PROXIES is 0. Behind a reverse proxy every '
            "anonymous visitor then shares the proxy's IP and one daily "
            'allowance. Set it to the number of proxy hops (Railway: 1).'
        )

# Heavy work happens on POST, so that's what gets rate-limited (per client IP).
# Swappable via app.limiter so tests can install a fresh, tiny-window instance.
app.limiter = RateLimiter(
    app.config['RATE_LIMIT_REQUESTS'], app.config['RATE_LIMIT_WINDOW']
)
_janitor = OutputJanitor(max_age=app.config['OUTPUT_MAX_AGE'], interval=300)


def _rate_limit():
    key = identity.client_ip()
    if app.limiter.allow(key):
        return None
    retry = max(1, round(app.limiter.retry_after(key)))
    response = jsonify({
        'error': 'Too many requests — please wait a moment '
                 'and try again.'
    })
    response.status_code = 429
    response.headers['Retry-After'] = str(retry)
    return response


@app.before_request
def _before_request():
    """The one before-request chain, in order. The cheapest refusal runs first,
    and nothing that costs a DB lookup or a body parse runs before the per-IP
    limiter."""
    # 1. Prune stale results opportunistically (throttled inside the janitor).
    #    Skipped under test so suites never touch the real output folder.
    if not app.testing:
        _janitor.maybe_prune(app.config['OUTPUT_FOLDER'])

    # 2. Per-IP POST rate limit, so 401s and CSRF 400s are limited too.
    if request.method == 'POST' and app.config['RATE_LIMIT_ENABLED']:
        if not app.testing or app.config.get('RATE_LIMIT_FORCE'):
            response = _rate_limit()
            if response is not None:
                return response

    # 3. g.user from the session, or from the API key on /api/v1 (may 401).
    # 4. The body limit for g.user's tier, before anything reads the body
    #    (may 413 on a declared length); stamps g.request_started.
    # 5. CSRF on state-changing browser requests (may 400; may parse the form).
    # 6. The daily limit on metered operations (may 429).
    for step in (identity.load_identity, uploads.apply_limit, csrf.check_csrf,
                 metering.check):
        response = step()
        if response is not None:
            return response
    return None


@app.after_request
def _after_request(response):
    # Counts a successful metered operation and commits it.
    return metering.record(response)


@app.route('/healthz')
def healthz():
    """Unauthenticated liveness probe for reverse proxies / uptime checks."""
    return jsonify({'status': 'ok'})


def _user_by_email(email):
    user = db.session.execute(
        select(User).where(User.email == User.normalize_email(email))
    ).scalar_one_or_none()
    if user is None:
        raise click.ClickException(f'No user with email {email!r}.')
    return user


@app.cli.command('set-plan')
@click.argument('email')
@click.argument('plan')
def set_plan(email, plan):
    """Set EMAIL's plan to PLAN ('free', or e.g. 'monthly' / 'lifetime')."""
    user = _user_by_email(email)
    user.plan = plan.strip()[:20]
    db.session.commit()
    click.echo(f'{user.email}: plan = {user.plan}')


@app.cli.command('verify-user')
@click.argument('email')
def verify_user(email):
    """Mark EMAIL as verified (for instances without an email sender)."""
    user = _user_by_email(email)
    if user.verified_at is None:
        user.verified_at = utcnow()
        db.session.commit()
    click.echo(f'{user.email}: verified')


# Auto-register every blueprint in the routes package: any module there
# that defines a module-level `bp` is picked up — drop in a new module
# and restart the app.
for _mod_info in pkgutil.iter_modules(routes.__path__):
    _module = importlib.import_module(f'routes.{_mod_info.name}')
    _bp = getattr(_module, 'bp', None)
    if _bp is not None:
        app.register_blueprint(_bp)

if __name__ == '__main__':
    app.run(
        debug=_env_flag('FLASK_DEBUG'),
        host=os.environ.get('DOCIST_HOST', '127.0.0.1'),
        port=_env_int('DOCIST_PORT', 5010),
    )
