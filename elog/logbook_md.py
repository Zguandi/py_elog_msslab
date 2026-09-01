"""Configuration and credential layer on top of :class:`elog.logbook.Logbook`.

Instead of hardcoding connection parameters and credentials at every call site, this
module reads the connection settings from a flat YAML file, prompts for the username
and password on the terminal, and hands back a ready-to-use ``Logbook``::

    from elog.logbook_md import open_from_config

    logbook = open_from_config('elog.yaml')   # prompts for user + password
    logbook.post('hello', attributes={'Author': 'me'})

The layer is deliberately built by *composition* rather than by subclassing ``Logbook``:
config parsing, credential acquisition, and HTTP access stay independently testable.

It also normalises several sharp edges in ``Logbook.__init__``:

* a bare ``hostname`` with no scheme silently produces the URL ``https:///<logbook>/``,
  so the scheme is always filled in here;
* ``port`` is compared against the ints 80/443, so a string port leaks a redundant
  ``:80`` into the URL -- ports are coerced to ``int``;
* an empty password is hashed into a real digest rather than treated as absent, so an
  empty password is rejected at the prompt.

The module also exports ELOG entries as Markdown notes with YAML frontmatter, suitable
for an Obsidian vault::

    from elog.logbook_md import export_from_config

    export_from_config('elog.yaml', 89, 'elog_export')   # writes elog_export/0089.md

`Logbook.read` returns ``(message, attributes, attachments)``; those become the note
body (HTML converted with markdownify), the frontmatter, and -- for now -- a plain list
of server URLs. Downloading attachments is not implemented: see ``AttachmentHandler``
for the seam that adds it without changing any signature above it.

The reverse direction, Markdown -> ELOG, is still reserved; see ``MarkdownConverter``
and ``ConvertedMessage`` at the bottom.
"""

import getpass
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from elog.logbook import Logbook
from elog.logbook_exceptions import LogbookError

