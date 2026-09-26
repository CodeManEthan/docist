"""Accounts: sign up, sign in and out, email verification, password reset.

Sessions are Flask's signed cookie. ``utils.identity`` owns the session
fields (``uid``/``epoch``) and loads ``g.user`` before every request; this
module only calls ``login_user``/``logout_user`` and never reads the cookie
itself. Every DB write here commits explicitly before the handler returns,
because Flask-SQLAlchemy rolls back uncommitted work at teardown.

Enumeration: sign-up says when an email already has an account (the user
needs to know to sign in instead); forgot-password never does. Login gives
one message for every failure and checks a dummy hash for unknown emails so
both failures take the same time.

Throttles are in-memory sliding windows (``utils.ratelimit.RateLimiter``),
per worker, keyed by ``identity.client_ip()`` and, where it matters, by the
normalized email. They are skipped under ``app.testing`` unless the
``AUTH_THROTTLE_FORCE`` config key is set.
"""
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request,
)
from werkzeug.security import check_password_hash, generate_password_hash

from models import ApiKey, EmailToken, User, db
from utils import identity, mailer
from utils.ratelimit import RateLimiter

bp = Blueprint('auth', __name__)

PASSWORD_MIN = 8
PASSWORD_MAX = 256
EMAIL_MAX = 254

THROTTLED = 'Too many attempts. Try again in a few minutes.'
LOGIN_FAILED = 'Wrong email or password.'
FORGOT_SENT = "If an account exists for that address, we've sent a reset link."
LINK_DEAD = 'This link has expired or has already been used.'

# Checked against when the email is unknown, so a miss costs one scrypt
# verification just like a wrong password does.
_DUMMY_HASH = generate_password_hash('docist-dummy-password-for-timing')

_MINUTE = 60
_HOUR = 60 * _MINUTE


def _new_limiters():
    return {
        'login_ip': RateLimiter(10, 15 * _MINUTE),
        'login_email': RateLimiter(5, 15 * _MINUTE),
        'signup_ip': RateLimiter(5, _HOUR),
        'forgot_ip': RateLimiter(5, _HOUR),
        'forgot_email': RateLimiter(3, _HOUR),
        'resend_ip': RateLimiter(5, _HOUR),
    }


limiters = _new_limiters()


def reset_throttles():
    """Forget every throttle hit (tests call this between cases)."""
    limiters.clear()
    limiters.update(_new_limiters())


def _throttled(*checks):
    """Record one hit per ``(limiter name, key)``; True if any is over its limit."""
    if current_app.testing and not current_app.config.get('AUTH_THROTTLE_FORCE'):
        return False
    over = False
    for name, key in checks:
        if not limiters[name].allow(key):
            over = True
    return over


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(raw):
    return (raw or '').strip().lower()


def _email_ok(email):
    if not email or len(email) > EMAIL_MAX or any(c.isspace() for c in email):
        return False
    local, at, domain = email.rpartition('@')
    return bool(at and local and '.' in domain.strip('.'))


def password_problem(password, confirm):
    """The error for a new password, or None when it is acceptable."""
    if len(password) < PASSWORD_MIN:
        return f'Use at least {PASSWORD_MIN} characters for the password.'
    if len(password) > PASSWORD_MAX:
        return f'Use at most {PASSWORD_MAX} characters for the password.'
    if password != confirm:
        return "The two passwords don't match."
    return None


def _safe_next(target):
    """Allow only a same-origin path as a redirect target.

    Checked as the browser will read it: a leading ``//`` or ``/\\`` is
    another host, a backslash anywhere is read as ``/``, and browsers drop
    tabs and newlines before parsing, so control characters are refused too.
    """
    if not target or not target.startswith('/'):
        return '/'
    if '\\' in target or any(ord(c) < 0x20 or ord(c) == 0x7f for c in target):
        return '/'
    if len(target) > 1 and target[1] in '/\\':
        return '/'
    return target


