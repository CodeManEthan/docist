"""A small in-memory sliding-window rate limiter, keyed per client IP.

Deliberately dependency-free. With multiple gunicorn workers each worker has
its own window, so the effective limit is (limit x workers) — acceptable
for a small self-hosted deployment and documented in DEPLOYMENT.md.
"""
import time
from collections import deque


class RateLimiter:
    """Allow at most ``max_requests`` per ``window`` seconds for each key."""

    def __init__(self, max_requests, window):
        self.max_requests = int(max_requests)
        self.window = float(window)
        self._hits = {}  # key -> deque[timestamps]

    def _prune(self, hits, now):
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def allow(self, key, now=None):
        """Record a request for ``key``; return False if it exceeds the limit."""
        if now is None:
            now = time.time()
        hits = self._hits.setdefault(key, deque())
        self._prune(hits, now)
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        # Bound memory: drop other keys whose windows have fully expired.
        if len(self._hits) > 1024:
            for other in list(self._hits):
                other_hits = self._hits[other]
                self._prune(other_hits, now)
                if not other_hits:
                    del self._hits[other]
        return True

    def retry_after(self, key, now=None):
        """Seconds until the oldest counted request leaves the window."""
        if now is None:
            now = time.time()
        hits = self._hits.get(key)
        if not hits:
            return 0.0
        return max(0.0, hits[0] + self.window - now)
