"""Daily operation limits for anonymous visitors, accounts and API keys.

The unit is one successful metered operation: a POST to one of the tool
blueprints or to /api/v1 whose response status is below 400. Failed requests
and requests the limit refuses cost nothing. Counters live in the ``usage``
table, one row per subject per UTC day, so limits hold across workers.

Who is counted where (``resolve``):

    anonymous                  ip:<hash>  LIMIT_ANON  (default 10)
    signed in, unverified      ip:<hash>  LIMIT_ANON  (same subject as anon)
    verified, plan 'free'      u:<id>     LIMIT_FREE  (default 50)
    verified, any other plan   u:<id>     LIMIT_PAID  (default 0 = unlimited)

``check()`` runs last in app.py's before-request chain and refuses with a 429
at the limit; ``record()`` runs after the request and counts it. Concurrent
requests right at the limit can overshoot by the number of workers.
"""
import logging
from datetime import datetime, time, timedelta, timezone

from flask import current_app, g, has_request_context, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from models import Usage, db
from utils.identity import anon_subject, current_user

log = logging.getLogger(__name__)

METERED_BLUEPRINTS = {'merge', 'convert', 'pages', 'print', 'export', 'security', 'api'}


def today():
    """The metering day: the current UTC date."""
    return datetime.now(timezone.utc).date()


def tier(user):
    """``'anon'``, ``'unverified'``, ``'free'`` or ``'paid'`` for ``user``.

    The one function that decides "paid" for every feature: metering, the
    Word engine (utils/render_opts.py) and the upload limit (utils/uploads.py)
    all read it. A paid feature never checks ``user.plan`` itself.
    """
    if user is None:
        return 'anon'
    if not user.is_verified:
        return 'unverified'
    return 'paid' if user.is_paid else 'free'


def resolve(user):
    """``(subject, limit)`` for ``user`` (None = anonymous); limit 0 = unlimited."""
    config = current_app.config
    level = tier(user)
    if level in ('anon', 'unverified'):
        return anon_subject(), config['LIMIT_ANON']
    limit = config['LIMIT_PAID'] if level == 'paid' else config['LIMIT_FREE']
    return f'u:{user.id}', limit


def usage_summary(user):
    """Today's usage for display: ``{'used', 'limit', 'remaining', 'tier'}``.

    ``limit`` and ``remaining`` are None when the tier is unlimited.
    """
    subject, limit = resolve(user)
    used = Usage.get(subject, today())
    return {
        'used': used,
        'limit': limit or None,
        'remaining': max(0, limit - used) if limit else None,
        'tier': tier(user),
    }


def _is_metered():
    return request.method == 'POST' and request.blueprint in METERED_BLUEPRINTS


def _seconds_to_midnight():
    now = datetime.now(timezone.utc)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


def _limit_message(tier, limit):
    free = current_app.config['LIMIT_FREE']
    if tier == 'anon':
        return (f"You've used today's {limit} free operations. Create a free "
                f"account and verify your email for {free} a day at /signup "
                "— or come back after midnight UTC.")
    if tier == 'unverified':
        return (f"You've used today's {limit} guest operations. Verify your "
                f"email (see /account) to raise your limit to {free} a day.")
    return (f"You've used today's {limit} operations. The limit resets at "
            "midnight UTC.")


def check():
    """Before a metered request: remember its meter, or refuse it at the limit."""
    g.meter = None
    if not current_app.config.get('METERING_ENABLED', True) or not _is_metered():
        return None
    user = current_user()
    subject, limit = resolve(user)
    used = Usage.get(subject, today())
    g.meter = (subject, limit, used)
    if limit and used >= limit:
        response = jsonify({
            'error': _limit_message(tier(user), limit),
            'code': 'daily_limit',
            'limit': limit,
            'used': used,
            'signup_url': '/signup',
        })
        response.status_code = 429
        response.headers['Retry-After'] = str(_seconds_to_midnight())
        return response
    return None


def record(response):
    """After a metered request: count it if it succeeded; set the usage headers."""
    meter = g.get('meter')
    if not meter:
        return response
    subject, limit, used = meter
    if response.status_code < 400:
        try:
            Usage.increment(subject, today())
            db.session.commit()
            used += 1
        except SQLAlchemyError:
            # The operation already ran; losing one count beats failing it.
            db.session.rollback()
            log.exception('usage count failed for %s', subject)
    response.headers['X-Docist-Usage-Limit'] = str(limit) if limit else 'unlimited'
    response.headers['X-Docist-Usage-Remaining'] = (
        str(max(0, limit - used)) if limit else 'unlimited'
    )
    return response


def init_app(app):
    @app.context_processor
    def _inject_usage_today():
        # Pages are rendered on GET; skip the DB lookup for anything else.
        if not has_request_context() or request.method != 'GET':
            return {'usage_today': None}
        return {'usage_today': usage_summary(current_user())}
