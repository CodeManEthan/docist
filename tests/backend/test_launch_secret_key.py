"""The secret-key file (design launch-hardening v0.4 §14, critic minor 2)."""
import os
import re

import pytest

import app as flask_app_module

persisted = flask_app_module._persisted_secret_key
HEX64 = re.compile(r'[0-9a-f]{64}')
OLD_KEY = 'ab' * 32


def _usable(key, path):
    assert HEX64.fullmatch(key)
    assert open(path).read() == key or open(path).read().strip() == key


@pytest.mark.parametrize('content', ['', 'abc123', 'ab' * 20, 'not a key at all\n'])
def test_an_empty_or_partial_file_is_replaced(tmp_path, content):
    path = tmp_path / 'secret_key'
    path.write_text(content)
    key = persisted(str(path))
    _usable(key, path)
    assert path.read_text() == key
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert persisted(str(path)) == key          # the next start keeps it


def test_a_missing_file_is_created(tmp_path):
    path = tmp_path / 'secret_key'
    key = persisted(str(path))
    _usable(key, path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['secret_key', 'secret_key.lock']


def test_an_old_key_padded_with_whitespace_is_kept(tmp_path):
    path = tmp_path / 'secret_key'
    path.write_text(f'  {OLD_KEY}\n')
    assert persisted(str(path)) == OLD_KEY
    assert path.read_text() == f'  {OLD_KEY}\n'


def test_an_unreadable_file_raises(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root reads anything')
    path = tmp_path / 'secret_key'
    path.write_text(OLD_KEY)
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            persisted(str(path))
    finally:
        path.chmod(0o600)


def test_two_processes_racing_on_a_missing_file_end_with_one_key(tmp_path):
    path = str(tmp_path / 'secret_key')
    readers = []
    for _ in range(6):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:   # pragma: no cover - child
            os.close(read_fd)
            try:
                os.write(write_fd, persisted(path).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        readers.append((pid, read_fd))
    keys = set()
    for pid, read_fd in readers:
        with os.fdopen(read_fd) as fh:
            keys.add(fh.read())
        os.waitpid(pid, 0)
    assert len(keys) == 1
    key = keys.pop()
    assert HEX64.fullmatch(key)
    assert open(path).read() == key
