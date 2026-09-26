"""Models and their helpers: URL normalization, users, API keys, email
tokens and the daily usage counter."""
from datetime import date, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

import app as flask_app_module
from models import (
    ApiKey, EmailToken, Usage, User, database_url, db, hash_token, utcnow,
)


@pytest.fixture
def ctx(client):
    """An app context over the fresh per-test database."""
    with flask_app_module.app.app_context():
        yield db.session


def _user(email="a@x.io", password="pw-12345678"):
    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


class TestDatabaseUrl:
    def test_unset_is_sqlite_file_in_instance(self):
        assert database_url({}) == "sqlite:///docist.db"
        assert database_url({"DATABASE_URL": "  "}) == "sqlite:///docist.db"

    def test_railway_postgres_scheme_is_normalized(self):
        assert (database_url({"DATABASE_URL": "postgres://u:p@h:5432/d"})
                == "postgresql+psycopg://u:p@h:5432/d")

    def test_bare_postgresql_picks_psycopg3(self):
        assert (database_url({"DATABASE_URL": "postgresql://u:p@h/d"})
                == "postgresql+psycopg://u:p@h/d")

    def test_other_urls_pass_through(self):
        assert database_url({"DATABASE_URL": "sqlite://"}) == "sqlite://"
        assert (database_url({"DATABASE_URL": "postgresql+psycopg://h/d"})
                == "postgresql+psycopg://h/d")


class TestSchema:
    def test_tables_survive_app_import(self, ctx):
        # The StaticPool engine must not be disposed after create_all, or the
        # in-memory database (and its tables) would be gone.
        names = set(inspect(db.engine).get_table_names())
        assert {"users", "api_keys", "usage", "email_tokens"} <= names


