"""PDF password security: protect (encrypt) and unlock (decrypt) documents.

``protect_pdf`` clones the input into a fresh writer and encrypts it with a
user password using **AES-256** (PDF 2.0 ``/V 5 /R 6``). ``unlock_pdf``
reverses that, writing a decrypted copy.

Note on algorithms: AES-256 needs a crypto backend, which pypdf takes from the
``cryptography`` package (a hard dependency of this project — see
``requirements.txt``). With that backend present pypdf also decrypts every
legacy scheme, so ``unlock_pdf`` opens both the AES-256 files ``protect_pdf``
produces *and* older RC4-40/RC4-128/AES-128 documents created elsewhere.
"""
from pypdf import PdfReader, PdfWriter

# Encryption used by :func:`protect_pdf`. AES-256 (``/V 5 /R 6``) is the
# strongest scheme in the PDF 2.0 spec; the older RC4-128 default is broken.
ENCRYPTION_ALGORITHM = 'AES-256'


def protect_pdf(input_path, output_path, password):
    """Encrypt the PDF at ``input_path`` with ``password``.

    Encryption is AES-256 (see :data:`ENCRYPTION_ALGORITHM`).
    Empty/whitespace-only passwords raise ``ValueError``.
    """
    if password is None or str(password) == '':
        raise ValueError('Password must not be empty')

    reader = PdfReader(input_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    writer.encrypt(password, algorithm=ENCRYPTION_ALGORITHM)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return output_path


def unlock_pdf(input_path, output_path, password):
    """Write a decrypted copy of an encrypted PDF.

    Handles every scheme pypdf supports with the ``cryptography`` backend --
    the AES-256 files :func:`protect_pdf` produces as well as legacy
    RC4-40/RC4-128/AES-128 documents created elsewhere.

    * Not-encrypted input           -> ValueError('PDF is not password-protected')
    * Wrong password / undecryptable -> ValueError('Incorrect password')
    """
    if password is None or str(password) == '':
        raise ValueError('Password must not be empty')

    reader = PdfReader(input_path)

    if not reader.is_encrypted:
        raise ValueError('PDF is not password-protected')

    try:
        result = reader.decrypt(password)
    except Exception:
        # pypdf raises for e.g. an unsupported/corrupt encryption dictionary.
        raise ValueError('Incorrect password')

    # decrypt() returns a falsy PasswordType (NOT_DECRYPTED == 0) on failure.
    if not result:
        raise ValueError('Incorrect password')

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return output_path
