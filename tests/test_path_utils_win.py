"""Unit tests for elog.path_utils_win.

Runs on any platform: the module parses with PureWindowsPath rather than the running
OS's rules, so Linux CI exercises the same code paths as Windows.
"""

import os
import tempfile
import unittest
from pathlib import Path

from elog.path_utils_win import (
    clean,
    looks_like_windows_path,
    to_path,
    to_python_literal,
    to_windows_string,
)

B = chr(92)   # a single backslash, spelled this way so nothing can mangle it
PASTED = B.join(['D:', 'notes', 'CosmoBrain', 'Tasks', '20260730-pos23.md'])


class TestClean(unittest.TestCase):

    def test_plain_path_is_untouched(self):
        self.assertEqual(clean(PASTED), PASTED)

    def test_double_quotes_are_stripped(self):
        # Explorer's "Copy as path" includes them.
        self.assertEqual(clean('"' + PASTED + '"'), PASTED)

    def test_single_quotes_are_stripped(self):
        self.assertEqual(clean("'" + PASTED + "'"), PASTED)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(clean('  ' + PASTED + ' \n'), PASTED)

    def test_invisible_bidi_mark_is_stripped(self):
        # Some Windows builds prefix "Copy as path" with U+202A.
        self.assertEqual(clean('\u202a' + PASTED), PASTED)

    def test_quotes_inside_the_name_survive(self):
        odd = B.join(['D:', "it's a note.md"])
        self.assertEqual(clean(odd), odd)

    def test_unmatched_quote_is_not_stripped(self):
        self.assertEqual(clean('"' + PASTED), '"' + PASTED)

    def test_empty_input(self):
        self.assertEqual(clean(''), '')
        self.assertEqual(clean(None), '')


class TestToWindowsString(unittest.TestCase):

    def test_backslashes_stay_single(self):
        # The whole point: no doubling. One real backslash per separator.
        result = to_windows_string(PASTED)
        self.assertEqual(result, PASTED)
        self.assertNotIn(B + B, result)

    def test_forward_slashes_are_converted(self):
        self.assertEqual(to_windows_string('D:/notes/Tasks/a.md'),
                         B.join(['D:', 'notes', 'Tasks', 'a.md']))

    def test_redundant_separators_collapse(self):
        self.assertEqual(to_windows_string('D:' + B + B + 'notes' + B + B + 'a.md'),
                         B.join(['D:', 'notes', 'a.md']))

    def test_quoted_input(self):
        self.assertEqual(to_windows_string('"D:/notes/a.md"'),
                         B.join(['D:', 'notes', 'a.md']))

    def test_unc_path_keeps_its_leading_pair(self):
        unc = B + B + 'server' + B + 'share' + B + 'a.md'
        self.assertEqual(to_windows_string(unc), unc)

    def test_environment_variable_is_expanded(self):
        os.environ['ELOG_TEST_DIR'] = 'D:' + B + 'notes'
        self.addCleanup(os.environ.pop, 'ELOG_TEST_DIR', None)
        self.assertEqual(to_windows_string('%ELOG_TEST_DIR%' + B + 'a.md'),
                         B.join(['D:', 'notes', 'a.md']))

    def test_expansion_can_be_disabled(self):
        self.assertIn('%NOPE%', to_windows_string('D:' + B + '%NOPE%', expand=False))

    def test_empty_raises(self):
        self.assertRaises(ValueError, to_windows_string, '   ')
        self.assertRaises(ValueError, to_windows_string, '""')


