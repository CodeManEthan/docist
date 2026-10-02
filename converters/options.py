"""Per-request render options, passed explicitly to converters and transforms.

Every per-request choice that changes how a file renders reaches the renderer
as one ``RenderOptions`` argument, built once per request in the web layer
(:mod:`utils.render_opts`). This module has no Flask dependency, so
``converters``, ``transforms`` and ``pdf_ops`` stay free of it.

Not a converter plugin: it defines no ``EXTENSIONS``, so the registry skips it.
"""
from dataclasses import dataclass, field

# PDF points, portrait (width, height).
PAPER_SIZES = {'letter': (612.0, 792.0), 'a4': (595.276, 841.89)}
DEFAULT_PAPER = 'letter'

# The CSS ``@page { size: ... }`` keyword for each paper.
PAPER_CSS = {'letter': 'letter', 'a4': 'A4'}


@dataclass
class RenderOptions:
    paper: str = DEFAULT_PAPER     # a key of PAPER_SIZES
    ocr_language: str = 'eng'      # Tesseract code(s), '+'-joined, already validated
    word_engine: str = 'reflow'    # 'reflow' | 'libreoffice'
    deadline: float | None = None  # time.monotonic() value rendering must finish by
    reflow_max_bytes: int | None = None  # a Word file over this is never re-flowed
    notes: list = field(default_factory=list)   # user-facing notes a renderer appends


def resolve(opts):
    """``opts`` itself, or default options when it is ``None``."""
    return opts if opts is not None else RenderOptions()


def paper_size(opts, landscape=False):
    """``(width, height)`` in points for the options' paper."""
    w, h = PAPER_SIZES[resolve(opts).paper]
    return (h, w) if landscape else (w, h)


def paper_css(opts):
    """The ``@page`` size keyword for the options' paper."""
    return PAPER_CSS[resolve(opts).paper]
