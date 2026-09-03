"""Unit tests for elog.logbook_md.

Unlike test_logbook.py these tests never touch the network: Logbook is either mocked
out or exercised only for its URL building, which performs no I/O. Several tests assert
that elog.logbook.requests was never called, as a hard safety net.
"""

import os
import tempfile
import unittest
from unittest import mock

from elog.logbook_md import (
    Credentials,
    CredentialPrompter,
    LogbookConfig,
    LogbookConfigFileError,
    LogbookConfigParseError,
    LogbookConfigValidationError,
    LogbookCredentialsError,
    LogbookSession,
    load_config,
)
from elog.logbook_exceptions import LogbookAuthenticationError, LogbookError


def _prompter(user='alice', password='secret', **kwargs):
    """A prompter that answers without touching a terminal."""
    kwargs.setdefault('require_tty', False)
    return CredentialPrompter(input_fn=lambda _: user,
                              getpass_fn=lambda _: password,
                              **kwargs)


class ConfigFileTestCase(unittest.TestCase):
    """Base class providing a scratch directory and a write helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'elog.yaml')

    def write(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)
        return self.path


class TestLoadConfig(ConfigFileTestCase):

    def test_minimal_config_uses_documented_defaults(self):
        config = load_config(self.write('hostname: https://elog.example.com\n'))
        self.assertEqual(config.hostname, 'https://elog.example.com')
        self.assertEqual(config.logbook, '')
        self.assertIsNone(config.port)
        self.assertEqual(config.subdir, '')
        self.assertIs(config.use_ssl, True)

    def test_full_config_round_trip(self):
        config = load_config(self.write(
            'hostname: http://elog.example.com\n'
            'logbook: Demo\n'
            'subdir: elogs\n'
            'port: 8080\n'
            'use_ssl: false\n'))
        self.assertEqual(config.to_logbook_kwargs(),
                         {'hostname': 'http://elog.example.com', 'logbook': 'Demo',
                          'port': 8080, 'subdir': 'elogs', 'use_ssl': False})

    def test_missing_file(self):
        missing = os.path.join(self._tmp.name, 'nope.yaml')
        self.assertRaises(LogbookConfigFileError, load_config, missing)

    def test_path_is_a_directory(self):
        self.assertRaises(LogbookConfigFileError, load_config, self._tmp.name)

    def test_malformed_yaml(self):
        self.assertRaises(LogbookConfigParseError, load_config,
                          self.write('hostname: [unclosed\n'))

    def test_empty_file(self):
        self.assertRaises(LogbookConfigValidationError, load_config, self.write(''))

    def test_top_level_is_a_list(self):
        self.assertRaises(LogbookConfigValidationError, load_config,
                          self.write('- a\n- b\n'))

    def test_missing_hostname_names_the_key(self):
        with self.assertRaises(LogbookConfigValidationError) as ctx:
            load_config(self.write('logbook: Demo\n'))
        self.assertIn('hostname', str(ctx.exception))

    def test_unknown_key_names_the_key(self):
        with self.assertRaises(LogbookConfigValidationError) as ctx:
            load_config(self.write('hostname: https://h\nuse-ssl: true\n'))
        self.assertIn('use-ssl', str(ctx.exception))

    def test_unknown_key_only_warns_when_not_strict(self):
        with self.assertWarns(UserWarning):
            config = load_config(self.write('hostname: https://h\nuse-ssl: true\n'),
                                 strict=False)
        self.assertEqual(config.hostname, 'https://h')

    def test_config_errors_derive_from_logbook_error(self):
        # Callers already catching LogbookError keep working.
        self.assertRaises(LogbookError, load_config, self.write(''))


class TestLogbookConfigCoercion(unittest.TestCase):
    """Exercises from_mapping directly: no file I/O, no PyYAML."""

    def test_string_port_becomes_int(self):
        # A str port never matches Logbook's `port == 80` check and leaks ':80'.
        config = LogbookConfig.from_mapping({'hostname': 'https://h', 'port': '8080'})
        self.assertEqual(config.port, 8080)
        self.assertIsInstance(config.port, int)

    def test_bool_port_is_rejected(self):
        # isinstance(True, int) is True, and `port: yes` in YAML is a bool.
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'https://h', 'port': True})

    def test_out_of_range_ports_are_rejected(self):
        for port in (0, 70000, -1):
            with self.subTest(port=port):
                self.assertRaises(LogbookConfigValidationError,
                                  LogbookConfig.from_mapping,
                                  {'hostname': 'https://h', 'port': port})

    def test_non_numeric_port_is_rejected(self):
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'https://h', 'port': 'eighty'})

    def test_falsey_use_ssl_strings(self):
        for value in ('false', 'False', 'no', 'off', '0'):
            with self.subTest(value=value):
                config = LogbookConfig.from_mapping(
                    {'hostname': 'http://h', 'use_ssl': value})
                self.assertIs(config.use_ssl, False)

    def test_truthy_use_ssl_strings(self):
        for value in ('true', 'yes', 'on', '1'):
            with self.subTest(value=value):
                config = LogbookConfig.from_mapping(
                    {'hostname': 'https://h', 'use_ssl': value})
                self.assertIs(config.use_ssl, True)

    def test_nonsense_use_ssl_is_rejected(self):
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'https://h', 'use_ssl': 'maybe'})

    def test_bare_hostname_gets_https_scheme(self):
        config = LogbookConfig.from_mapping({'hostname': 'elog.example.com'})
        self.assertEqual(config.hostname, 'https://elog.example.com')

    def test_bare_hostname_gets_http_scheme_when_ssl_disabled(self):
        config = LogbookConfig.from_mapping(
            {'hostname': 'elog.example.com', 'use_ssl': False})
        self.assertEqual(config.hostname, 'http://elog.example.com')

    def test_unsupported_scheme_is_rejected(self):
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'ftp://elog.example.com'})

    def test_path_in_hostname_conflicts_with_subdir(self):
        # Logbook would silently treat the URL path as the subdir and drop ours.
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'https://h/elogs', 'subdir': 'x'})

    def test_port_in_hostname_conflicts_with_port(self):
        # Verified: the URL port wins and the argument is silently ignored.
        self.assertRaises(LogbookConfigValidationError, LogbookConfig.from_mapping,
                          {'hostname': 'https://h:8080', 'port': 1234})

    def test_scheme_contradicting_explicit_use_ssl_only_warns(self):
        with self.assertWarns(UserWarning):
            config = LogbookConfig.from_mapping(
                {'hostname': 'http://h', 'use_ssl': True})
        self.assertEqual(config.hostname, 'http://h')

    def test_defaulted_use_ssl_against_http_does_not_warn(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            config = LogbookConfig.from_mapping({'hostname': 'http://h'})
        self.assertEqual(config.hostname, 'http://h')

    def test_subdir_slashes_are_stripped(self):
        config = LogbookConfig.from_mapping({'hostname': 'https://h', 'subdir': '/elogs/'})
        self.assertEqual(config.subdir, 'elogs')

    def test_null_values_fall_back_to_defaults(self):
        config = LogbookConfig.from_mapping(
            {'hostname': 'https://h', 'logbook': None, 'subdir': None, 'port': None})
        self.assertEqual(config.logbook, '')
        self.assertEqual(config.subdir, '')
        self.assertIsNone(config.port)

    def test_errors_are_aggregated(self):
        with self.assertRaises(LogbookConfigValidationError) as ctx:
            LogbookConfig.from_mapping({'port': 'eighty', 'use_ssl': 'maybe'})
        message = str(ctx.exception)
        self.assertIn('hostname', message)
        self.assertIn('port', message)
        self.assertIn('use_ssl', message)


class TestCredentials(unittest.TestCase):

    def test_repr_masks_the_password(self):
        creds = Credentials(user='alice', password='secret')
        self.assertNotIn('secret', repr(creds))
        self.assertIn('alice', repr(creds))

    def test_clear_drops_the_password(self):
        creds = Credentials(user='alice', password='secret')
        creds.clear()
        self.assertIsNone(creds.password)
        self.assertIn('cleared', repr(creds))


class TestCredentialPrompter(unittest.TestCase):

    def test_injected_callables(self):
        creds = _prompter().prompt()
        self.assertEqual(creds.user, 'alice')
        self.assertEqual(creds.password, 'secret')

    def test_default_wiring_is_patchable(self):
        # Proves the callables are resolved in __init__ rather than captured as
        # default argument values, which mock.patch could not reach.
        with mock.patch('builtins.input', return_value='bob') as m_input, \
                mock.patch('getpass.getpass', return_value='hunter2') as m_getpass, \
                mock.patch('sys.stdin') as m_stdin:
            m_stdin.isatty.return_value = True
            creds = CredentialPrompter().prompt()
        self.assertEqual(creds.user, 'bob')
        self.assertEqual(creds.password, 'hunter2')
        m_input.assert_called_once()
        m_getpass.assert_called_once()

    def test_username_is_stripped(self):
        self.assertEqual(_prompter(user='  alice  ').prompt().user, 'alice')

    def test_password_is_not_stripped(self):
        self.assertEqual(_prompter(password=' s p ').prompt().password, ' s p ')

    def test_empty_username_reprompts(self):
        reader = mock.Mock(side_effect=['', '  ', 'alice'])
        prompter = CredentialPrompter(require_tty=False, input_fn=reader,
                                      getpass_fn=lambda _: 'secret')
        self.assertEqual(prompter.prompt().user, 'alice')
        self.assertEqual(reader.call_count, 3)

    def test_empty_password_reprompts(self):
        # An empty password would be hashed by Logbook into a real digest rather
        # than treated as absent, so it must never get through.
        reader = mock.Mock(side_effect=['', 'secret'])
        prompter = CredentialPrompter(require_tty=False, input_fn=lambda _: 'alice',
                                      getpass_fn=reader)
        self.assertEqual(prompter.prompt().password, 'secret')
        self.assertEqual(reader.call_count, 2)

    def test_attempts_exhausted(self):
        prompter = CredentialPrompter(require_tty=False, max_attempts=2,
                                      input_fn=lambda _: '',
                                      getpass_fn=lambda _: 'secret')
        self.assertRaises(LogbookCredentialsError, prompter.prompt)

    def test_eof_becomes_credentials_error(self):
        prompter = CredentialPrompter(require_tty=False,
                                      input_fn=mock.Mock(side_effect=EOFError),
                                      getpass_fn=lambda _: 'secret')
        self.assertRaises(LogbookCredentialsError, prompter.prompt)

    def test_keyboard_interrupt_propagates(self):
        prompter = CredentialPrompter(require_tty=False,
                                      input_fn=mock.Mock(side_effect=KeyboardInterrupt),
                                      getpass_fn=lambda _: 'secret')
        self.assertRaises(KeyboardInterrupt, prompter.prompt)

    def test_non_tty_is_refused(self):
        # Otherwise getpass falls back to echoing the password to the screen.
        with mock.patch('sys.stdin') as m_stdin:
            m_stdin.isatty.return_value = False
            prompter = CredentialPrompter(input_fn=lambda _: 'alice',
                                          getpass_fn=lambda _: 'secret')
            self.assertRaises(LogbookCredentialsError, prompter.prompt)


class TestLogbookSession(unittest.TestCase):

    def setUp(self):
        self.config = LogbookConfig.from_mapping(
            {'hostname': 'https://elog.example.com', 'logbook': 'Demo',
             'subdir': 'elogs', 'port': 8080})
        # Patched in logbook_md, not elog.logbook: logbook_md binds the name at
        # import time, so patching the original module would have no effect.
        patcher = mock.patch('elog.logbook_md.Logbook')
        self.MockLogbook = patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, **kwargs):
        kwargs.setdefault('prompter', _prompter())
        return LogbookSession(self.config, **kwargs)

    def test_constructor_kwargs(self):
        self._session().connect()
        self.MockLogbook.assert_called_once_with(
            hostname='https://elog.example.com', logbook='Demo', port=8080,
            subdir='elogs', use_ssl=True, user='alice', password='secret',
            encrypt_pwd=True)

    def test_no_network_on_connect(self):
        with mock.patch('elog.logbook.requests') as m_requests:
            self._session().connect()
        self.assertFalse(m_requests.method_calls)

    def test_password_is_cleared_after_connect(self):
        session = self._session()
        session.connect()
        self.assertIsNone(session.credentials.password)

    def test_password_is_cleared_even_when_construction_fails(self):
        self.MockLogbook.side_effect = ValueError('boom')
        session = self._session()
        self.assertRaises(ValueError, session.connect)
        self.assertIsNone(session.credentials.password)

    def test_verify_issues_one_request(self):
        instance = self.MockLogbook.return_value
        instance.get_message_ids.return_value = []
        self._session().connect(verify=True)
        instance.get_message_ids.assert_called_once()

    def test_empty_logbook_is_not_an_error(self):
        self.MockLogbook.return_value.get_message_ids.return_value = []
        self.assertIsNotNone(self._session().connect(verify=True))

    def test_auth_failure_propagates(self):
        self.MockLogbook.return_value.get_message_ids.side_effect = \
            LogbookAuthenticationError('nope')
        self.assertRaises(LogbookAuthenticationError,
                          self._session().connect, verify=True)

    def test_logbook_property_connects_once(self):
        session = self._session()
        self.assertIs(session.logbook, session.logbook)
        self.assertEqual(self.MockLogbook.call_count, 1)

    def test_supplied_credentials_skip_the_prompt(self):
        prompter = mock.Mock()
        session = LogbookSession(self.config, credentials=Credentials('bob', 'pw'),
                                 prompter=prompter)
        session.connect()
        prompter.prompt.assert_not_called()
        self.assertEqual(self.MockLogbook.call_args.kwargs['user'], 'bob')

    def test_connect_interactive_retries_then_succeeds(self):
        instance = self.MockLogbook.return_value
        instance.get_message_ids.side_effect = [
            LogbookAuthenticationError('nope'), []]
        session = self._session()
        self.assertIsNotNone(session.connect_interactive(attempts=2))
        self.assertEqual(self.MockLogbook.call_count, 2)

    def test_connect_interactive_gives_up(self):
        self.MockLogbook.return_value.get_message_ids.side_effect = \
            LogbookAuthenticationError('nope')
        self.assertRaises(LogbookAuthenticationError,
                          self._session().connect_interactive, attempts=2)

    def test_upload_markdown_delegates_to_the_uploader(self):
        session = self._session()
        with mock.patch.object(type(session.uploader), 'upload',
                               return_value='sentinel') as m_upload:
            self.assertEqual(session.upload_markdown('note.md'), 'sentinel')
        m_upload.assert_called_once()


class TestEndToEndOffline(ConfigFileTestCase):
    """The whole chain against the real Logbook class -- still without any I/O.

    Logbook.__init__ only builds a URL string, and connect(verify=False) issues no
    request, so this is safe by construction. The requests patch is a safety net.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch('elog.logbook.requests')
        self.m_requests = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertFalse(self.m_requests.method_calls))

    def test_real_logbook_url_from_yaml(self):
        self.write('hostname: https://elog.psi.ch\n'
                   'subdir: elogs\n'
                   'logbook: Linux+Demo\n')
        logbook = LogbookSession(load_config(self.path),
                                 prompter=_prompter()).connect()
        self.assertEqual(logbook._url, 'https://elog.psi.ch/elogs/Linux+Demo/')
        self.assertEqual(logbook.logbook, 'Linux+Demo')
        self.assertEqual(logbook._user, 'alice')
        # Logbook hashes the plaintext we hand it.
        self.assertNotEqual(logbook._password, 'secret')
        self.assertTrue(logbook._password)

    def test_bare_hostname_does_not_produce_a_schemeless_url(self):
        # Without normalisation this yields the verified failure mode 'https:///demo/'.
        self.write('hostname: elog.example.com\nlogbook: demo\n')
        logbook = LogbookSession(load_config(self.path),
                                 prompter=_prompter()).connect()
        self.assertEqual(logbook._url, 'https://elog.example.com/demo/')

    def test_port_is_not_duplicated_for_plain_http(self):
        self.write('hostname: elog.example.com\nlogbook: demo\n'
                   'use_ssl: false\nport: 80\n')
        logbook = LogbookSession(load_config(self.path),
                                 prompter=_prompter()).connect()
        self.assertEqual(logbook._url, 'http://elog.example.com/demo/')

    def test_non_default_port_is_kept(self):
        self.write('hostname: elog.example.com\nlogbook: demo\n'
                   'use_ssl: false\nport: 8080\n')
        logbook = LogbookSession(load_config(self.path),
                                 prompter=_prompter()).connect()
        self.assertEqual(logbook._url, 'http://elog.example.com:8080/demo/')


if __name__ == '__main__':
    unittest.main()
