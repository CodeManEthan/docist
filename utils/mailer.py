"""Outgoing email: one ``send()`` call, three pluggable backends.

Named ``mailer`` rather than ``email`` so it never shadows the stdlib package.
The backend comes from ``DOCIST_EMAIL_BACKEND``:

    console  (default) log the subject and body at WARNING, so verify and
             reset links show up in the dev terminal and in Railway logs
    smtp     smtplib with STARTTLS (``DOCIST_SMTP_TLS=starttls``, default),
             implicit TLS (``ssl``) or plain (``none``)
    resend   one HTTPS POST to the Resend API, no SDK

``send()`` never raises: every failure is logged and returns False, so a
broken sender can never fail a signup or a reset request.

Links in emails are built from ``DOCIST_BASE_URL`` and never from the request
Host header, since a poisoned Host would let an attacker receive someone
else's reset link. Only the console backend falls back to the request's own
root URL; smtp and resend refuse to send while ``DOCIST_BASE_URL`` is unset.

Settings are read from the environment at call time, not import time.
"""
import json
import logging
import os
import smtplib
import urllib.request
from email.message import EmailMessage

from flask import has_request_context, request

log = logging.getLogger(__name__)

RESEND_URL = 'https://api.resend.com/emails'
TIMEOUT = 10
DEFAULT_FROM = 'Docist <no-reply@localhost>'


def backend():
    return (os.environ.get('DOCIST_EMAIL_BACKEND') or 'console').strip().lower()


def configured():
    """True when mail actually leaves the box (any backend but console)."""
    return backend() != 'console'


def _base_url():
    return (os.environ.get('DOCIST_BASE_URL') or '').strip().rstrip('/')


def absolute_url(path):
    """``DOCIST_BASE_URL`` + ``path``; the console backend may use the request root.

    Real senders refuse to send without ``DOCIST_BASE_URL`` (see ``send``), so
    a Host-derived link built here can only ever reach the console log.
    """
    base = _base_url()
    if not base and not configured() and has_request_context():
        base = request.url_root.rstrip('/')
    return f'{base}{path}'


def _from_address():
    return os.environ.get('DOCIST_EMAIL_FROM') or DEFAULT_FROM


def _send_console(to, subject, text):
    log.warning('Email (console backend) to %s\nSubject: %s\n\n%s', to, subject, text)
    return True


def _send_smtp(to, subject, text):
    host = os.environ.get('DOCIST_SMTP_HOST', '').strip()
    if not host:
        log.error('Email not sent: DOCIST_SMTP_HOST is unset')
        return False
    try:
        port = int(os.environ.get('DOCIST_SMTP_PORT') or 587)
    except ValueError:
        log.error('Email not sent: DOCIST_SMTP_PORT is not a number')
        return False
    tls = (os.environ.get('DOCIST_SMTP_TLS') or 'starttls').strip().lower()
    user = os.environ.get('DOCIST_SMTP_USER', '')
    password = os.environ.get('DOCIST_SMTP_PASSWORD', '')

    msg = EmailMessage()
    msg['From'] = _from_address()
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(text)

    if tls == 'ssl':
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    with server:
        if tls == 'starttls':
            server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)
    return True


def _send_resend(to, subject, text):
    api_key = os.environ.get('DOCIST_RESEND_API_KEY', '').strip()
    if not api_key:
        log.error('Email not sent: DOCIST_RESEND_API_KEY is unset')
        return False
    body = json.dumps({
        'from': _from_address(),
        'to': [to],
        'subject': subject,
        'text': text,
    }).encode()
    req = urllib.request.Request(
        RESEND_URL, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        status = getattr(resp, 'status', 200)
    if not 200 <= status < 300:
        log.error('Email not sent: Resend answered %s', status)
        return False
    return True


_BACKENDS = {
    'console': _send_console,
    'smtp': _send_smtp,
    'resend': _send_resend,
}


def get_sender():
    """The send function for the configured backend, or None if unknown."""
    return _BACKENDS.get(backend())


def send(to, subject, text):
    """Send one plain-text email. Never raises; returns True on success."""
    name = backend()
    sender = get_sender()
    if sender is None:
        log.error('Email not sent: unknown DOCIST_EMAIL_BACKEND %r', name)
        return False
    if name != 'console' and not _base_url():
        log.error('Email not sent: DOCIST_BASE_URL must be set for the %s backend', name)
        return False
    try:
        return bool(sender(to, subject, text))
    except Exception:  # noqa: BLE001 -- a sender failure must never escape
        log.exception('Email to %s failed (%s backend)', to, name)
        return False
