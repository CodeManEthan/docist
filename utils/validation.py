"""Upload content validation: check magic bytes against the claimed extension.

Extension checks alone trust the client; this sniffs the first bytes of the
saved upload so a renamed executable can't ride in as ``report.pdf``.

Policy (deliberately pragmatic):
- Binary formats with reliable signatures are checked strictly.
- Text-ish formats (markdown, csv, json, ...) have no signature; they are
  accepted as long as they don't contain NUL bytes near the start — the
  converters themselves are lenient with malformed text input.
- Unknown extensions pass through untouched: the converter/transform registry
  is the authority on what is supported, and it fails with its own message.
"""
import os

_SNIFF_LEN = 8192


class UploadValidationError(ValueError):
    """The upload's content does not match its extension."""


# ext -> accepted signatures at offset 0
_MAGIC = {
    '.pdf': (b'%PDF-',),
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
    '.gif': (b'GIF87a', b'GIF89a'),
    '.bmp': (b'BM',),
    '.tiff': (b'II*\x00', b'MM\x00*'),
    '.tif': (b'II*\x00', b'MM\x00*'),
    # OOXML containers are ZIPs (including empty-ish PK\x05\x06 archives).
    '.docx': (b'PK',),
    '.xlsx': (b'PK',),
}

# Formats parsed as text by the converters: no signature, just "not binary".
_TEXT_EXTS = {
    '.txt', '.md', '.markdown', '.html', '.htm', '.csv', '.json',
    '.yaml', '.yml', '.rtf', '.svg',
}


def _head(path):
    with open(path, 'rb') as fh:
        return fh.read(_SNIFF_LEN)


def validate_upload(path, ext=None):
    """Raise UploadValidationError if the file's content belies its extension.

    ``ext`` defaults to the extension of ``path``. Unknown extensions pass.
    """
    if ext is None:
        ext = os.path.splitext(path)[1]
    ext = (ext or '').lower()
    head = _head(path)

    if ext in ('.webp',):
        if not (head[:4] == b'RIFF' and head[8:12] == b'WEBP'):
            raise UploadValidationError(
                f"The file does not look like a {ext} file."
            )
        return

    if ext in ('.heic', '.heif'):
        if head[4:8] != b'ftyp':
            raise UploadValidationError(
                f"The file does not look like a {ext} file."
            )
        return

    signatures = _MAGIC.get(ext)
    if signatures is not None:
        if not any(head.startswith(sig) for sig in signatures):
            raise UploadValidationError(
                f"The file does not look like a {ext} file "
                "(its content doesn't match the extension)."
            )
        return

    if ext in _TEXT_EXTS:
        if b'\x00' in head:
            raise UploadValidationError(
                f"The file claims to be {ext} but contains binary data."
            )
        return

    # Unknown extension: let the converter registry be the judge.