class TestToPath(unittest.TestCase):

    def test_returns_a_path(self):
        self.assertIsInstance(to_path(PASTED), Path)

    def test_filename_is_preserved(self):
        self.assertEqual(to_path(PASTED).name, '20260730-pos23.md')

    def test_quoted_input(self):
        self.assertEqual(to_path('"' + PASTED + '"').name, '20260730-pos23.md')

    def test_must_exist_accepts_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'note.md'
            real.write_text('hi', encoding='utf-8')
            # Quoted and with forward slashes, i.e. the awkward pasted form.
            self.assertEqual(to_path('"' + real.as_posix() + '"', must_exist=True).name,
                             'note.md')

    def test_must_exist_rejects_a_missing_file(self):
        self.assertRaises(FileNotFoundError, to_path,
                          'D:' + B + 'definitely' + B + 'not' + B + 'here.md',
                          must_exist=True)

    def test_empty_raises(self):
        self.assertRaises(ValueError, to_path, '')


class TestLooksLikeWindowsPath(unittest.TestCase):

    def test_drive_letter(self):
        self.assertTrue(looks_like_windows_path(PASTED))

    def test_unc(self):
        self.assertTrue(looks_like_windows_path(B + B + 'server' + B + 'share'))

    def test_quoted_drive_letter(self):
        self.assertTrue(looks_like_windows_path('"D:/notes/a.md"'))

    def test_posix_path_is_not_windows(self):
        self.assertFalse(looks_like_windows_path('/home/gdzhao/notes/a.md'))

    def test_bare_filename_is_not_windows(self):
        self.assertFalse(looks_like_windows_path('a.md'))


class TestToPythonLiteral(unittest.TestCase):

    def test_emits_a_raw_string(self):
        self.assertEqual(to_python_literal(PASTED), "r'" + PASTED + "'")

    def test_literal_evaluates_back_to_the_path(self):
        # The real guarantee: pasting the output into source reproduces the path.
        self.assertEqual(eval(to_python_literal(PASTED)), PASTED)

    def test_octal_escape_trap_is_avoided(self):
        # '\20260730' in a non-raw literal becomes '\x0260730'.
        tricky = B.join(['D:', 'Tasks', '20260730-pos23.md'])
        self.assertEqual(eval(to_python_literal(tricky)), tricky)

    def test_tab_escape_trap_is_avoided(self):
        tricky = B.join(['D:', 'temp', 'notes.md'])
        self.assertEqual(eval(to_python_literal(tricky)), tricky)

    def test_trailing_separator_is_normalised_away(self):
        # PureWindowsPath drops it, so the raw-string form still applies.
        self.assertEqual(to_python_literal('D:/notes/'),
                         "r'" + B.join(['D:', 'notes']) + "'")

    def test_drive_root_falls_back_to_escaping(self):
        # 'D:\' keeps its separator, and a raw string cannot end in a backslash.
        literal = to_python_literal('D:/')
        self.assertFalse(literal.startswith('r'))
        self.assertEqual(eval(literal), 'D:' + B)

    def test_unc_root_falls_back_to_escaping(self):
        unc = B + B + 'server' + B + 'share' + B
        literal = to_python_literal(unc)
        self.assertFalse(literal.startswith('r'))
        self.assertEqual(eval(literal), unc)

    def test_double_quote_style(self):
        self.assertEqual(eval(to_python_literal(PASTED, quote='"')), PASTED)

    def test_path_containing_the_quote_character(self):
        odd = B.join(['D:', "it's.md"])
        self.assertEqual(eval(to_python_literal(odd, quote="'")), odd)

    def test_invalid_quote_character(self):
        self.assertRaises(ValueError, to_python_literal, PASTED, quote='`')


class TestRuntimeStringsNeedNoEscaping(unittest.TestCase):
    """The premise behind this module: a pasted path is already usable as-is."""

    def test_single_backslashes_open_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'note.md'
            real.write_text('hi', encoding='utf-8')
            # str(real) holds single literal separators, exactly like input() gives.
            self.assertTrue(Path(str(real)).exists())

    def test_repr_shows_doubled_backslashes_but_the_value_is_single(self):
        # This display convention is what makes doubling look necessary.
        self.assertIn(B + B, repr(PASTED))
        self.assertNotIn(B + B, PASTED)


if __name__ == '__main__':
    unittest.main()
