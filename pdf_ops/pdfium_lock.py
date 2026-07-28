"""A process-wide lock serializing every use of pypdfium2.

PDFium itself is not thread-safe: two threads rendering at the same time
corrupt the allocator (observed as ``malloc(): unaligned tcache chunk
detected`` killing the process when the merge page requests several
thumbnails concurrently under the threaded dev server). Every module that
touches ``pypdfium2`` must hold :data:`PDFIUM_LOCK` from document open to
document close. Gunicorn's sync workers are single-threaded per request, so
there the lock is simply uncontended.
"""
import threading

PDFIUM_LOCK = threading.Lock()
