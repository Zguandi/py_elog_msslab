# windows system path parsing.
"""Clean up a Windows path pasted into a prompt.

Handles what Explorer and humans actually paste: wrapping quotes, invisible bidi marks,
forward slashes, ``%VARS%`` and ``~``::

    import os
    from pathlib import Path
    from elog.path_utils_win import to_path

    raw = input('Markdown file: ')
    path = to_path(raw) if os.name == 'nt' else Path(raw.strip())

``to_path`` parses with ``PureWindowsPath``, so it works on any platform and a caller
that would rather not branch on ``os.name`` does not have to.

Note that backslashes are *not* doubled. A path arriving at runtime already holds single
literal backslashes and needs no un-escaping; ``repr()`` merely displays them doubled.
Doubling only matters when writing a path into Python source, which is what
:func:`to_python_literal` is for -- display only, never for opening a file.
"""

import os
import re
from pathlib import Path, PureWindowsPath

__all__ = [
    'clean',
    'to_path',
    'to_windows_string',
    'looks_like_windows_path',
    'to_python_literal',
]

#: Explorer's "Copy as path" wraps its result in double quotes; people and shells
#: sometimes use single ones.
_QUOTES = ('"', "'")

#: Some Windows builds prefix "Copy as path" output with an invisible bidi mark, which
#: otherwise ends up inside the drive letter and makes every later comparison fail.
_INVISIBLE = '\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff'

#: A drive letter followed by a colon, e.g. 'D:'.
_DRIVE_RE = re.compile(r'^[A-Za-z]:')


def clean(raw):
    """Strip whitespace, invisible marks, and one layer of matching quotes."""
    text = (raw or '').strip().strip(_INVISIBLE).strip()
    for quote in _QUOTES:
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
            break
    return text


def looks_like_windows_path(raw):
    """True when the text looks like a Windows path rather than a POSIX one."""
    text = clean(raw)
    return bool(_DRIVE_RE.match(text)) or text.startswith('\\\\') or '\\' in text


def to_windows_string(raw, expand=True):
    """Return the cleaned path as a canonical Windows string, single backslashes.

    Accepts either slash direction and collapses redundant separators.

    :param expand: expand ``%VARS%``, ``$VARS`` and a leading ``~``
    :raises ValueError: if nothing is left after cleaning
    """
    text = clean(raw)
    if not text:
        raise ValueError('Empty path.')
    if expand:
        text = os.path.expanduser(os.path.expandvars(text))
    # PureWindowsPath applies Windows parsing rules (drive letters, UNC, both slash
    # directions) whatever OS is running, so this behaves identically on Linux CI.
    return str(PureWindowsPath(text))


def to_path(raw, expand=True, must_exist=False):
    """Turn a pasted Windows path into a :class:`pathlib.Path`.

    :param expand: expand ``%VARS%``, ``$VARS`` and a leading ``~``
    :param must_exist: raise if the path does not point at an existing file or directory
    :raises ValueError: if the text is empty once cleaned
    :raises FileNotFoundError: if ``must_exist`` and it does not exist
    """
    text = to_windows_string(raw, expand=expand)

    # On Windows use the text directly; elsewhere reinterpret the Windows-parsed parts
    # so the function stays usable (and testable) off-platform.
    path = Path(text) if os.name == 'nt' else Path(PureWindowsPath(text).as_posix())

    if must_exist and not path.exists():
        raise FileNotFoundError('No such file or directory: {0}'.format(path))
    return path


def to_python_literal(raw, quote="'"):
    """Render the path as a Python source literal, for printing -- not for opening.

    Emits a raw string, falling back to a doubled-backslash literal when the path ends
    in a backslash (which a raw string cannot express).

    :param quote: the quote character to wrap with
    """
    if quote not in _QUOTES:
        raise ValueError('quote must be a single or double quote character.')

    text = to_windows_string(raw)
    if quote not in text and not text.endswith('\\'):
        # A raw string is the honest representation: no escaping, nothing to get wrong.
        return 'r{0}{1}{0}'.format(quote, text)
    body = text.replace('\\', '\\\\').replace(quote, '\\' + quote)
    return '{0}{1}{0}'.format(quote, body)