__all__ = [
    'LogbookConfigError',
    'LogbookConfigFileError',
    'LogbookConfigParseError',
    'LogbookConfigValidationError',
    'LogbookCredentialsError',
    'LogbookExportError',
    'MarkdownFileExistsError',
    'LogbookConfig',
    'load_config',
    'Credentials',
    'CredentialPrompter',
    'build_frontmatter',
    'convert_body',
    'dump_frontmatter',
    'MarkdownDocument',
    'AttachmentHandler',
    'MarkdownExporter',
    'BatchResult',
    'export_to_markdown_file',
    'export_from_config',
    'export_all_from_config',
    'print_progress',
    'LogbookSession',
    'open_from_config',
    'ConvertedMessage',
    'MarkdownConverter',
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LogbookConfigError(LogbookError):
    """Base class for problems with the logbook configuration file."""


class LogbookConfigFileError(LogbookConfigError):
    """The configuration file is missing, is not a file, or cannot be read."""


class LogbookConfigParseError(LogbookConfigError):
    """The configuration file is not well-formed YAML."""


class LogbookConfigValidationError(LogbookConfigError):
    """The configuration parsed correctly but violates the schema."""


class LogbookCredentialsError(LogbookError):
    """The username or password could not be obtained interactively."""


class LogbookExportError(LogbookError):
    """An ELOG entry could not be exported to Markdown."""


class MarkdownFileExistsError(LogbookExportError, FileExistsError):
    """The target Markdown file exists and ``overwrite=False``.

    Inherits from both so that callers catching ``LogbookError`` and callers catching
    the idiomatic ``FileExistsError`` each work.
    """


# ---------------------------------------------------------------------------
# Coercion helpers
#
# Each helper appends a human readable message to ``errors`` instead of raising, so
# that LogbookConfig.from_mapping can report every problem in a config file at once.
# ---------------------------------------------------------------------------

_TRUE_STRINGS = frozenset(('true', 'yes', 'on', '1'))
_FALSE_STRINGS = frozenset(('false', 'no', 'off', '0'))


def _coerce_str(value, key, errors, default=''):
    """Return ``value`` as a stripped string. ``None`` becomes ``default``."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, str):
        errors.append("'{0}' must be a string, got {1!r}".format(key, value))
        return default
    return value.strip()


def _coerce_bool(value, key, errors, default=True):
    """Return ``value`` as a bool, accepting the usual YAML-ish string spellings.

    PyYAML resolves an unquoted ``false`` to a bool, but a quoted ``"false"`` stays a
    string -- and ``bool('false')`` is ``True``, which would silently enable SSL.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    errors.append("'{0}' must be a boolean, got {1!r}".format(key, value))
    return default


def _coerce_port(value, key, errors):
    """Return ``value`` as an ``int`` port number, or ``None`` if unset.

    Logbook compares the port against the ints 80 and 443, so a string port never
    matches and leaks a redundant ':80' into the URL. Booleans are rejected explicitly
    because ``isinstance(True, int)`` is ``True``, and ``port: yes`` in YAML is a bool.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        errors.append("'{0}' must be a port number, got {1!r}".format(key, value))
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            errors.append("'{0}' must be a port number, got {1!r}".format(key, value))
            return None
    if not isinstance(value, int):
        errors.append("'{0}' must be a port number, got {1!r}".format(key, value))
        return None
    if not 1 <= value <= 65535:
        errors.append("'{0}' must be between 1 and 65535, got {1}".format(key, value))
        return None
    return value


def _normalize_hostname(hostname, use_ssl, errors):
    """Ensure ``hostname`` carries an explicit http:// or https:// scheme.

    Without this, ``urlsplit('elog.example.com')`` yields an empty netloc and Logbook
    builds the URL 'https:///<logbook>/', which only fails later with an opaque
    requests error.
    """
    if not hostname:
        errors.append("'hostname' is required")
        return hostname
    if '://' not in hostname:
        return ('https://' if use_ssl else 'http://') + hostname
    scheme = hostname.split('://', 1)[0].lower()
    if scheme not in ('http', 'https'):
        errors.append("'hostname' scheme must be http or https, got {0!r}".format(scheme))
    return hostname


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LogbookConfig:
    """Connection settings for a single ELOG logbook.

    This class performs no I/O: it is a pure function of a mapping, which keeps the
    schema testable without touching the filesystem, the network, or PyYAML.
    """

    hostname: str
    logbook: str = ''
    port: int = None
    subdir: str = ''
    use_ssl: bool = True

    #: Recognised YAML keys. Anything else is rejected by ``from_mapping``.
    FIELDS = ('hostname', 'logbook', 'port', 'subdir', 'use_ssl')

    @classmethod
    def from_mapping(cls, mapping, strict=True):
        """Build a config from a plain dict, coercing and validating every field.

        :param mapping: the parsed YAML document
        :param strict: if True (default) unknown keys raise; otherwise they only warn
        :raises LogbookConfigValidationError: aggregating every problem found
        """
        if mapping is None:
            raise LogbookConfigValidationError(
                'Configuration is empty; at least "hostname" is required.')
        if not isinstance(mapping, dict):
            raise LogbookConfigValidationError(
                'Configuration must be a mapping of keys to values, got {0}.'.format(
                    type(mapping).__name__))

        errors = []

        unknown = sorted(set(mapping) - set(cls.FIELDS))
        if unknown:
            message = 'Unknown configuration key(s): {0}. Valid keys are: {1}.'.format(
                ', '.join(repr(k) for k in unknown), ', '.join(cls.FIELDS))
            if strict:
                errors.append(message)
            else:
                warnings.warn(message, stacklevel=2)

        use_ssl = _coerce_bool(mapping.get('use_ssl'), 'use_ssl', errors, default=True)
        port = _coerce_port(mapping.get('port'), 'port', errors)
        logbook = _coerce_str(mapping.get('logbook'), 'logbook', errors)
        subdir = _coerce_str(mapping.get('subdir'), 'subdir', errors).strip('/')
        hostname = _normalize_hostname(
            _coerce_str(mapping.get('hostname'), 'hostname', errors), use_ssl, errors)

        cls._check_conflicts(hostname, mapping, use_ssl, subdir, port, errors)

        if errors:
            raise LogbookConfigValidationError(
                'Invalid logbook configuration:\n  - ' + '\n  - '.join(errors))

        return cls(hostname=hostname, logbook=logbook, port=port,
                   subdir=subdir, use_ssl=use_ssl)

    @staticmethod
    def _check_conflicts(hostname, mapping, use_ssl, subdir, port, errors):
        """Reject settings that Logbook would silently discard.

        When ``hostname`` is a full URL, Logbook lets the URL win over the ``subdir``,
        ``port`` and ``use_ssl`` arguments without saying so. Failing loudly here beats
        debugging a URL that quietly points somewhere else.
        """
        if '://' not in hostname:
            return

        scheme, _, remainder = hostname.partition('://')
        netloc = remainder.split('/', 1)[0]
        path = remainder[len(netloc):].strip('/')

        if path and subdir:
            errors.append(
                "'hostname' already contains the path {0!r}, so 'subdir' must not be "
                'set as well'.format(path))
        if ':' in netloc and mapping.get('port') is not None:
            errors.append(
                "'hostname' already specifies a port, so 'port' must not be set as well")
        if 'use_ssl' in mapping and scheme.lower() != ('https' if use_ssl else 'http'):
            warnings.warn(
                "'use_ssl' is {0} but 'hostname' uses the {1}:// scheme; the scheme "
                'wins.'.format(use_ssl, scheme.lower()), stacklevel=3)

    def to_logbook_kwargs(self):
        """Return the constructor kwargs for :class:`elog.logbook.Logbook`.

        Credentials are deliberately not included; ``LogbookSession`` adds those.
        """
        return {'hostname': self.hostname,
                'logbook': self.logbook,
                'port': self.port,
                'subdir': self.subdir,
                'use_ssl': self.use_ssl}


def load_config(path, strict=True):
    """Read a YAML configuration file and return a :class:`LogbookConfig`.

    :param path: path to the YAML file
    :param strict: if True (default) unknown keys raise instead of warning
    :raises LogbookConfigFileError: the file is missing or unreadable
    :raises LogbookConfigParseError: the file is not well-formed YAML
    :raises LogbookConfigValidationError: the file violates the schema
    """
    # Imported lazily to match the house style (passlib and lxml are handled the same
    # way) and so the rest of this module stays importable without PyYAML installed.
    try:
        import yaml
    except ImportError as e:
        raise LogbookConfigError(
            'PyYAML is required to read ELOG configuration files '
            '(pip install PyYAML).') from e

    config_path = Path(path)
    try:
        # Path.read_text rather than open(), because elog.open shadows the builtin
        # inside this package.
        text = config_path.read_text(encoding='utf-8')
    except OSError as e:
        raise LogbookConfigFileError(
            'Cannot read logbook configuration file {0}: {1}'.format(config_path, e)) from e

    try:
        # safe_load, never load: a config file must not be able to execute code.
        document = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LogbookConfigParseError(
            'Cannot parse logbook configuration file {0}: {1}'.format(config_path, e)) from e

    try:
        return LogbookConfig.from_mapping(document, strict=strict)
    except LogbookConfigValidationError as e:
        raise LogbookConfigValidationError('{0} (in {1})'.format(e, config_path)) from e


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """An ELOG username plus a plaintext password, with a short intended lifetime.

    ``Logbook.__init__`` hashes the password itself, so it has to be handed over in
    plaintext. CPython cannot zero an immutable str, so what this class offers instead
    is a masking ``__repr__`` -- keeping the password out of tracebacks, log records and
    ``locals()`` dumps -- and ``clear()``, which drops our reference as soon as the
    hand-off has happened.
    """

    user: str
    password: str = None

    def clear(self):
        """Drop the plaintext password once it has been handed to Logbook."""
        self.password = None

    def __repr__(self):
        state = 'set' if self.password else 'cleared'
        return 'Credentials(user={0!r}, password=<{1}>)'.format(self.user, state)


class CredentialPrompter:
    """Asks for an ELOG username and password on the terminal."""

    def __init__(self, user_prompt='ELOG username: ', password_prompt='ELOG password: ',
                 max_attempts=3, require_tty=True, input_fn=None, getpass_fn=None):
        """
        :param max_attempts: how often to re-ask before giving up on empty input
        :param require_tty: fail instead of prompting when stdin is not a terminal
        :param input_fn: override for ``input``, for testing
        :param getpass_fn: override for ``getpass.getpass``, for testing
        """
        self._user_prompt = user_prompt
        self._password_prompt = password_prompt
        self._max_attempts = max_attempts
        self._require_tty = require_tty
        # Resolved here rather than used as default argument values: defaults are bound
        # at def time, so mock.patch('builtins.input') would never be seen.
        self._input = input_fn if input_fn is not None else input
        self._getpass = getpass_fn if getpass_fn is not None else getpass.getpass

    def prompt(self):
        """Return :class:`Credentials` read from the terminal."""
        self._check_tty()
        user = self._read(self._input, self._user_prompt, 'username').strip()
        password = self._read(self._getpass, self._password_prompt, 'password')
        return Credentials(user=user, password=password)

    def _check_tty(self):
        """Refuse to prompt when stdin is not a terminal.

        Without this getpass falls back to reading from a plain stream with echo on,
        which would print the password to the screen and into the scrollback.
        """
        if not self._require_tty:
            return
        isatty = getattr(sys.stdin, 'isatty', None)
        if isatty is None or not isatty():
            raise LogbookCredentialsError(
                'Cannot prompt for ELOG credentials: standard input is not a terminal.')

    def _read(self, reader, prompt, label):
        """Read one non-empty value, re-asking up to ``max_attempts`` times.

        Empty values are never accepted: an empty password would be hashed by
        Logbook into a real digest and sent as a bogus credential rather than
        being treated as absent.
        """
        for remaining in range(self._max_attempts, 0, -1):
            try:
                value = reader(prompt)
            except EOFError as e:
                raise LogbookCredentialsError(
                    'Input stream closed while reading the ELOG {0}.'.format(label)) from e
            # KeyboardInterrupt is deliberately not caught: Ctrl-C must abort.
            if value and value.strip():
                return value
            if remaining > 1:
                print('The ELOG {0} must not be empty.'.format(label), file=sys.stderr)
        raise LogbookCredentialsError(
            'No ELOG {0} provided after {1} attempt(s).'.format(label, self._max_attempts))


# ---------------------------------------------------------------------------
# Inbound: ELOG entry -> Markdown note
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile('[^0-9a-zA-Z]+')
_TAG_WS_RE = re.compile(r'\s+')

#: ELOG attribute name -> (frontmatter key, coercion). Consulted before the generic
#: slug path, so these names never reach _slugify_key.
_FRONTMATTER_MAP = None  # populated below, once the coercions are defined

#: Consumed elsewhere, never emitted. 'Encoding' is the body converter's input;
#: 'Text' and 'Attachment' are diverted by Logbook.read.
_FRONTMATTER_DROP = frozenset(('Encoding', 'Text', 'Attachment'))

#: Emitted in this order; extras follow alphabetically, then 'attachments' last.
_FRONTMATTER_ORDER = ('id', 'date', 'author', 'subject', 'tags')

#: Owned by this module. An ELOG attribute whose slug collides gets an 'elog_' prefix.
_FRONTMATTER_RESERVED = frozenset(_FRONTMATTER_ORDER) | {'attachments'}

#: Comma-separated ID lists that are far more useful as ints (thread reconstruction).
_FRONTMATTER_INT_LISTS = frozenset(('reply_to', 'in_reply_to'))


def _repair_mojibake(text):
    """Undo the latin-1 mis-decode of a UTF-8 ELOG entry.

    ``Logbook.read`` decodes every response with 'iso-8859-1', so an entry authored as
    UTF-8 in the web UI arrives with 'e-acute' as 'A-tilde c-cedilla'. Re-encoding to
    latin-1 and decoding as UTF-8 reverses that exactly; if the text really was latin-1
    the UTF-8 decode fails and the original is returned unchanged.

    Not lossless in theory, which is why the exporter exposes repair_encoding=False.
    """
    if not text or text.isascii():
        # Pure ASCII cannot be mojibake. Fast path, not a correctness requirement.
        return text
    try:
        # Strict, no 'ignore': a character above U+00FF cannot have come from a
        # latin-1 decode, so the error is the right signal to leave the text alone.
        return text.encode('iso-8859-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _slugify_key(key):
    """'Reply to' -> 'reply_to', 'Locked by' -> 'locked_by'.

    Deliberately the same shape as the key sanitising ELOG itself uses on the wire
    (logbook.py:808), plus lowercasing and edge trimming.
    """
    return _SLUG_RE.sub('_', key).strip('_').lower()


def _coerce_msg_id(raw):
    """ELOG's '$@MID@$' header -> an int, or None when it is absent or not numeric."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_elog_date(raw):
    """RFC 2822 date -> ISO 8601 string, e.g. '2026-08-28T17:28:38+02:00'.

    Returns a *string*, not a datetime: PyYAML renders a datetime as
    '2026-08-28 17:28:38+02:00' (space separated, unquoted), which Obsidian's date
    property parser rejects. A quoted ISO string is accepted everywhere.

    Unparseable input comes back verbatim with a warning, so a single odd date can
    never abort a bulk export.
    """
    from email.utils import parsedate_to_datetime

    text = (raw or '').strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        # ValueError on 3.10+, TypeError on 3.9 and earlier.
        warnings.warn('Cannot parse ELOG date {0!r}; keeping it verbatim.'.format(text),
                      stacklevel=2)
        return text


def _obsidian_tags(raw):
    """ELOG 'Type' -> a list of Obsidian-safe tags.

    'Hardware | Calibration'         -> ['Hardware', 'Calibration']
    'Software Installation | Setup'  -> ['Software-Installation', 'Setup']

    Obsidian tags cannot contain whitespace, so every internal whitespace run collapses
    to a single '-'. A leading '#' is stripped because Obsidian adds it itself.
    """
    tags = []
    for segment in (raw or '').split('|'):
        tag = _TAG_WS_RE.sub('-', segment.strip()).lstrip('#').strip('-')
        if tag:
            tags.append(tag)
    # dict.fromkeys deduplicates while preserving first-seen order; a repeated segment
    # would otherwise show twice in the property panel.
    return list(dict.fromkeys(tags)) or None


def _coerce_int_list(raw):
    """'12,34' -> [12, 34]. Non-numeric entries are kept as strings."""
    values = []
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            values.append(item)
    return values or None


_FRONTMATTER_MAP = {
    '$@MID@$': ('id', _coerce_msg_id),
    'Date': ('date', _parse_elog_date),
    'Author': ('author', None),
    'Subject': ('subject', None),
    'Type': ('tags', _obsidian_tags),
}


def build_frontmatter(attributes, attachments=(), repair=True):
    """Normalise ELOG attributes into Obsidian-friendly frontmatter.

    Known attributes are renamed and coerced (see ``_FRONTMATTER_MAP``); notably
    'Type' becomes a list under 'tags'. Every other attribute is kept under a
    slugified key rather than dropped, so the export stays lossless for logbooks with
    site-specific attributes. Empty values are omitted.

    :param attributes: the dict returned by ``Logbook.read`` (all values are strings)
    :param attachments: the attachment list to record, already in its final form
    :param repair: undo latin-1/UTF-8 mojibake in the values
    :return: an insertion-ordered dict ready for :func:`dump_frontmatter`
    """
    known = {}
    extras = {}

    for key, value in (attributes or {}).items():
        if key in _FRONTMATTER_DROP:
            continue
        if repair and isinstance(value, str):
            value = _repair_mojibake(value)

        mapping = _FRONTMATTER_MAP.get(key)
        if mapping is not None:
            name, coercion = mapping
            known[name] = coercion(value) if coercion is not None else value
            continue

        slug = _slugify_key(key)
        if not slug:
            continue
        if slug in _FRONTMATTER_INT_LISTS:
            value = _coerce_int_list(value)
        if slug in _FRONTMATTER_RESERVED:
            # Never shadow a key this module owns; surface the clash instead.
            warnings.warn(
                'ELOG attribute {0!r} collides with the reserved frontmatter key '
                '{1!r}; emitting it as {2!r}.'.format(key, slug, 'elog_' + slug),
                stacklevel=2)
            slug = 'elog_' + slug
        if slug in extras:
            warnings.warn(
                'ELOG attributes {0!r} and an earlier one both map to {1!r}; '
                'keeping the first.'.format(key, slug), stacklevel=2)
            continue
        extras[slug] = value

    frontmatter = {}
    for name in _FRONTMATTER_ORDER:
        value = known.get(name)
        # Drop blanks: ELOG sends every configured attribute even when empty, and a
        # panel full of nulls makes every note look half-filled.
        if value or value == 0:
            frontmatter[name] = value
    for slug in sorted(extras):
        if extras[slug]:
            frontmatter[slug] = extras[slug]
    frontmatter['attachments'] = list(attachments)
    return frontmatter


_DANGEROUS_RE = re.compile(r'<(script|style)\b[^>]*>.*?</\1\s*>', re.IGNORECASE | re.DOTALL)

#: Chosen for Obsidian- and git-friendly output. Every value is passed explicitly so a
#: markdownify default change cannot silently alter exported notes.
_MARKDOWNIFY_OPTIONS = {
    'heading_style': 'ATX',        # '# H1'. The default 'underlined' cannot express h3+,
                                   # and ATX is what Obsidian writes, so a re-save is a
                                   # no-op diff.
    'bullets': '-',                # One char, so every nesting level uses '-'. The
                                   # default '*+-' alternates per level and churns diffs.
    'strong_em_symbol': '*',       # '**bold**' / '*italic*'; '_' collides with snake_case.
    'newline_style': 'BACKSLASH',  # <br> -> trailing '\'. The default two trailing spaces
                                   # are invisible and get stripped by editors and linters.
    'autolinks': False,            # <a href="x">x</a> -> '[x](x)', not '<x>'. Bare
                                   # autolinks with query strings break some renderers.
    'escape_asterisks': True,
    'escape_underscores': True,
    'escape_misc': True,           # Escapes a leading '#', '-', '1.' etc. Without it a
                                   # paragraph starting '- 5 V' becomes a list item.
    'wrap': False,                 # Never hard-wrap: one paragraph per line keeps git
                                   # diffs word-level instead of reflowing whole blocks.
    'code_language': '',           # Do not guess a language for <pre><code>.
}


def _html_to_markdown(text):
    """Convert an ELOG HTML body to Markdown."""
    # Lazy, matching lxml in Logbook.search and passlib in _handle_pswd. Imported as a
    # module and never `from markdownify import ...`: markdownify exports a class also
    # named MarkdownConverter, which would shadow this module's outbound seam.
    try:
        import markdownify
    except ImportError as e:
        raise LogbookExportError(
            'markdownify is required to convert HTML ELOG entries to Markdown '
            '(pip install markdownify).') from e

    # markdownify's strip= removes only the tag and keeps its text, so a <script> body
    # would land in the note as prose. Drop those elements outright first.
    text = _DANGEROUS_RE.sub('', text)
    return markdownify.markdownify(text, **_MARKDOWNIFY_OPTIONS)


def _plain_to_markdown(text):
    """Plain-text ELOG bodies are emitted verbatim.

    Deliberately no escaping: in practice these entries are already written
    Markdown-ish, and escaping every '*' and '#' would make them worse to read.
    """
    return '\n'.join(line.rstrip() for line in text.splitlines())


_ELCODE_CODE_RE = re.compile(r'\[code\](.*?)\[/code\]', re.IGNORECASE | re.DOTALL)
_ELCODE_QUOTE_RE = re.compile(r'\[quote\](.*?)\[/quote\]', re.IGNORECASE | re.DOTALL)
_ELCODE_INLINE = (
    (re.compile(r'\[b\](.*?)\[/b\]', re.IGNORECASE | re.DOTALL), r'**\1**'),
    (re.compile(r'\[i\](.*?)\[/i\]', re.IGNORECASE | re.DOTALL), r'*\1*'),
    (re.compile(r'\[s\](.*?)\[/s\]', re.IGNORECASE | re.DOTALL), r'~~\1~~'),
    # Markdown has no underline; keep the HTML tag rather than losing the emphasis.
    (re.compile(r'\[u\](.*?)\[/u\]', re.IGNORECASE | re.DOTALL), r'<u>\1</u>'),
    (re.compile(r'\[url=(.*?)\](.*?)\[/url\]', re.IGNORECASE | re.DOTALL), r'[\2](\1)'),
    (re.compile(r'\[url\](.*?)\[/url\]', re.IGNORECASE | re.DOTALL), r'<\1>'),
    (re.compile(r'\[img\](.*?)\[/img\]', re.IGNORECASE | re.DOTALL), r'![](\1)'),
    # Purely presentational: drop the tag, keep the content.
    (re.compile(r'\[/?(?:size|color|font|center)(?:=[^\]]*)?\]', re.IGNORECASE), ''),
)


def _elcode_to_markdown(text):
    """Best-effort ELCode (BBCode-like) -> Markdown.

    Unrecognised or unbalanced tags are left verbatim rather than mangled: a note with
    a stray '[foo]' is recoverable, a note with half a sentence eaten is not.
    Nested lists are flattened to a single level.
    """
    # Code blocks first, so the inline rules below cannot rewrite their contents.
    def _fence(match):
        return '\n```\n{0}\n```\n'.format(match.group(1).strip('\n'))

    text = _ELCODE_CODE_RE.sub(_fence, text)

    def _blockquote(match):
        lines = match.group(1).strip('\n').splitlines()
        return '\n' + '\n'.join('> ' + line for line in lines) + '\n'

    text = _ELCODE_QUOTE_RE.sub(_blockquote, text)

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r'\[/?list(?:=[^\]]*)?\]', stripped, re.IGNORECASE):
            # List containers carry no content of their own.
            continue
        lines.append(re.sub(r'^\s*\[\*\]\s*', '- ', line))
    text = '\n'.join(lines)

    for pattern, replacement in _ELCODE_INLINE:
        text = pattern.sub(replacement, text)
    return text