def login_required(view):
    """Send anonymous visitors to /login, coming back here afterwards."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if identity.current_user() is None:
            target = request.path if request.method == 'GET' else '/account'
            return redirect(f'/login?next={target}')
        return view(*args, **kwargs)
    return wrapped


def _find_user(email):
    return db.session.execute(
        db.select(User).where(User.email == email)
    ).scalar_one_or_none()


def _dead_link():
    return render_template(
        'message.html', title='Link expired', body=LINK_DEAD,
        link_href='/forgot', link_text='Request a new reset link',
    ), 400


# --------------------------------------------------------------------------
# Emails (plain text)
# --------------------------------------------------------------------------
VERIFY_EMAIL = """\
Confirm your email for Docist

Open this link to verify {email}:

{link}

The link works once and expires in 48 hours. Verifying raises your daily
limit and lets you create API keys.

If you didn't create a Docist account, ignore this email.
"""

RESET_EMAIL = """\
Reset your Docist password

Someone asked to reset the password for {email}. To choose a new one, open:

{link}

The link works once and expires in 1 hour. Resetting signs out every session
and revokes your API keys.

If you didn't ask for this, ignore this email; your password stays as it is.
"""


def send_verify_email(user):
    """Issue a verify token, commit it, and send the link. Returns send()'s bool."""
    raw = EmailToken.issue(user, 'verify')
    db.session.commit()
    link = mailer.absolute_url(f'/verify/{raw}')
    return mailer.send(
        user.email, 'Confirm your email for Docist',
        VERIFY_EMAIL.format(email=user.email, link=link),
    )


def _send_detached(to, subject, text):
    """Hand one email to ``mailer.send`` on a daemon thread and return the thread.

    /forgot must answer in the same time whether or not the account exists; a
    blocking SMTP or Resend round trip on the known-account path alone would
    give it away. ``send`` needs no app context and never raises.
    """
    thread = threading.Thread(
        target=mailer.send, args=(to, subject, text),
        name='docist-mail', daemon=True,
    )
    thread.start()
    return thread


def _send_reset_email(user):
    """Issue a reset token, commit it, and send the link without waiting."""
    raw = EmailToken.issue(user, 'reset')
    db.session.commit()
    link = mailer.absolute_url(f'/reset/{raw}')
    return _send_detached(
        user.email, 'Reset your Docist password',
        RESET_EMAIL.format(email=user.email, link=link),
    )


def _verify_flash(sent, email):
    if not mailer.configured():
        flash("Email isn't set up on this instance yet, so your account "
              "runs on guest limits for now.", 'info')
    elif sent:
        flash(f'We sent a verification link to {email}.', 'ok')
    else:
        flash("We couldn't send the verification email just now. "
              "Try the resend button in a few minutes.", 'error')


# --------------------------------------------------------------------------
# Sign up, sign in, sign out
# --------------------------------------------------------------------------
@bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if identity.current_user() is not None:
        return redirect('/account')
    if request.method == 'GET':
        return render_template('signup.html', error=None, email='')

    email = normalize_email(request.form.get('email'))
    password = request.form.get('password', '')
    confirm = request.form.get('confirm', '')

    def fail(message, status=400):
        return render_template('signup.html', error=message, email=email), status

    if _throttled(('signup_ip', identity.client_ip())):
        return fail(THROTTLED, 429)
    if not _email_ok(email):
        return fail('Enter a valid email address.')
    problem = password_problem(password, confirm)
    if problem:
        return fail(problem)

    exists = ('An account with that email already exists. '
              'Sign in instead.')
    if _find_user(email) is not None:
        return fail(exists)
    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:  # IntegrityError: lost a race to the same email
        db.session.rollback()
        if _find_user(email) is not None:
            return fail(exists)
        raise

    identity.login_user(user)
    _verify_flash(send_verify_email(user), email)
    return redirect('/account')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if identity.current_user() is not None:
            return redirect(_safe_next(request.args.get('next')))
        return render_template(
            'login.html', error=None, email='',
            next=_safe_next(request.args.get('next')),
        )

    email = normalize_email(request.form.get('email'))
    password = request.form.get('password', '')
    next_target = _safe_next(request.form.get('next'))

    def fail(message, status):
        return render_template(
            'login.html', error=message, email=email, next=next_target,
        ), status

    if _throttled(('login_ip', identity.client_ip()), ('login_email', email)):
        return fail(THROTTLED, 429)

    user = _find_user(email) if email else None
    if user is None:
        check_password_hash(_DUMMY_HASH, password)
        return fail(LOGIN_FAILED, 401)
    if not user.check_password(password) or user.disabled:
        return fail(LOGIN_FAILED, 401)

    identity.login_user(user)
    return redirect(next_target)


