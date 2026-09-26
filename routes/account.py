"""The account page: plan and usage, password, sessions, API keys.

Everything here needs a signed-in user (``login_required`` sends anyone else
to /login). API keys need a verified email too, so a throwaway signup can't
mint keys. A new key's raw value is rendered once, in the response to the
POST that created it; only its sha256 is stored, so it can never be shown
again. Every handler commits its writes before returning.
"""
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request

from models import ApiKey, db
from routes.auth import login_required, password_problem
from utils import identity, mailer, metering

bp = Blueprint('account', __name__)

MAX_KEYS = 5
LABEL_MAX = 60


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _live_keys(user):
    return db.session.execute(
        db.select(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at, ApiKey.id)
    ).scalars().all()


def render_account(status=200, new_key=None, new_key_label=None, error=None):
    """Render the account page for the current user."""
    user = identity.current_user()
    return render_template(
        'account.html',
        user=user,
        usage=metering.usage_summary(user),
        keys=_live_keys(user),
        max_keys=MAX_KEYS,
        new_key=new_key,
        new_key_label=new_key_label,
        error=error,
        mail_configured=mailer.configured(),
    ), status


@bp.route('/account')
@login_required
def account():
    return render_account()


@bp.route('/account/password', methods=['POST'])
@login_required
def change_password():
    user = identity.current_user()
    if not user.check_password(request.form.get('current', '')):
        flash('Your current password is wrong, so nothing changed.', 'error')
        return redirect('/account')
    new = request.form.get('password', '')
    problem = password_problem(new, request.form.get('confirm', ''))
    if problem:
        flash(problem, 'error')
        return redirect('/account')

    # A voluntary change signs out other sessions but keeps API keys.
    user.set_password(new)
    user.bump_epoch()
    db.session.commit()
    identity.login_user(user)
    flash('Password changed. Every other session is signed out.', 'ok')
    return redirect('/account')


@bp.route('/account/signout-all', methods=['POST'])
@login_required
def signout_all():
    user = identity.current_user()
    user.bump_epoch()
    db.session.commit()
    identity.login_user(user)
    flash('Signed out everywhere else. This browser stays signed in.', 'ok')
    return redirect('/account')


@bp.route('/account/keys', methods=['POST'])
@login_required
def create_key():
    user = identity.current_user()
    if not user.is_verified:
        flash('Verify your email before creating API keys.', 'error')
        return redirect('/account')
    if len(_live_keys(user)) >= MAX_KEYS:
        flash(f'You already have {MAX_KEYS} keys. Revoke one to make room.', 'error')
        return redirect('/account')

    label = ' '.join((request.form.get('label') or '').split())[:LABEL_MAX]
    raw, _key = ApiKey.issue(user, label or 'API key')
    db.session.commit()
    # Rendered, not redirected: this response is the only place the raw
    # key ever appears.
    return render_account(new_key=raw, new_key_label=label or 'API key')


@bp.route('/account/keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def revoke_key(key_id):
    user = identity.current_user()
    key = db.session.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        abort(404)
    if key.revoked_at is None:
        key.revoked_at = _now()
        db.session.commit()
    flash(f'Revoked key {key.prefix}…', 'ok')
    return redirect('/account')