_BODY_CONVERTERS = {
    'HTML': _html_to_markdown,
    'plain': _plain_to_markdown,
    'ELCode': _elcode_to_markdown,
}

_HTML_SNIFF_RE = re.compile(
    r'<\s*(p|br|div|table|ul|ol|h[1-6]|b|i|a|img|span|font)\b', re.IGNORECASE)
_ELCODE_SNIFF_RE = re.compile(
    r'\[/?(b|i|u|s|url|img|list|code|quote|size|color)\b', re.IGNORECASE)


def _sniff_encoding(text):
    """Guess the body encoding when the 'Encoding' attribute is absent or unknown.

    Older ELOG installations omit it. Guessing beats the alternatives: defaulting to
    HTML would leak raw markup into a plain entry, defaulting to plain would dump a
    wall of tags into the note.
    """
    if _HTML_SNIFF_RE.search(text):
        return 'HTML'
    if _ELCODE_SNIFF_RE.search(text):
        return 'ELCode'
    return 'plain'


def convert_body(message, encoding=None, repair=True):
    """Convert one ELOG message body to Markdown.

    :param message: the message string returned by ``Logbook.read``
    :param encoding: ``attributes.get('Encoding')`` -- 'HTML', 'plain' or 'ELCode'.
                     Missing or unrecognised values fall back to content sniffing.
                     Matching is case sensitive, as it is on the wire.
    :param repair: undo latin-1/UTF-8 mojibake, see :func:`_repair_mojibake`
    """
    text = _repair_mojibake(message or '') if repair else (message or '')
    handler = _BODY_CONVERTERS.get(encoding)
    if handler is None:
        sniffed = _sniff_encoding(text)
        warnings.warn(
            'ELOG entry has {0} Encoding attribute; treating the body as {1}.'.format(
                'no' if encoding is None else 'unknown {0!r}'.format(encoding), sniffed),
            stacklevel=2)
        handler = _BODY_CONVERTERS[sniffed]
    return handler(text).strip('\n')