@bp.route('/logout', methods=['POST'])
def logout():
    identity.logout_user()
    return redirect('/')


# --------------------------------------------------------------------------
# Email verification
# --------------------------------------------------------------------------
@bp.route('/verify/<token>')
def verify(token):
    user = EmailToken.consume(token, 'verify')
    if user is None:
        db.session.rollback()
        return render_template(
            'message.html', title='Link expired', body=LINK_DEAD,
            link_href='/account', link_text='Go to your account to get a new one',
        ), 400
    if user.verified_at is None:
        user.verified_at = _now()
    db.session.commit()
    return render_template(
        'message.html', title='Email verified',
        body=f'Thanks. {user.email} is verified, so your account now has its '
             'full daily limit and can create API keys.',
        link_href='/account', link_text='Go to your account',
    )


@bp.route('/verify/resend', methods=['POST'])
@login_required
def verify_resend():
    user = identity.current_user()
    if user.is_verified:
        flash('Your email is already verified.', 'ok')
        return redirect('/account')
    if _throttled(('resend_ip', identity.client_ip())):
        from routes.account import render_account  # account imports this module
        return render_account(status=429, error=THROTTLED)
    _verify_flash(send_verify_email(user), user.email)
    return redirect('/account')


# --------------------------------------------------------------------------
# Forgotten password
# --------------------------------------------------------------------------
@bp.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'GET':
        return render_template('forgot.html', error=None, sent=False, email='')

    email = normalize_email(request.form.get('email'))
    if _throttled(('forgot_ip', identity.client_ip()), ('forgot_email', email)):
        return render_template(
            'forgot.html', error=THROTTLED, sent=False, email=email,
        ), 429

    user = _find_user(email) if _email_ok(email) else None
    if user is not None and not user.disabled:
        _send_reset_email(user)
    # The same answer, and no wait on the mail server, whether or not the
    # account exists.
    return render_template('forgot.html', error=None, sent=True, email='')


@bp.route('/reset/<token>', methods=['GET', 'POST'])
def reset(token):
    if request.method == 'GET':
        if EmailToken.peek(token, 'reset') is None:
            return _dead_link()
        return render_template('reset.html', token=token, error=None)

    password = request.form.get('password', '')
    problem = password_problem(password, request.form.get('confirm', ''))
    if problem:
        # Checked before consuming, so a typo doesn't burn the link.
        if EmailToken.peek(token, 'reset') is None:
            return _dead_link()
        return render_template('reset.html', token=token, error=problem), 400

    user = EmailToken.consume(token, 'reset')
    if user is None or user.disabled:
        db.session.rollback()
        return _dead_link()

    # Recovery ends every credential issued before it: sessions and keys.
    user.set_password(password)
    if user.verified_at is None:
        user.verified_at = _now()  # they just proved they own the inbox
    user.bump_epoch()
    ApiKey.revoke_all(user)
    db.session.commit()

    identity.login_user(user)
    flash('Your password is reset and every other session is signed out. '
          'Any API keys you had were revoked; create new ones below.', 'ok')
    return redirect('/account')
