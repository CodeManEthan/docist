"""Unguessable, owner-bound output filenames, shared by every route that writes results.

Every browser route writes its result into the one shared OUTPUT_FOLDER and
hands the client a name to fetch it by from /download. A stored name is

    <16 hex owner tag><32 random hex>_<friendly name>

The owner tag (identity.owner_tag()) is an HMAC of the signed-in user, or of
the anonymous browser session, so /download can check that the name belongs to
whoever is asking. The 128 random bits keep names unguessable and make
collisions between concurrent results practically impossible. Old 32-hex
names no longer match and age out through the output janitor.
"""
import re
import secrets

from flask import has_request_context

_KEY_RE = re.compile(r'^([0-9a-f]{16})[0-9a-f]{32}_(.+)$')


def result_name(name, owner=None):
    """Return the stored filename for a result whose friendly name is ``name``.

    'report.zip' -> '<16 hex owner><32 random hex>_report.zip'. ``owner``
    defaults to the current request's owner tag; outside a request it must be
    given, or this raises RuntimeError.
    """
    if owner is None:
        if not has_request_context():
            raise RuntimeError('result_name() needs an owner outside a request')
        from utils.identity import owner_tag
        owner = owner_tag()
    return f"{owner}{secrets.token_hex(16)}_{name}"


def display_name(stored):
    """The friendly name inside a stored result name, or None if it has no key."""
    match = _KEY_RE.match(stored or '')
    return match.group(2) if match else None


def owner_of(stored):
    """The 16-hex owner tag a stored result name starts with, or None."""
    match = _KEY_RE.match(stored or '')
    return match.group(1) if match else None
