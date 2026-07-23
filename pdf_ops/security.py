"""PDF password security: protect (encrypt) and unlock (decrypt) documents.

``protect_pdf`` clones the input into a fresh writer and encrypts it with a
user password using PyPDF2's default algorithm (RC4-128). ``unlock_pdf``
reverses that, writing a decrypted copy.

Note on algorithms: PyPDF2 3.x can *always* handle RC4 (what ``protect_pdf``
produces here) with no extra packages. Decrypting AES-encrypted inputs created
elsewhere additionally needs an AES backend (PyPDF2 looks for PyCryptodome);
that optional package is not installed, so AES-encrypted inputs may fail to
decrypt. Round-tripping documents produced by ``protect_pdf`` always works.
"""
from PyPDF2 import PdfReader, PdfWriter


def protect_pdf(input_path, output_path, password):
    """Encrypt the PDF at ``input_path`` with ``password``.

    Empty/whitespace-only passwords raise ``ValueError``.
    """
    if password is None or str(password) == '':
        raise ValueError('Password must not be empty')

    reader = PdfReader(input_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    writer.encrypt(password)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return output_path


def unlock_pdf(input_path, output_path, password):
    """Write a decrypted copy of an encrypted PDF.

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
        # PyPDF2 raises for e.g. AES inputs without an AES backend.
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
