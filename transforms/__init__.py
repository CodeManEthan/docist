"""File-to-file transform registry (any format -> any format).

Each module in this package is a transform plugin. A plugin defines:

    TRANSFORMS = {
        ('.png', '.jpg'): convert_png_to_jpg,   # lowercase exts with dot
        ...
    }

where each function has the signature ``func(input_path, output_path)``
and writes the converted file to ``output_path``. A function may return
the path it actually wrote (e.g. a ``.zip`` when one input yields many
outputs); returning ``None`` means ``output_path`` was written as given.
Raise ``TransformError`` (or any exception) on failure.

Plugins are discovered automatically at import time.

Pivot routing: when no direct (src, dst) transform exists but both
(src, '.pdf') and ('.pdf', dst) do, the registry composes them through a
temporary PDF, so e.g. DOCX -> PNG works without a dedicated plugin.
"""
import importlib
import os
import pkgutil
import tempfile


class TransformError(Exception):
    """Raised when a file cannot be transformed to the requested format."""


PIVOT = '.pdf'

_DIRECT = {}

for _mod_info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    _module = importlib.import_module(f'{__name__}.{_mod_info.name}')
    _pairs = getattr(_module, 'TRANSFORMS', None)
    if _pairs:
        for (_src, _dst), _func in _pairs.items():
            _DIRECT.setdefault((_src.lower(), _dst.lower()), _func)


def _run(func, input_path, output_path):
    result = func(input_path, output_path)
    return result or output_path


def _composed(to_pdf, from_pdf):
    def transform(input_path, output_path):
        with tempfile.TemporaryDirectory() as tmp:
            mid = os.path.join(tmp, 'pivot.pdf')
            mid = _run(to_pdf, input_path, mid)
            return _run(from_pdf, mid, output_path)
    return transform


def get_transform(src, dst):
    """Callable(input, output) -> actual output path, or None if unsupported."""
    src, dst = src.lower(), dst.lower()
    if src == dst:
        return None
    direct = _DIRECT.get((src, dst))
    if direct:
        return lambda i, o: _run(direct, i, o)
    to_pdf = _DIRECT.get((src, PIVOT))
    from_pdf = _DIRECT.get((PIVOT, dst))
    if to_pdf and from_pdf:
        return _composed(to_pdf, from_pdf)
    return None


def targets_for(src):
    """Sorted target extensions reachable from ``src`` (direct + pivot)."""
    src = src.lower()
    targets = {d for (s, d) in _DIRECT if s == src}
    if (src, PIVOT) in _DIRECT:
        targets |= {d for (s, d) in _DIRECT if s == PIVOT}
        targets.add(PIVOT)
    targets.discard(src)
    return sorted(targets)


def supported_sources():
    """Sorted source extensions with at least one available target."""
    return sorted({s for (s, _d) in _DIRECT})


def matrix():
    """Full conversion map: {source_ext: [target_ext, ...]} incl. pivots."""
    return {src: targets_for(src) for src in supported_sources()}