def dump_frontmatter(frontmatter):
    """Render a frontmatter dict as a fenced YAML block, without a trailing newline."""
    try:
        import yaml
    except ImportError as e:
        raise LogbookExportError(
            'PyYAML is required to write Markdown frontmatter '
            '(pip install PyYAML).') from e

    class _IndentedDumper(yaml.SafeDumper):
        """Indent block sequences under their key.

        PyYAML puts '- Hardware' flush against the key by default. Obsidian parses
        both but writes the indented form itself, so matching it keeps a note Obsidian
        has re-saved byte-identical to a freshly exported one.
        """

        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow=flow, indentless=False)

    # A SafeDumper subclass, so this keeps safe_dump's guarantees while raising
    # RepresenterError on anything build_frontmatter should never have produced.
    body = yaml.dump(
        frontmatter,
        Dumper=_IndentedDumper,
        sort_keys=False,           # build_frontmatter already chose the order
        allow_unicode=True,        # a real 'a-umlaut', not an escape
        default_flow_style=False,  # block style: 'tags:\n  - A', not 'tags: [A]'
        width=4096,                # effectively unlimited; folding a long subject
                                   # across lines confuses some frontmatter parsers,
                                   # Obsidian's included
    )
    return '---\n{0}---'.format(body)


@dataclass(frozen=True)
class MarkdownDocument:
    """A YAML-frontmatter Markdown note derived from one ELOG entry.

    Frozen and free of I/O: the exporter builds it and either writes it or hands it
    back. Keeping it separate from the writer is what lets the whole conversion be
    tested without a filesystem.
    """

    frontmatter: dict
    body: str

    @property
    def msg_id(self):
        """The ELOG message ID, or None when the entry carried no '$@MID@$'."""
        return self.frontmatter.get('id')

    def to_text(self):
        """Render the note: fenced YAML, one blank line, then the body."""
        return '{0}\n\n{1}\n'.format(dump_frontmatter(self.frontmatter),
                                     self.body.rstrip('\n'))


