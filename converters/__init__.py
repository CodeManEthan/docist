"""Converter registry: turns non-PDF uploads into PDFs for the merge pipeline.

Each module in this package is a converter plugin. A plugin must define:

    EXTENSIONS = ['.md', '.markdown']   # lowercase, with leading dot

    def convert(input_path, output_path, opts=None):
        '''Convert the file at input_path to a PDF written to output_path.
        Raise ConversionError (or any exception) on failure.'''

``opts`` is a :class:`converters.options.RenderOptions` (paper size and the
like) or None for the defaults. A plugin may leave it out of its signature;
the registry then drops it, so every callable :func:`get_converter` returns
takes ``(input_path, output_path, opts=None)``.

A ``ConversionError`` message reaches the visitor as it is. It names the
upload by :func:`upload_name` (never a path) and says what to do; a library's
own text goes to the log through :func:`logged_error`, never into the message.

Plugins are discovered automatically at import time — just drop a new
module in this directory and restart the app.
"""
import importlib
import inspect
import logging
import os
import pkgutil

log = logging.getLogger(__name__)


class ConversionError(Exception):
    """Raised when a file cannot be converted to PDF."""


def upload_name(path):
    """The name messages give an upload: the file name the route saved it
    under (the name the visitor sent, made safe), never the server path."""
    return os.path.basename(path)


def logged_error(error_type, message, exc):
    """``error_type(message)`` for the visitor, with ``exc`` logged at warning
    level. The message names the upload and says what to do; the library's
    own text can carry a server path, so it goes to the log, not the client.
    Raise the result ``from exc``."""
    log.warning('%s (%s: %s)', message, type(exc).__name__, exc)
    return error_type(message)


_REGISTRY = {}


def accepts_opts(func):
    """True when ``func`` takes a third positional ``opts`` argument."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    opts = params.get('opts')
    if opts is not None:
        return opts.kind in (opts.POSITIONAL_ONLY, opts.POSITIONAL_OR_KEYWORD)
    return any(p.kind == p.VAR_POSITIONAL for p in params.values())


def with_opts(func):
    """``func`` as a ``(input_path, output_path, opts=None)`` callable.

    Inspected once, here, not per call.
    """
    if accepts_opts(func):
        return func

    takes_keyword = 'opts' in inspect.signature(func).parameters

    def call(input_path, output_path, opts=None):
        if takes_keyword:
            return func(input_path, output_path, opts=opts)
        return func(input_path, output_path)
    call.__name__ = getattr(func, '__name__', 'convert')
    call.__wrapped__ = func
    return call


for _mod_info in pkgutil.iter_modules(__path__):
    _module = importlib.import_module(f'{__name__}.{_mod_info.name}')
    _exts = getattr(_module, 'EXTENSIONS', None)
    _convert = getattr(_module, 'convert', None)
    if _exts and callable(_convert):
        for _ext in _exts:
            _REGISTRY[_ext.lower()] = with_opts(_convert)


def supported_extensions():
    """All non-PDF extensions that can be converted, e.g. ['.jpg', '.md']."""
    return sorted(_REGISTRY)


def get_converter(extension):
    """Return ``convert(input_path, output_path, opts=None)`` for an extension, or None."""
    return _REGISTRY.get(extension.lower())
