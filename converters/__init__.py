"""Converter registry: turns non-PDF uploads into PDFs for the merge pipeline.

Each module in this package is a converter plugin. A plugin must define:

    EXTENSIONS = ['.md', '.markdown']   # lowercase, with leading dot

    def convert(input_path, output_path):
        '''Convert the file at input_path to a PDF written to output_path.
        Raise ConversionError (or any exception) on failure.'''

Plugins are discovered automatically at import time — just drop a new
module in this directory and restart the app.
"""
import importlib
import pkgutil


class ConversionError(Exception):
    """Raised when a file cannot be converted to PDF."""


_REGISTRY = {}

for _mod_info in pkgutil.iter_modules(__path__):
    _module = importlib.import_module(f'{__name__}.{_mod_info.name}')
    _exts = getattr(_module, 'EXTENSIONS', None)
    _convert = getattr(_module, 'convert', None)
    if _exts and callable(_convert):
        for _ext in _exts:
            _REGISTRY[_ext.lower()] = _convert


def supported_extensions():
    """All non-PDF extensions that can be converted, e.g. ['.jpg', '.md']."""
    return sorted(_REGISTRY)


def get_converter(extension):
    """Return the convert function for an extension, or None."""
    return _REGISTRY.get(extension.lower())