class AttachmentHandler:
    """Decides what happens to an entry's attachments. Link-only stub.

    The default implementation performs no I/O: it records the server URLs in the
    frontmatter and leaves the body alone. That keeps the export offline-safe and
    preserves the join key between the two -- the timestamped filename in an
    ``![](260828_172838_plot.png)`` link is exactly ``os.path.basename()`` of the
    corresponding URL, so a downloading subclass can rewrite the links
    deterministically.

    To add downloading later, subclass and override ``process``. Nothing above this
    class changes signature::

        class DownloadingAttachmentHandler(AttachmentHandler):
            def process(self, body, attachments, out_dir=None, stem=None, logbook=None):
                target = Path(out_dir) / 'attachments'
                target.mkdir(parents=True, exist_ok=True)
                values = []
                for url in attachments:
                    timestamped = os.path.basename(url)
                    name = timestamped[14:] or timestamped  # strip '<YYMMDD>_<HHMMSS>_'
                    (target / name).write_bytes(logbook.download_attachment(url))
                    body = body.replace('({0})'.format(timestamped),
                                        '(attachments/{0})'.format(name))
                    values.append('attachments/{0}'.format(name))
                return body, values
    """

    def process(self, body, attachments, out_dir=None, stem=None, logbook=None):
        """Return ``(body, frontmatter_value)`` for one entry's attachments.

        One call returning both, rather than two methods, because downloading has to
        change the body *and* the frontmatter and the two must agree on the same
        filenames.

        :param body: the already-converted Markdown body
        :param attachments: absolute URLs as returned by ``Logbook.read``
        :param out_dir: destination directory, or None when exporting in memory
        :param stem: the note's filename stem, e.g. '0089'
        :param logbook: the ``Logbook``, for ``download_attachment``
        """
        return body, list(attachments)


