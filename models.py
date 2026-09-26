"""Database models: users, API keys, daily usage counters and email tokens.

One ``db = SQLAlchemy()`` instance, bound to the app in app.py. The helpers on
each model are the interface the auth, account and metering code builds on.

Commit discipline: Flask-SQLAlchemy rolls back uncommitted work when the app
context ends, so every write is committed explicitly at a named point. The
helpers here only stage changes (``add`` / ``UPDATE``) and leave the commit to
the caller -- except ``ApiKey.lookup``, which commits its own ``last_used_at``
so the stamp survives whatever the request does next.

Timestamps are naive UTC ``DateTime`` values (SQLite has no time zones).
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

API_KEY_PREFIX = 'dk_'
TOKEN_TTL = {
    'verify': timedelta(hours=48),
    'reset': timedelta(hours=1),
}


def utcnow():
    """The current UTC time as a naive datetime (the form every column stores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(raw):
    """sha256 hex of a raw key or token -- the only form kept at rest."""
    return hashlib.sha256((raw or '').encode()).hexdigest()


def database_url(env):
    """The SQLAlchemy URL for ``env`` (a mapping such as ``os.environ``).

    Unset -> a SQLite file that Flask-SQLAlchemy places in ``instance/``.
    ``postgres://`` (Railway's scheme, dropped by SQLAlchemy 2) and a bare
    ``postgresql://`` (which would pick psycopg2, not installed) both become
    ``postgresql+psycopg://``.
    """
    url = (env.get('DATABASE_URL') or '').strip()
    if not url:
        return 'sqlite:///docist.db'
    for scheme in ('postgres://', 'postgresql://'):
        if url.startswith(scheme):
            return 'postgresql+psycopg://' + url[len(scheme):]
    return url


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    verified_at = db.Column(db.DateTime, nullable=True)
    plan = db.Column(db.String(20), nullable=False, default='free')
    session_epoch = db.Column(db.Integer, nullable=False, default=0)
    disabled = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @staticmethod
    def normalize_email(email):
        return (email or '').strip().lower()

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw or '')

    def bump_epoch(self):
        """Invalidate every session issued for this user so far."""
        self.session_epoch = (self.session_epoch or 0) + 1

    @property
    def is_verified(self):
        return self.verified_at is not None

    @property
    def is_paid(self):
        return (self.plan or 'free') != 'free'


class ApiKey(db.Model):
    __tablename__ = 'api_keys'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    label = db.Column(db.String(60), nullable=False, default='')
    prefix = db.Column(db.String(12), nullable=False)
    key_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')

    @classmethod
    def issue(cls, user, label):
        """Create a key for ``user``; return ``(raw, key)``. Caller commits.

        The raw key is shown once and never stored: 256 bits of entropy make a
        plain sha256 enough at rest.
        """
        raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
        key = cls(
            user_id=user.id, label=(label or '')[:60], prefix=raw[:12],
            key_hash=hash_token(raw),
        )
        db.session.add(key)
        return raw, key

    @classmethod
    def lookup(cls, raw):
        """The live key for ``raw``, or None if unknown, revoked or its user is
        disabled. Stamps and commits ``last_used_at`` on a hit."""
        if not raw:
            return None
        key = db.session.execute(
            select(cls).join(User, User.id == cls.user_id).where(
                cls.key_hash == hash_token(raw),
                cls.revoked_at.is_(None),
                User.disabled.is_(False),
            )
        ).scalar_one_or_none()
        if key is not None:
            key.last_used_at = utcnow()
            db.session.commit()
        return key

    @classmethod
    def revoke_all(cls, user):
        """Revoke every live key ``user`` holds. Caller commits."""
        db.session.execute(
            update(cls)
            .where(cls.user_id == user.id, cls.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )


class Usage(db.Model):
    """One row per subject per UTC day: how many metered operations succeeded."""
    __tablename__ = 'usage'

    subject = db.Column(db.String(64), primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    count = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def get(cls, subject, day):
        count = db.session.execute(
            select(cls.count).where(cls.subject == subject, cls.day == day)
        ).scalar_one_or_none()
        return count or 0

    @classmethod
    def _bump(cls, subject, day):
        return db.session.execute(
            update(cls)
            .where(cls.subject == subject, cls.day == day)
            .values(count=cls.count + 1)
        ).rowcount

    @classmethod
    def increment(cls, subject, day):
        """Add one to the counter, creating the row on first use. Caller commits.

        UPDATE first; if no row exists, INSERT; if a concurrent request inserted
        it in between, the INSERT's savepoint rolls back and the UPDATE runs
        again. The same statements work on SQLite and Postgres.
        """
        if cls._bump(subject, day):
            return
        try:
            with db.session.begin_nested():
                db.session.add(cls(subject=subject, day=day, count=1))
        except IntegrityError:
            cls._bump(subject, day)


class EmailToken(db.Model):
    __tablename__ = 'email_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    purpose = db.Column(db.String(10), nullable=False)  # 'verify' | 'reset'
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship('User')

    @classmethod
    def issue(cls, user, purpose):
        """Create a ``purpose`` token for ``user``; return the raw token.

        A new reset token supersedes every earlier unused one. Caller commits.
        """
        now = utcnow()
        if purpose == 'reset':
            db.session.execute(
                update(cls)
                .where(cls.user_id == user.id, cls.purpose == 'reset',
                       cls.used_at.is_(None))
                .values(used_at=now)
            )
        raw = secrets.token_urlsafe(32)
        db.session.add(cls(
            user_id=user.id, purpose=purpose, token_hash=hash_token(raw),
            expires_at=now + TOKEN_TTL[purpose],
        ))
        return raw

    @classmethod
    def peek(cls, raw, purpose):
        """The user a live token belongs to, without using it up (or None)."""
        token = db.session.execute(
            select(cls).where(
                cls.token_hash == hash_token(raw), cls.purpose == purpose,
                cls.used_at.is_(None), cls.expires_at > utcnow(),
            )
        ).scalar_one_or_none()
        return token.user if token is not None else None

    @classmethod
    def consume(cls, raw, purpose):
        """Use up a live token and return its user, or None. Caller commits.

        The single-use check and the use are one conditional UPDATE, never a
        read followed by a write, so two concurrent consumers cannot both win.
        """
        if not raw:
            return None
        now = utcnow()
        digest = hash_token(raw)
        result = db.session.execute(
            update(cls)
            .where(cls.token_hash == digest, cls.purpose == purpose,
                   cls.used_at.is_(None), cls.expires_at > now)
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        token = db.session.execute(
            select(cls).where(cls.token_hash == digest)
        ).scalar_one()
        return token.user
