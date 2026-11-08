"""Public code, docs and tests carry no pointers into private planning notes.

Comments and docstrings say why in plain words. A reference to a design
section, a review tag, a build stage, a planning round or a private notes path
means nothing to a reader of this repository, so this test fails on any
tracked text file that holds one, naming file:line for each hit.

The file list comes from ``git ls-files``; where there is no git checkout (the
Docker image ships without ``.git`` and without ``tests/``) the test skips.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# The planning rounds this project has had. A round name after "round" or
# "design" is a pointer into the notes.
ROUND_NAMES = (
    'accounts',
    'launch-hardening',
    'packet-builder',
    'prelaunch-fixes',
    'redesign',
    'showcase-fixes',
)
_ROUNDS = '|'.join(re.escape(name) for name in ROUND_NAMES)

# Case is as written: "The lead" at the start of a sentence is left alone on
# purpose, so ordinary prose is not caught.
PATTERNS = [
    ('section sign', re.compile(r'§\s*\d')),
    ('bracketed tag', re.compile(r'\[[BCFIRQLMWS]\d+\]')),
    ('stage reference', re.compile(r"\bB\d+'s\b")),
    ('stage reference', re.compile(r'\bstage [BC]\d+\b')),
    ('ruling', re.compile(r'\bruled\s*(?:\[|\(|(?:on\s+)?\d{4}-\d{2}-\d{2})')),
    ('round name', re.compile(r'\bround (?:%s|note)\b' % _ROUNDS)),
    ('design name', re.compile(r'\bdesign (?:%s)\b' % _ROUNDS)),
    ('reviewer', re.compile(r'\bthe critic\b')),
    ('reviewer', re.compile(r'\bthe lead\b')),
    ('wave', re.compile(r'\bWave \d+\b')),
    ('work package', re.compile(r'\bWP-[A-Z]\b')),
    ('rule letter', re.compile(r'\bRule [A-Z]\b')),
    ('fleet tool', re.compile(r'overseer')),
    ('private path', re.compile(r'jpluto')),
    ('private path', re.compile(r'/notes/')),
]

# A test file named for the stage that added it.
STAGE_FILE_NAME = re.compile(r'(?:^|/)test_[bc]\d+_')

# Hits that are allowed: (file, matched text, reason). Keep each entry to one
# file and the exact text the pattern matched, never a whole file.
ALLOWED = [
    # Files under tests/ and static/:
    # (none: every hit there was rewritten.)

    # Files outside tests/ and static/ (Python, templates, Dockerfile, units,
    # office profile, top-level docs) go here, each with its reason.
]


def tracked_files():
    if shutil.which('git') is None:
        pytest.skip('git is not installed, so the tracked files cannot be listed')
    if not (ROOT / '.git').exists():
        pytest.skip('no git checkout here (the Docker image ships without .git)')
    result = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', '-z'],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip('git ls-files failed: %s'
                    % result.stderr.decode('utf-8', 'replace').strip())
    return [name for name in result.stdout.decode('utf-8').split('\0') if name]


def read_text(path):
    """The file's text, or None for a binary or missing file."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b'\0' in data:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def is_allowed(name, matched, used):
    for index, (path, text, _reason) in enumerate(ALLOWED):
        if path == name and text == matched:
            used.add(index)
            return True
    return False


def find_hits(names):
    hits = []
    used = set()
    for name in names:
        if name == SELF:
            continue
        if STAGE_FILE_NAME.search(name) and not is_allowed(name, name, used):
            hits.append('%s: file name carries a stage id' % name)
        text = read_text(ROOT / name)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    if is_allowed(name, match.group(0), used):
                        continue
                    hits.append('%s:%d: %s %r: %s'
                                % (name, number, label, match.group(0), line.strip()))
    return hits, used


def test_no_tracked_file_cites_private_notes():
    hits, _used = find_hits(tracked_files())
    assert not hits, ('%d internal reference(s); say the reason in plain words '
                      'instead, or add an ALLOWED entry with its reason:\n%s'
                      % (len(hits), '\n'.join(hits)))


def test_every_allowed_entry_still_matches():
    _hits, used = find_hits(tracked_files())
    stale = [ALLOWED[index][:2] for index in range(len(ALLOWED)) if index not in used]
    assert not stale, 'ALLOWED entries that match nothing any more: %r' % stale


def test_patterns_catch_the_forms_they_name():
    samples = [
        'see § 4.2', '[B7]', '[C3]', '[R5]', "B2's check", 'stage B3',
        'ruled [Q1]', 'ruled (comms 1)', 'ruled 2026-10-02',
        'round packet-builder', 'round note', 'design launch-hardening',
        'the critic', 'the lead', 'Wave 7', 'WP-A', 'Rule D',
        'overseer', 'jpluto', '/notes/',
    ]
    for sample in samples:
        assert any(p.search(sample) for _label, p in PATTERNS), sample
    plain = ['a ruled line', 'round trip', 'the design', 'The leading edge',
             '[b7]', 'leader', 'Rule of thumb']
    for sample in plain:
        assert not any(p.search(sample) for _label, p in PATTERNS), sample
    assert STAGE_FILE_NAME.search('tests/backend/test_b2_bounds.py')
    assert not STAGE_FILE_NAME.search('tests/backend/test_bounds.py')
