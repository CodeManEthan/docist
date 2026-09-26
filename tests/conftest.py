"""Root test configuration: loaded before any test module imports ``app``.

Tests never touch a database they did not create, so DATABASE_URL is
*assigned* (never setdefault: a shell or .env pointing at Postgres must be
overridden) to in-memory SQLite. app.py then also skips instance/secret_key.
"""
import os

os.environ['DATABASE_URL'] = 'sqlite://'