def _stem(msg_id):
    """89 or '89' -> '0089'. Zero-padded so notes sort correctly in a file browser.

    The padding is a minimum, so a five-digit ID is unaffected.
    """
    try:
        return '{0:04d}'.format(int(msg_id))
    except (TypeError, ValueError) as e:
        raise LogbookExportError('Invalid ELOG message ID {0!r}.'.format(msg_id)) from e


@dataclass
class BatchResult:
    """What a batch export did, entry by entry.

    Returned rather than printed so a caller can act on it -- retry the failures, assert
    in a test, or write a report. ``failed`` keeps the exception itself, not a string,
    so nothing is lost.
    """

    exported: list = field(default_factory=list)   # [(msg_id, Path)]
    skipped: list = field(default_factory=list)    # [msg_id] -- already on disk
    failed: list = field(default_factory=list)     # [(msg_id, exception)]

    @property
    def total(self):
        """How many entries were considered."""
        return len(self.exported) + len(self.skipped) + len(self.failed)

    def summary(self):
        """A one-line human-readable tally."""
        return '{0} exported, {1} skipped, {2} failed (of {3})'.format(
            len(self.exported), len(self.skipped), len(self.failed), self.total)

    def __bool__(self):
        """True when nothing failed."""
        return not self.failed


class MarkdownExporter:
    """Turns ELOG entries into Markdown notes.

    Composition rather than subclassing: it holds a ``Logbook``, so the whole
    conversion can be tested against a small fake.
    """

    def __init__(self, logbook, attachments=None, repair_encoding=True, timeout=None):
        """
        :param logbook: an ``elog.logbook.Logbook``, or anything with ``read`` and
                        ``download_attachment``
        :param attachments: an :class:`AttachmentHandler`; the link-only stub by default
        :param repair_encoding: undo latin-1/UTF-8 mojibake, see :func:`_repair_mojibake`
        :param timeout: request timeout in seconds, passed to ``Logbook.read``
        """
        self.logbook = logbook
        self.attachments = attachments if attachments is not None else AttachmentHandler()
        self.repair_encoding = repair_encoding
        self.timeout = timeout

    def to_markdown(self, msg_id, out_dir=None):
        """Read one entry and return it as a :class:`MarkdownDocument`. No file I/O.

        ``out_dir`` is unused by the default attachment handler; it is threaded through
        so a downloading handler can save files beside the note without a signature
        change.

        Note that ``Logbook.read`` raises a bare ``ValueError`` when the server
        response carries no '=' delimiter line, which happens for truncated or error
        responses; that is not translated here.
        """
        message, attributes, attachments = self.logbook.read(msg_id, timeout=self.timeout)

        body = convert_body(message, attributes.get('Encoding'),
                            repair=self.repair_encoding)
        body, attachment_values = self.attachments.process(
            body, attachments, out_dir=out_dir, stem=_stem(msg_id), logbook=self.logbook)
        frontmatter = build_frontmatter(attributes, attachment_values,
                                        repair=self.repair_encoding)
        return MarkdownDocument(frontmatter=frontmatter, body=body)

    def to_markdown_file(self, msg_id, out_dir, overwrite=True):
        """Export one entry to ``<out_dir>/<msg_id:04d>.md`` and return the Path.

        :param out_dir: destination directory, created with parents if missing. There
                        is no default: the library never picks a location for you.
        :param overwrite: replace an existing note. True by default because re-running
                          a sync must refresh; False raises
                          :class:`MarkdownFileExistsError`.
        """
        out_dir = Path(out_dir)
        # Before the network read, so a bad path fails fast and cheaply.
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / '{0}.md'.format(_stem(msg_id))
        if target.exists() and not overwrite:
            raise MarkdownFileExistsError(
                'Refusing to overwrite existing Markdown file {0}.'.format(target))

        document = self.to_markdown(msg_id, out_dir=out_dir)

        # Write to a sibling temp file and rename: Path.replace is atomic within a
        # filesystem, so an interrupted export cannot leave a truncated note behind in
        # a vault that something else is syncing.
        temp = target.with_suffix('.md.tmp')
        # Path.write_text, not open(): elog.open shadows the builtin in this package.
        # newline='\n' stops Windows from turning every '\n' into '\r\n', which would
        # make every note differ from a Linux-generated one.
        temp.write_text(document.to_text(), encoding='utf-8', newline='\n')
        temp.replace(target)
        return target


    def list_message_ids(self, page_size=1000000):
        """Return every message ID in the logbook, ascending.

        Uses ``search('')`` rather than ``get_message_ids()``: search passes ``npp``
        (entries per page) to the server, while ``get_message_ids`` requests
        ``<url>page`` with no page size and so returns only as many entries as the
        logbook's own "entries per page" setting allows -- silently truncating the
        batch on a large logbook.

        Deleted entries simply never appear in the listing, so every ID returned is
        valid and no probing for gaps is needed.

        :param page_size: the ``npp`` value; the default is high enough to mean
                          "everything" for any realistic logbook
        """
        return sorted(self.logbook.search('', n_results=page_size,
                                          timeout=self.timeout))

    def export_all(self, out_dir, msg_ids=None, overwrite=True, skip_existing=False,
                   stop_on_error=False, progress=None):
        """Export many entries, one at a time, into ``out_dir``.

        The credentials were already resolved when the ``Logbook`` was built, so a
        whole batch prompts at most once regardless of size.

        :param out_dir: destination directory, created if missing
        :param msg_ids: which entries to export; every entry in the logbook by default
        :param overwrite: replace notes that already exist (see ``skip_existing``)
        :param skip_existing: leave entries that already have a ``.md`` file alone.
                              Makes a re-run cheap and resumable, at the cost of not
                              picking up edits made on the server since.
        :param stop_on_error: re-raise the first failure instead of recording it. Off
                              by default: one unreadable entry must not cost you the
                              other 899.
        :param progress: optional callable ``(done, total, msg_id, status)`` where
                         status is 'exported', 'skipped' or 'failed'
        :return: a :class:`BatchResult`
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if msg_ids is None:
            msg_ids = self.list_message_ids()
        msg_ids = list(msg_ids)

        result = BatchResult()
        for done, msg_id in enumerate(msg_ids, start=1):
            try:
                target = out_dir / '{0}.md'.format(_stem(msg_id))
                if skip_existing and target.exists():
                    result.skipped.append(msg_id)
                    status = 'skipped'
                else:
                    target = self.to_markdown_file(msg_id, out_dir, overwrite=overwrite)
                    result.exported.append((msg_id, target))
                    status = 'exported'
            except (LogbookError, ValueError) as e:
                # LogbookError covers the library's own failures; ValueError covers
                # Logbook.read raising bare when a response carries no '=' delimiter,
                # which is what a truncated or error page looks like.
                if stop_on_error:
                    raise
                warnings.warn('Skipping ELOG entry {0}: {1}'.format(msg_id, e),
                              stacklevel=2)
                result.failed.append((msg_id, e))
                status = 'failed'

            if progress is not None:
                progress(done, len(msg_ids), msg_id, status)
        return result


def export_to_markdown_file(logbook, msg_id, out_dir, **kwargs):
    """Export one entry against an existing ``Logbook``::

        export_to_markdown_file(elog.open(url), 89, 'vault/elog')
    """
    return MarkdownExporter(logbook, **kwargs).to_markdown_file(msg_id, out_dir)


# ---------------------------------------------------------------------------
# Session / factory
# ---------------------------------------------------------------------------

class LogbookSession:
    """Binds a :class:`LogbookConfig` to credentials and produces a ``Logbook``.

    This is the only class here that knows ``Logbook`` exists; everything above it is a
    pure data or terminal-I/O concern.
    """

    def __init__(self, config, credentials=None, prompter=None, converter=None,
                 attachments=None, timeout=None):
        """
        :param config: a :class:`LogbookConfig`
        :param credentials: pre-supplied credentials; prompted for when omitted
        :param prompter: a :class:`CredentialPrompter`; a default one is made if omitted
        :param converter: reserved for the future outbound Markdown converter, see
                          :class:`MarkdownConverter`
        :param attachments: an :class:`AttachmentHandler` for exports; the link-only
                            stub by default
        :param timeout: request timeout in seconds, used by ``verify`` and by exports
        """
        self.config = config
        self.credentials = credentials
        self._prompter = prompter if prompter is not None else CredentialPrompter()
        self._converter = converter
        self._attachments = attachments
        self._timeout = timeout
        self._logbook = None
        self._exporter = None

    @classmethod
    def from_config_file(cls, path, strict=True, **kwargs):
        """Build a session from a YAML config file."""
        return cls(load_config(path, strict=strict), **kwargs)

    @property
    def logbook(self):
        """The ``Logbook``, connecting (and prompting) on first access."""
        if self._logbook is None:
            self.connect()
        return self._logbook

    def connect(self, verify=False):
        """Create and return the ``Logbook``, prompting for credentials if needed.

        :param verify: issue one request to confirm the server accepts the credentials.
                       Off by default so that building a Logbook stays free of I/O.
        """
        if self.credentials is None:
            self.credentials = self._prompter.prompt()
        try:
            self._logbook = Logbook(
                user=self.credentials.user,
                password=self.credentials.password,
                # Explicit even though it is the default: the contract of this layer is
                # that we hand over plaintext and Logbook does the hashing.
                encrypt_pwd=True,
                **self.config.to_logbook_kwargs())
        finally:
            self.credentials.clear()

        if verify:
            # get_message_ids rather than get_last_message_id: the latter returns None
            # for an empty logbook, which is indistinguishable from a failure.
            self._logbook.get_message_ids(timeout=self._timeout)
        return self._logbook

    def connect_interactive(self, attempts=3):
        """Like ``connect(verify=True)``, re-prompting when authentication fails."""
        # Imported here because __init__.py does not re-export every exception name.
        from elog.logbook_exceptions import LogbookAuthenticationError

        for remaining in range(attempts, 0, -1):
            try:
                return self.connect(verify=True)
            except LogbookAuthenticationError:
                self._logbook = None
                self.credentials = None
                if remaining == 1:
                    raise
                print('Authentication failed, please try again.', file=sys.stderr)

    @property
    def exporter(self):
        """The :class:`MarkdownExporter`, built (and connecting) on first access."""
        if self._exporter is None:
            self._exporter = MarkdownExporter(
                self.logbook, attachments=self._attachments, timeout=self._timeout)
        return self._exporter

    def to_markdown(self, msg_id, out_dir=None):
        """Read one ELOG entry and return it as a :class:`MarkdownDocument`."""
        return self.exporter.to_markdown(msg_id, out_dir=out_dir)

    def to_markdown_file(self, msg_id, out_dir, overwrite=True):
        """Export one ELOG entry to ``<out_dir>/<msg_id:04d>.md``."""
        return self.exporter.to_markdown_file(msg_id, out_dir, overwrite=overwrite)

    def list_message_ids(self, page_size=1000000):
        """Return every message ID in the logbook, ascending."""
        return self.exporter.list_message_ids(page_size=page_size)

    def export_all(self, out_dir, **kwargs):
        """Export every ELOG entry to ``out_dir``, prompting for credentials once.

        See :meth:`MarkdownExporter.export_all` for the parameters.
        """
        return self.exporter.export_all(out_dir, **kwargs)

    def post_markdown(self, markdown_text, **kwargs):
        """Convert Markdown and post it. Not implemented yet.

        The seam is reserved so that adding a converter later is not a signature
        change; see :class:`MarkdownConverter`.
        """
        raise NotImplementedError(
            'Markdown conversion is not implemented yet. Pass a converter to '
            'LogbookSession(converter=...) once one exists.')


def open_from_config(path, strict=True, verify=False, **kwargs):
    """Return a ``Logbook`` built from a YAML config file, prompting for credentials.

    Mirrors the existing :func:`elog.open` idiom::

        logbook = open_from_config('elog.yaml')
    """
    return LogbookSession.from_config_file(path, strict=strict, **kwargs).connect(
        verify=verify)


def export_from_config(path, msg_id, out_dir, strict=True, overwrite=True, **kwargs):
    """Export one entry to Markdown from a YAML config file, prompting for credentials::

        export_from_config('elog.yaml', 89, 'elog_export')
    """
    session = LogbookSession.from_config_file(path, strict=strict, **kwargs)
    return session.to_markdown_file(msg_id, out_dir, overwrite=overwrite)


def export_all_from_config(path, out_dir, strict=True, progress=None, **kwargs):
    """Export every entry to Markdown from a YAML config file, prompting once::

        result = export_all_from_config('elog.yaml', 'elog_export')
        print(result.summary())

    Pass ``progress=print_progress`` for a running count on the terminal.
    """
    session_kwargs = {k: kwargs.pop(k) for k in
                      ('credentials', 'prompter', 'attachments', 'timeout')
                      if k in kwargs}
    session = LogbookSession.from_config_file(path, strict=strict, **session_kwargs)
    return session.export_all(out_dir, progress=progress, **kwargs)


def print_progress(done, total, msg_id, status):
    """A ready-made ``progress`` callback that overwrites one terminal line.

    Written to stderr so that piping an export's stdout somewhere stays clean.
    """
    end = '\n' if done == total else ''
    print('\r  [{0}/{1}] {2} {3}    '.format(done, total, status, msg_id),
          end=end, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Markdown seam -- reserved, not implemented
# ---------------------------------------------------------------------------

@dataclass
class ConvertedMessage:
    """The result of converting Markdown into something ``Logbook.post`` accepts.

    A dataclass rather than a (body, encoding) tuple because Markdown image links will
    eventually have to become ELOG attachments: adding a field is additive, widening a
    tuple would break every caller.
    """

    body: str
    #: One of 'plain', 'HTML' or 'ELCode' -- case sensitive, see Logbook.post.
    encoding: str = 'ELCode'
    attachments: list = field(default_factory=list)


class MarkdownConverter:
    """Interface for turning Markdown into a :class:`ConvertedMessage`.

    Implement ``convert`` and pass an instance as ``LogbookSession(converter=...)``.
    """

    def convert(self, markdown_text):
        raise NotImplementedError
