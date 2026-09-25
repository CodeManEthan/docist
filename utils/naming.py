"""Unguessable output filenames, shared by every route that writes results.

Every browser route writes its result into the one shared OUTPUT_FOLDER and
hands the client a name to fetch it by from /download. Those names used to be
derived from the uploader's filename ('report-merged.pdf', 'report_1-...'),
so anyone past the login gate could fetch another visitor's result by
guessing. Each stored name now starts with 128 random bits that only the
requester is ever told, and /download serves nothing without that prefix.
"""
import re
import secrets

_KEY_RE = re.compile(r'^[0-9a-f]{32}_(.+)$')


def result_name(name):
    """Return the stored filename for a result whose friendly name is ``name``.

    'report.zip' -> '<32 random hex>_report.zip'. The random prefix also makes
    collisions between concurrent results practically impossible.
    """
    return f"{secrets.token_hex(16)}_{name}"


def display_name(stored):
    """The friendly name inside a stored result name, or None if it has no key."""
    match = _KEY_RE.match(stored or '')
    return match.group(1) if match else None
