"""Display names for Tesseract language codes.

Covers every pack the image installs (see the Dockerfile). A code with no
name here shows as the code itself.
"""

NAMES = {
    'eng': 'English',
    'spa': 'Spanish',
    'fra': 'French',
    'deu': 'German',
    'por': 'Portuguese',
    'ita': 'Italian',
    'chi_sim': 'Chinese (Simplified)',
    'chi_tra': 'Chinese (Traditional)',
    'vie': 'Vietnamese',
    'ara': 'Arabic',
    'rus': 'Russian',
    'kor': 'Korean',
    'jpn': 'Japanese',
    'fil': 'Filipino',
}

# Tesseract lists these alongside the languages, but they don't recognise text.
NOT_LANGUAGES = {'osd', 'equ'}


def language_name(code):
    """The display name for ``code``, or the code itself."""
    return NAMES.get(code, code)


def language_choices(codes):
    """``[{'code', 'name'}]`` for the recognition languages in ``codes``.

    English first, then the rest by name.
    """
    usable = [c for c in codes if c not in NOT_LANGUAGES]
    usable.sort(key=lambda c: (c != 'eng', language_name(c).lower()))
    return [{'code': c, 'name': language_name(c)} for c in usable]
