"""Packet builder page: GET /packet only (design.md §4.1, stage [B2]).

The page stages files, holds them and builds the packet through the
``packet_stage`` and ``packet_build`` blueprints ([B1]), which live in their
own modules so this one stays the page alone. Nothing here is metered:
``METERED_BLUEPRINTS`` names ``packet_build``, never ``packet``.
"""
from flask import Blueprint, current_app, render_template

from utils.identity import current_user
from utils.metering import tier
from utils.uploads import free_limit_bytes, limit_mb, paid_limit_bytes

bp = Blueprint('packet', __name__)

# Staging lifetimes when the app config doesn't set them ([B1] reads
# DOCIST_STAGING_TTL_MINUTES and DOCIST_STAGING_MAX_HOURS into these keys).
DEFAULT_TTL_MINUTES = 120
DEFAULT_MAX_HOURS = 24


def _packet_limit_bytes(user):
    """The most staged PDF bytes one build may read: the tier's request
    limit (§4.6), 50 MB free and 90 MB paid by default."""
    return paid_limit_bytes() if tier(user) == 'paid' else free_limit_bytes()


@bp.route('/packet')
def index():
    config = current_app.config
    packet_limit = _packet_limit_bytes(current_user())
    return render_template(
        'packet.html',
        packet_limit_bytes=packet_limit,
        packet_limit_mb=limit_mb(packet_limit),
        staging_ttl_minutes=config.get('STAGING_TTL_MINUTES', DEFAULT_TTL_MINUTES),
        staging_max_hours=config.get('STAGING_MAX_HOURS', DEFAULT_MAX_HOURS),
    )
