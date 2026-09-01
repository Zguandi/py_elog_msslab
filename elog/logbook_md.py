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

Markdown conversion (the eventual purpose of this module) is not implemented yet; see
``MarkdownConverter`` and ``ConvertedMessage`` at the bottom for the reserved seam.
"""

import getpass
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
    'LogbookConfig',
    'load_config',
    'Credentials',
    'CredentialPrompter',
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
# Session / factory
# ---------------------------------------------------------------------------

class LogbookSession:
    """Binds a :class:`LogbookConfig` to credentials and produces a ``Logbook``.

    This is the only class here that knows ``Logbook`` exists; everything above it is a
    pure data or terminal-I/O concern.
    """

    def __init__(self, config, credentials=None, prompter=None, converter=None,
                 timeout=None):
        """
        :param config: a :class:`LogbookConfig`
        :param credentials: pre-supplied credentials; prompted for when omitted
        :param prompter: a :class:`CredentialPrompter`; a default one is made if omitted
        :param converter: reserved for the future Markdown converter, see
                          :class:`MarkdownConverter`
        :param timeout: request timeout in seconds, used only by ``verify``
        """
        self.config = config
        self.credentials = credentials
        self._prompter = prompter if prompter is not None else CredentialPrompter()
        self._converter = converter
        self._timeout = timeout
        self._logbook = None

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
