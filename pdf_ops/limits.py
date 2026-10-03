"""The launch-hardening limits, their refusal messages, and the frame check.

No Flask here. Every limit is a module constant, so a test patches it down
here (the one place it is read) and uses a small fixture; nothing in this
round builds a full-size bomb (design launch-hardening v0.4 §2, §15).

Rule A bounds memory, time and file size by the job each request's work runs
in (:mod:`pdf_ops.jobs`). The up-front counts here exist only where the user
can act on the number (pixels per frame, split parts) or where a small file
would otherwise hold a worker until the deadline (table cells, a zip's
central directory) (Rule B).

Every refusal is a :class:`LimitError`, a ``ValueError``, so a route that
already answers ``ValueError`` with a 400 keeps doing so. The messages are
the ruled wording (round note [C3]) with the ruled numbers ([C2]).
"""
import math

MIB = 1024 * 1024

JOB_MEM_BYTES = 768 * MIB            # each process of a job, above where it started
RESULT_BYTES = 200 * MIB             # each file a job writes; all files one export or split writes
FRAME_PIXELS = 36_000_000            # one decoded frame or rendered page
SPLIT_PARTS = 1000                   # parts in one split
TABLE_CELLS_READ = 2_000_000         # spreadsheet cells read per request, padding included
MAX_CENTRAL_DIR_BYTES = 4 * MIB      # a zip's central directory, before zipfile reads it
FORM_FIELD_BYTES = 1 * MIB           # one multipart text field
DEFAULT_BUDGET = 90                  # seconds, when no request set a deadline


class LimitError(ValueError):
    """A request went over one of the round's limits. Routes answer 400 with
    the message. ``kind`` names the limit ('memory', 'file', 'time', 'died',
    'frame', 'split', 'cells', 'archive', ...)."""

    def __init__(self, message, kind='limit'):
        super().__init__(message)
        self.kind = kind

    def __reduce__(self):
        return (type(self), (str(self), self.kind))


def _number(value):
    """``value`` with thousands separators, or a short decimal when small."""
    if value == int(value):
        return f'{int(value):,}'
    return f'{value:g}'


def _mb(nbytes):
    return _number(round(nbytes / MIB, 3))


def _megapixels(pixels):
    return _number(round(pixels / 1_000_000, 3))


# ---------------------------------------------------------------------------
# Messages (design-records.md, "Refusal wording", ruled [C3])
# ---------------------------------------------------------------------------
def memory_message():
    return ('This file needs more memory to convert than Docist allows for one '
            'request. Try a smaller file or split it.')


def time_message(budget=None):
    budget = DEFAULT_BUDGET if budget is None else budget
    return (f'This took longer than Docist allows for one request ({_number(budget)} '
            'seconds, counting the upload). Try a smaller file or fewer pages.')


def died_message():
    return ("Docist couldn't convert this file. It may be damaged, or too complex "
            'to convert.')


def file_message():
    return (f'Converting this would write a file over {_mb(RESULT_BYTES)} MB, so '
            'Docist stopped. Try fewer pages or a smaller file.')


def archive_message():
    return ('This file unpacks to more than Docist can open. Save it again from Word '
            'or Excel, or split it into smaller files.')


def cells_message():
    return (f'This spreadsheet has more than {_number(TABLE_CELLS_READ)} cells, counting '
            'empty rows and columns it declares. Save just the range you need and '
            'try again.')


YAML_MESSAGE = 'This YAML file refers to itself or nests too deeply to turn into JSON.'


def image_message():
    return (f'This image is over {_megapixels(FRAME_PIXELS)} megapixels. Resize it '
            'and try again.')


def page_dpi_message(page_number, dpi):
    return (f'Page {page_number} is over {_megapixels(FRAME_PIXELS)} megapixels at '
            f'{dpi} DPI. Choose a lower DPI.')


def page_fixed_message(page_number):
    return (f'Page {page_number} is too large to render. Docist can render pages up '
            f'to {_megapixels(FRAME_PIXELS)} megapixels.')


def split_message():
    return f'A split can make at most {_number(SPLIT_PARTS)} files.'


FORM_MESSAGE = 'Part of this form is too large, or it has too many files.'
CHUNKED_MESSAGE = ('This upload is too large, or part of the form is, or it has too '
                   'many files.')


def merge_unsupported_message(names):
    return f"Docist can't merge {', '.join(names)}. Remove them and try again."


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def frame_ok(width, height):
    """True when a ``width`` x ``height`` frame is within ``FRAME_PIXELS``."""
    return int(width) * int(height) <= FRAME_PIXELS


def check_frame(width, height, message=None):
    """Raise :class:`LimitError` when one frame or page is over ``FRAME_PIXELS``.

    Call it after a seek and before the frame is loaded. ``message`` defaults
    to the image message."""
    if not frame_ok(width, height):
        raise LimitError(message or image_message(), kind='frame')


def render_page(page, scale, *, page_number, dpi=None):
    """``page.render(scale=scale)`` (a pypdfium2 page), after checking the
    bitmap it would make against ``FRAME_PIXELS``.

    ``ceil(w * scale) * ceil(h * scale)`` from the page's size in points. Over
    the limit: the DPI message when ``dpi`` is given (the user chose it), else
    the fixed-resolution message.
    """
    width, height = page.get_size()
    pixels_w = math.ceil(abs(width) * scale)
    pixels_h = math.ceil(abs(height) * scale)
    if not frame_ok(pixels_w, pixels_h):
        if dpi is not None:
            message = page_dpi_message(page_number, dpi)
        else:
            message = page_fixed_message(page_number)
        raise LimitError(message, kind='frame')
    return page.render(scale=scale)


def charge_written(opts, nbytes):
    """Count ``nbytes`` a request wrote against ``RESULT_BYTES`` (Rule C, per
    request). ``opts`` is anything with a ``written`` attribute (a
    RenderOptions, or :class:`WriteTotal`)."""
    opts.written += int(nbytes)
    if opts.written > RESULT_BYTES:
        raise LimitError(file_message(), kind='file')


class WriteTotal:
    """A running total for a call that has no RenderOptions."""

    def __init__(self):
        self.written = 0


class CellCount:
    """Counts spreadsheet cells as ``openpyxl`` yields them, padding included."""

    def __init__(self, opts=None):
        self._opts = opts
        self.cells = 0 if opts is None else getattr(opts, 'cells_read', 0)

    def add(self, n):
        self.cells += n
        if self._opts is not None:
            self._opts.cells_read = self.cells
        if self.cells > TABLE_CELLS_READ:
            raise LimitError(cells_message(), kind='cells')
