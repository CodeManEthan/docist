"""Periodic pruning of OUTPUT_FOLDER, which otherwise grows forever.

Wired in as an opportunistic before-request hook: at most once every
``interval`` seconds, files older than ``max_age`` are deleted. No cron or
background thread needed for a small self-hosted deployment.
"""
import os
import time


def prune_old_files(folder, max_age, now=None):
    """Delete regular files in ``folder`` whose mtime is older than max_age seconds.

    Returns the number of files removed. Missing folders and races with
    concurrent deletes are ignored.
    """
    if now is None:
        now = time.time()
    removed = 0
    try:
        entries = os.listdir(folder)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(path):
                continue
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


class OutputJanitor:
    """Throttled wrapper: calls prune_old_files at most once per interval."""

    def __init__(self, max_age, interval):
        self.max_age = max_age
        self.interval = interval
        self._last_run = 0.0

    def maybe_prune(self, folder, now=None):
        if now is None:
            now = time.time()
        if now - self._last_run < self.interval:
            return 0
        self._last_run = now
        return prune_old_files(folder, self.max_age, now=now)


class StagingJanitor:
    """The packet staging store's sweep (utils/staging.py), at most once per
    interval, on the same opportunistic schedule as :class:`OutputJanitor`.

    A sweep removes expired and holderless entries, skipping any entry whose
    lock is held (a build or render is reading it), clears lane files
    untouched for a day, and rebuilds the ledger. ``prune_old_files`` is flat
    and mtime-only, so it isn't reused here.
    """

    def __init__(self, interval):
        self.interval = interval
        self._last_run = 0.0

    def maybe_sweep(self, config, now=None):
        if now is None:
            now = time.time()
        if now - self._last_run < self.interval:
            return 0
        self._last_run = now
        from utils.staging import Store
        return Store.from_config(config).sweep()