class TestUser:
    def test_email_is_unique(self, ctx):
        _user()
        db.session.add(User(email="a@x.io", password_hash="x"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_password_round_trip(self, ctx):
        user = _user(password="correct horse")
        assert user.password_hash != "correct horse"
        assert user.check_password("correct horse")
        assert not user.check_password("wrong")
        assert not user.check_password(None)

    def test_defaults_and_flags(self, ctx):
        user = _user()
        assert user.plan == "free" and not user.is_paid
        assert not user.is_verified and user.session_epoch == 0
        assert not user.disabled and user.created_at is not None
        user.plan = "lifetime"
        user.verified_at = utcnow()
        user.bump_epoch()
        assert user.is_paid and user.is_verified and user.session_epoch == 1


class TestApiKey:
    def test_issue_stores_only_the_hash(self, ctx):
        user = _user()
        raw, key = ApiKey.issue(user, "laptop")
        db.session.commit()
        assert raw.startswith("dk_") and len(raw) > 40
        assert key.prefix == raw[:12]
        assert key.key_hash == hash_token(raw) and raw not in key.key_hash

    def test_lookup_hit_sets_last_used(self, ctx):
        user = _user()
        raw, key = ApiKey.issue(user, "k")
        db.session.commit()
        found = ApiKey.lookup(raw)
        assert found is not None and found.id == key.id and found.user.id == user.id
        assert found.last_used_at is not None

    def test_lookup_misses(self, ctx):
        assert ApiKey.lookup("") is None
        assert ApiKey.lookup("dk_nope") is None

    def test_revoked_key_is_not_found(self, ctx):
        user = _user()
        raw, _ = ApiKey.issue(user, "k")
        db.session.commit()
        ApiKey.revoke_all(user)
        db.session.commit()
        assert ApiKey.lookup(raw) is None

    def test_disabled_users_key_is_not_found(self, ctx):
        user = _user()
        raw, _ = ApiKey.issue(user, "k")
        user.disabled = True
        db.session.commit()
        assert ApiKey.lookup(raw) is None

    def test_revoke_all_only_touches_that_users_live_keys(self, ctx):
        alice, bob = _user("alice@x.io"), _user("bob@x.io")
        a1, _ = ApiKey.issue(alice, "1")
        a2, _ = ApiKey.issue(alice, "2")
        b1, _ = ApiKey.issue(bob, "1")
        db.session.commit()
        ApiKey.revoke_all(alice)
        db.session.commit()
        assert ApiKey.lookup(a1) is None and ApiKey.lookup(a2) is None
        assert ApiKey.lookup(b1) is not None


class TestEmailToken:
    def test_consume_once(self, ctx):
        user = _user()
        raw = EmailToken.issue(user, "verify")
        db.session.commit()
        assert EmailToken.peek(raw, "verify").id == user.id
        assert EmailToken.consume(raw, "verify").id == user.id
        db.session.commit()
        assert EmailToken.consume(raw, "verify") is None
        assert EmailToken.peek(raw, "verify") is None

    def test_purpose_must_match(self, ctx):
        raw = EmailToken.issue(_user(), "verify")
        db.session.commit()
        assert EmailToken.consume(raw, "reset") is None

    def test_expired_token_fails(self, ctx):
        raw = EmailToken.issue(_user(), "reset")
        db.session.commit()
        token = db.session.execute(
            select(EmailToken).where(EmailToken.token_hash == hash_token(raw))
        ).scalar_one()
        token.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        assert EmailToken.peek(raw, "reset") is None
        assert EmailToken.consume(raw, "reset") is None

    def test_ttls(self, ctx):
        user = _user()
        EmailToken.issue(user, "verify")
        EmailToken.issue(user, "reset")
        db.session.commit()
        tokens = {t.purpose: t for t in db.session.execute(select(EmailToken)).scalars()}
        assert tokens["verify"].expires_at - tokens["verify"].created_at > timedelta(hours=47)
        assert tokens["reset"].expires_at - tokens["reset"].created_at <= timedelta(hours=1)

    def test_new_reset_supersedes_earlier_reset(self, ctx):
        user = _user()
        verify = EmailToken.issue(user, "verify")
        first = EmailToken.issue(user, "reset")
        db.session.commit()
        second = EmailToken.issue(user, "reset")
        db.session.commit()
        assert EmailToken.consume(first, "reset") is None
        assert EmailToken.consume(second, "reset").id == user.id
        # Only reset tokens are superseded.
        assert EmailToken.consume(verify, "verify").id == user.id


class TestUsage:
    def test_increment_inserts_then_updates(self, ctx):
        day = date(2026, 1, 2)
        assert Usage.get("ip:abc", day) == 0
        Usage.increment("ip:abc", day)
        db.session.commit()
        assert Usage.get("ip:abc", day) == 1
        Usage.increment("ip:abc", day)
        Usage.increment("ip:abc", day)
        db.session.commit()
        assert Usage.get("ip:abc", day) == 3
        assert Usage.get("ip:abc", day + timedelta(days=1)) == 0
        assert Usage.get("u:1", day) == 0

    def test_increment_recovers_from_a_concurrent_insert(self, ctx, monkeypatch):
        # Another worker inserted the row between our UPDATE (0 rows) and our
        # INSERT: the INSERT fails, its savepoint rolls back, the UPDATE reruns
        # -- and earlier work in the same transaction survives.
        day = date(2026, 1, 2)
        Usage.increment("ip:abc", day)
        db.session.commit()
        user = _user()
        user.plan = "monthly"  # uncommitted work that must survive
        real_bump = Usage._bump.__func__
        calls = []

        def racy_bump(cls, subject, d):
            calls.append(1)
            return 0 if len(calls) == 1 else real_bump(cls, subject, d)

        monkeypatch.setattr(Usage, "_bump", classmethod(racy_bump))
        Usage.increment("ip:abc", day)
        db.session.commit()
        assert len(calls) == 2
        assert Usage.get("ip:abc", day) == 2
        assert db.session.get(User, user.id).plan == "monthly"
