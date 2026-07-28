"""Docist — Flask bootstrap.

Configuration comes from environment variables (all optional; defaults suit
local development):

    DOCIST_PASSWORD                enable the login gate (off when unset)
    DOCIST_SECRET_KEY              session-signing key; auto-generated per
                                   process when unset (logins then reset on
                                   restart — set it in production)
    DOCIST_HOST / DOCIST_PORT      dev-server bind (default 127.0.0.1:5010)
    DOCIST_MAX_UPLOAD_MB           request size cap (default 50)
    DOCIST_RATE_LIMIT              POSTs allowed per window per IP (default 30)
    DOCIST_RATE_WINDOW             rate-limit window in seconds (default 60)
    DOCIST_OUTPUT_MAX_AGE_MINUTES  results older than this are pruned (default
                                   1440 = 24h; pruning runs opportunistically)
    DOCIST_COOKIE_SECURE           set to 1 when serving over HTTPS
    FLASK_DEBUG                    set to 1 for the dev server's debugger
"""
import importlib
import os
import pkgutil
import secrets

from flask import Flask, jsonify, request

import routes
from utils.cleanup import OutputJanitor
from utils.ratelimit import RateLimiter


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'output'
app.config['MAX_CONTENT_LENGTH'] = _env_int('DOCIST_MAX_UPLOAD_MB', 50) * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('DOCIST_SECRET_KEY') or secrets.token_hex(32)
app.config['ACCESS_PASSWORD'] = os.environ.get('DOCIST_PASSWORD', '')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = _env_flag('DOCIST_COOKIE_SECURE')
app.config['RATE_LIMIT_REQUESTS'] = _env_int('DOCIST_RATE_LIMIT', 30)
app.config['RATE_LIMIT_WINDOW'] = _env_int('DOCIST_RATE_WINDOW', 60)
app.config['RATE_LIMIT_ENABLED'] = True
app.config['OUTPUT_MAX_AGE'] = _env_int('DOCIST_OUTPUT_MAX_AGE_MINUTES', 24 * 60) * 60

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Heavy work happens on POST, so that's what gets rate-limited (per client IP).
# Swappable via app.limiter so tests can install a fresh, tiny-window instance.
app.limiter = RateLimiter(
    app.config['RATE_LIMIT_REQUESTS'], app.config['RATE_LIMIT_WINDOW']
)
_janitor = OutputJanitor(max_age=app.config['OUTPUT_MAX_AGE'], interval=300)


@app.before_request
def _rate_limit_and_prune():
    # Prune stale results opportunistically (throttled inside the janitor).
    # Skipped under test so suites never touch the real output folder.
    if not app.testing:
        _janitor.maybe_prune(app.config['OUTPUT_FOLDER'])

    if request.method == 'POST' and app.config['RATE_LIMIT_ENABLED']:
        if not app.testing or app.config.get('RATE_LIMIT_FORCE'):
            key = request.remote_addr or 'unknown'
            if not app.limiter.allow(key):
                retry = max(1, round(app.limiter.retry_after(key)))
                response = jsonify({
                    'error': 'Too many requests — please wait a moment '
                             'and try again.'
                })
                response.status_code = 429
                response.headers['Retry-After'] = str(retry)
                return response
    return None


@app.route('/healthz')
def healthz():
    """Unauthenticated liveness probe for reverse proxies / uptime checks."""
    return jsonify({'status': 'ok'})


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
