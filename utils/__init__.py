"""Shared web-layer helpers: naming, upload validation, cleanup, rate limiting.

Unlike ``pdf_ops`` (pure PDF logic) these helpers exist for the HTTP surface:
they know about output folders, uploads and abuse, but still import no Flask —
routes wire them in.
"""
