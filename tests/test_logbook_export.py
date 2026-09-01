"""Unit tests for the ELOG -> Markdown exporter in elog.logbook_md.

Entirely offline: the exporter runs against a fake logbook, and the one test that
exercises the real Logbook.read parser patches requests.get.
"""

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import elog.logbook
from elog.logbook_exceptions import LogbookInvalidMessageID
from elog.logbook_md import (
    AttachmentHandler,
    BatchResult,
    LogbookConfig,
    LogbookExportError,
    LogbookSession,
    MarkdownDocument,
    MarkdownExporter,
    MarkdownFileExistsError,
    build_frontmatter,
    convert_body,
    dump_frontmatter,
)

# The user's real example entry.
EXAMPLE_ATTRIBUTES = {
    '$@MID@$': '89',
    'Date': 'Fri, 28 Aug 2026 17:28:38 +0200',
    'Author': 'Johannes',
    'Type': 'Hardware | Calibration',
    'Subject': 'Measurement with Laser in Reflection Mode with Sanded PTFE Screen',
    'Encoding': 'HTML',
}
EXAMPLE_ATTACHMENTS = [
    'https://elog.physik.uzh.ch:8080/Positioners/260828_172838_plot.png']


class _FakeLogbook:
    """Minimal stand-in: records its calls and never touches the network."""

    def __init__(self, message='<p>hi</p>', attributes=None, attachments=()):
        self.message = message
        self.attributes = dict(EXAMPLE_ATTRIBUTES if attributes is None else attributes)
        self.attachments = list(attachments)
        self.read_calls = []

    def read(self, msg_id, timeout=None):
        self.read_calls.append((msg_id, timeout))
        return self.message, dict(self.attributes), list(self.attachments)

    def download_attachment(self, url, timeout=None):
        raise AssertionError('the stub handler must not download anything')


class TestBuildFrontmatter(unittest.TestCase):

    def test_example_entry(self):
        self.assertEqual(
            build_frontmatter(EXAMPLE_ATTRIBUTES, EXAMPLE_ATTACHMENTS),
            {'id': 89,
             'date': '2026-08-28T17:28:38+02:00',
             'author': 'Johannes',
             'subject': 'Measurement with Laser in Reflection Mode with '
                        'Sanded PTFE Screen',
             'tags': ['Hardware', 'Calibration'],
             'attachments': EXAMPLE_ATTACHMENTS})

    def test_key_order(self):
        frontmatter = build_frontmatter(EXAMPLE_ATTRIBUTES, EXAMPLE_ATTACHMENTS)
        self.assertEqual(list(frontmatter),
                         ['id', 'date', 'author', 'subject', 'tags', 'attachments'])

    def test_encoding_is_dropped(self):
        self.assertNotIn('encoding', build_frontmatter(EXAMPLE_ATTRIBUTES))

    def test_id_is_an_int(self):
        self.assertIsInstance(build_frontmatter(EXAMPLE_ATTRIBUTES)['id'], int)

    def test_unknown_attributes_are_kept_and_slugified(self):
        frontmatter = build_frontmatter(
            {'$@MID@$': '1', 'Category': 'General', 'Locked by': 'ab'})
        self.assertEqual(frontmatter['category'], 'General')
        self.assertEqual(frontmatter['locked_by'], 'ab')

    def test_reply_ids_become_int_lists(self):
        frontmatter = build_frontmatter({'$@MID@$': '1', 'In reply to': '12,34'})
        self.assertEqual(frontmatter['in_reply_to'], [12, 34])

    def test_blank_values_are_dropped(self):
        frontmatter = build_frontmatter(
            {'$@MID@$': '1', 'Author': '', 'Category': ''})
        self.assertNotIn('author', frontmatter)
        self.assertNotIn('category', frontmatter)

    def test_reserved_key_collision_is_prefixed_and_warns(self):
        with self.assertWarns(UserWarning):
            frontmatter = build_frontmatter({'$@MID@$': '1', 'Tags': 'x'})
        self.assertEqual(frontmatter['elog_tags'], 'x')
        self.assertEqual(frontmatter.get('tags'), None)

    def test_slug_collision_keeps_the_first_and_warns(self):
        with self.assertWarns(UserWarning):
            frontmatter = build_frontmatter(
                {'$@MID@$': '1', 'Reply to': '1', 'Reply_To': '2'})
        self.assertEqual(frontmatter['reply_to'], [1])

    def test_attachments_default_to_empty(self):
        self.assertEqual(build_frontmatter(EXAMPLE_ATTRIBUTES)['attachments'], [])


class TestFrontmatterDate(unittest.TestCase):

    def _date(self, raw):
        return build_frontmatter({'$@MID@$': '1', 'Date': raw}).get('date')

    def test_offset_is_preserved(self):
        self.assertEqual(self._date('Fri, 28 Aug 2026 17:28:38 +0200'),
                         '2026-08-28T17:28:38+02:00')

    def test_result_is_a_string_not_a_datetime(self):
        # PyYAML renders a datetime space-separated and unquoted, which Obsidian
        # rejects as a date property.
        self.assertIsInstance(self._date('Fri, 28 Aug 2026 17:28:38 +0200'), str)

    def test_naive_date_stays_naive(self):
        self.assertEqual(self._date('Fri, 28 Aug 2026 17:28:38'),
                         '2026-08-28T17:28:38')

    def test_missing_date_omits_the_key(self):
        self.assertIsNone(self._date(''))
        self.assertNotIn('date', build_frontmatter({'$@MID@$': '1'}))

    def test_unparseable_date_is_kept_verbatim_and_warns(self):
        with self.assertWarns(UserWarning):
            self.assertEqual(self._date('not a date'), 'not a date')


class TestObsidianTags(unittest.TestCase):

    def _tags(self, raw):
        return build_frontmatter({'$@MID@$': '1', 'Type': raw}).get('tags')

    def test_pipe_split(self):
        self.assertEqual(self._tags('Hardware | Calibration'),
                         ['Hardware', 'Calibration'])

    def test_spaces_become_dashes(self):
        # Obsidian tags cannot contain whitespace.
        self.assertEqual(self._tags('Software Installation'),
                         ['Software-Installation'])

    def test_whitespace_runs_collapse(self):
        self.assertEqual(self._tags('Problem\t\tFixed'), ['Problem-Fixed'])

    def test_empty_segments_are_dropped(self):
        self.assertEqual(self._tags('Hardware ||  | Calibration'),
                         ['Hardware', 'Calibration'])

    def test_duplicates_dedupe_first_seen(self):
        self.assertEqual(self._tags('B | A | B'), ['B', 'A'])

    def test_leading_hash_is_stripped(self):
        self.assertEqual(self._tags('#Routine'), ['Routine'])

    def test_missing_type_omits_the_key(self):
        self.assertIsNone(self._tags(''))
        self.assertNotIn('tags', build_frontmatter({'$@MID@$': '1'}))


class TestHtmlBody(unittest.TestCase):

    def convert(self, html):
        return convert_body(html, 'HTML')

    def test_bold_and_italic(self):
        self.assertEqual(self.convert('<p><b>a</b> and <i>b</i></p>'),
                         '**a** and *b*')

    def test_atx_headings(self):
        self.assertEqual(self.convert('<h2>Setup</h2>'), '## Setup')

    def test_dash_bullets_at_every_level(self):
        markdown = self.convert('<ul><li>a<ul><li>b</li></ul></li></ul>')
        self.assertIn('- a', markdown)
        self.assertIn('- b', markdown)
        self.assertNotIn('*', markdown)

    def test_links_are_not_autolinks(self):
        self.assertEqual(
            self.convert('<a href="http://x.example">http://x.example</a>'),
            '[http://x.example](http://x.example)')

    def test_image_keeps_the_server_filename(self):
        # That filename is the join key to the attachments frontmatter list.
        self.assertEqual(self.convert('<img src="260828_172838_plot.png">'),
                         '![](260828_172838_plot.png)')

    def test_script_content_is_removed(self):
        # markdownify's strip= would keep the text; the regex pre-pass must drop it.
        self.assertNotIn('alert', self.convert('<p>hi</p><script>alert(1)</script>'))

    def test_style_content_is_removed(self):
        self.assertNotIn('color', self.convert('<style>p{color:red}</style><p>hi</p>'))

    def test_long_paragraphs_are_not_wrapped(self):
        html = '<p>{0}</p>'.format('word ' * 60)
        self.assertEqual(len(self.convert(html).splitlines()), 1)

    def test_entities_are_decoded(self):
        # escape_misc escapes the resulting '&'. That renders as a plain '&', and the
        # option is worth keeping: without it a paragraph starting '- 5 V' would turn
        # into a list item.
        self.assertEqual(self.convert('<p>a &amp; b</p>'), r'a \& b')

    def test_leading_dash_does_not_become_a_list_item(self):
        # The reason escape_misc stays on.
        self.assertEqual(self.convert('<p>- 5 V supply</p>'), r'\- 5 V supply')


class TestPlainBody(unittest.TestCase):

    def test_verbatim(self):
        self.assertEqual(convert_body('line one\nline two', 'plain'),
                         'line one\nline two')

    def test_markdown_characters_are_not_escaped(self):
        self.assertEqual(convert_body('use *stars* and # hash', 'plain'),
                         'use *stars* and # hash')

    def test_trailing_whitespace_is_trimmed(self):
        self.assertEqual(convert_body('a   \nb\t', 'plain'), 'a\nb')


class TestELCodeBody(unittest.TestCase):

    def convert(self, text):
        return convert_body(text, 'ELCode')

    def test_inline_tags(self):
        self.assertEqual(self.convert('[b]a[/b] [i]b[/i] [s]c[/s]'),
                         '**a** *b* ~~c~~')

    def test_url_with_label(self):
        self.assertEqual(self.convert('[url=http://x]link[/url]'), '[link](http://x)')

    def test_image(self):
        self.assertEqual(self.convert('[img]plot.png[/img]'), '![](plot.png)')

    def test_list(self):
        self.assertEqual(self.convert('[list]\n[*]a\n[*]b\n[/list]'), '- a\n- b')

    def test_code_block(self):
        self.assertIn('```', self.convert('[code]x = 1[/code]'))

    def test_presentational_tags_are_dropped_keeping_content(self):
        self.assertEqual(self.convert('[color=red]hot[/color]'), 'hot')

    def test_unknown_tag_survives_verbatim(self):
        self.assertIn('[foo]', self.convert('a [foo] b'))

    def test_unbalanced_tag_is_not_mangled(self):
        self.assertIn('[b]', self.convert('a [b] b'))


class TestEncodingDispatch(unittest.TestCase):

    def test_each_named_encoding_routes_correctly(self):
        self.assertEqual(convert_body('<b>x</b>', 'HTML'), '**x**')
        self.assertEqual(convert_body('<b>x</b>', 'plain'), '<b>x</b>')
        self.assertEqual(convert_body('[b]x[/b]', 'ELCode'), '**x**')

    def test_missing_encoding_sniffs_html_and_warns(self):
        with self.assertWarns(UserWarning):
            self.assertEqual(convert_body('<p>hi</p>', None), 'hi')

    def test_wrong_case_encoding_falls_through_to_the_sniffer(self):
        # 'Encoding' is case sensitive on the wire, so 'html' is genuinely unknown.
        with self.assertWarns(UserWarning):
            self.assertEqual(convert_body('<b>x</b>', 'html'), '**x**')

    def test_missing_encoding_sniffs_elcode(self):
        with self.assertWarns(UserWarning):
            self.assertEqual(convert_body('[b]x[/b]', None), '**x**')

    def test_missing_encoding_defaults_to_plain(self):
        with self.assertWarns(UserWarning):
            self.assertEqual(convert_body('just words', None), 'just words')


class TestMojibakeRepair(unittest.TestCase):

    #: 'café' as UTF-8 bytes mis-decoded as latin-1, which is what read() produces.
    MOJIBAKE = 'cafÃ©'

    def test_body_is_repaired(self):
        self.assertEqual(convert_body(self.MOJIBAKE, 'plain'), 'café')

    def test_attribute_is_repaired(self):
        frontmatter = build_frontmatter({'$@MID@$': '1', 'Author': self.MOJIBAKE})
        self.assertEqual(frontmatter['author'], 'café')

    def test_repair_can_be_disabled(self):
        self.assertEqual(convert_body(self.MOJIBAKE, 'plain', repair=False),
                         self.MOJIBAKE)

    def test_ascii_is_untouched(self):
        self.assertEqual(convert_body('plain ascii', 'plain'), 'plain ascii')

    def test_genuine_latin1_is_untouched(self):
        # Already correct text must not be double-decoded into nonsense.
        self.assertEqual(convert_body('café', 'plain'), 'café')


class TestDumpFrontmatter(unittest.TestCase):

    def test_example_yaml(self):
        self.assertEqual(
            dump_frontmatter(build_frontmatter(EXAMPLE_ATTRIBUTES, EXAMPLE_ATTACHMENTS)),
            "---\n"
            "id: 89\n"
            "date: '2026-08-28T17:28:38+02:00'\n"
            "author: Johannes\n"
            "subject: Measurement with Laser in Reflection Mode with Sanded PTFE Screen\n"
            "tags:\n"
            "  - Hardware\n"
            "  - Calibration\n"
            "attachments:\n"
            "  - https://elog.physik.uzh.ch:8080/Positioners/260828_172838_plot.png\n"
            "---")

    def test_date_is_quoted(self):
        # Unquoted it would round-trip back as a !!timestamp, not a string.
        self.assertIn("date: '2026-08-28T17:28:38+02:00'",
                      dump_frontmatter(build_frontmatter(EXAMPLE_ATTRIBUTES)))

    def test_list_items_are_indented(self):
        # Obsidian writes the indented form, so matching it keeps re-saves diff-free.
        self.assertIn('\n  - Hardware', dump_frontmatter(
            build_frontmatter(EXAMPLE_ATTRIBUTES)))

    def test_long_values_are_not_folded(self):
        frontmatter = build_frontmatter(
            {'$@MID@$': '1', 'Subject': 'x' * 300})
        self.assertIn('x' * 300, dump_frontmatter(frontmatter))

    def test_unicode_is_not_escaped(self):
        frontmatter = build_frontmatter({'$@MID@$': '1', 'Author': 'Müller'})
        self.assertIn('Müller', dump_frontmatter(frontmatter))


class TestMarkdownDocument(unittest.TestCase):

    def test_one_blank_line_between_fence_and_body(self):
        text = MarkdownDocument({'id': 1, 'attachments': []}, 'body').to_text()
        self.assertIn('---\n\nbody', text)

    def test_ends_with_exactly_one_newline(self):
        text = MarkdownDocument({'id': 1, 'attachments': []}, 'body\n\n\n').to_text()
        self.assertTrue(text.endswith('body\n'))
        self.assertFalse(text.endswith('body\n\n'))

    def test_msg_id(self):
        self.assertEqual(MarkdownDocument({'id': 89}, '').msg_id, 89)


class TestMarkdownExporter(unittest.TestCase):

    def test_reads_once_with_the_timeout(self):
        logbook = _FakeLogbook()
        MarkdownExporter(logbook, timeout=7).to_markdown(89)
        self.assertEqual(logbook.read_calls, [(89, 7)])

    def test_end_to_end_document(self):
        logbook = _FakeLogbook(message='<p>Ran the <b>laser</b>.</p>',
                               attachments=EXAMPLE_ATTACHMENTS)
        document = MarkdownExporter(logbook).to_markdown(89)
        self.assertEqual(document.body, 'Ran the **laser**.')
        self.assertEqual(document.frontmatter['id'], 89)
        self.assertEqual(document.frontmatter['tags'], ['Hardware', 'Calibration'])
        self.assertEqual(document.frontmatter['attachments'], EXAMPLE_ATTACHMENTS)


class TestToMarkdownFile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / 'vault'
        self.exporter = MarkdownExporter(_FakeLogbook())

    def test_zero_padded_filename(self):
        self.assertEqual(self.exporter.to_markdown_file(89, self.out).name, '0089.md')

    def test_string_msg_id_is_accepted(self):
        # Callers pass attributes['$@MID@$'], which is a string.
        self.assertEqual(self.exporter.to_markdown_file('89', self.out).name, '0089.md')

    def test_padding_is_a_minimum(self):
        self.assertEqual(self.exporter.to_markdown_file(12345, self.out).name,
                         '12345.md')

    def test_missing_directory_is_created(self):
        target = self.exporter.to_markdown_file(89, self.out / 'a' / 'b')
        self.assertTrue(target.exists())

    def test_overwrites_by_default(self):
        first = self.exporter.to_markdown_file(89, self.out)
        first.write_text('stale', encoding='utf-8')
        self.exporter.to_markdown_file(89, self.out)
        self.assertNotIn('stale', first.read_text(encoding='utf-8'))

    def test_overwrite_false_raises_both_types(self):
        self.exporter.to_markdown_file(89, self.out)
        with self.assertRaises(MarkdownFileExistsError):
            self.exporter.to_markdown_file(89, self.out, overwrite=False)
        with self.assertRaises(FileExistsError):
            self.exporter.to_markdown_file(89, self.out, overwrite=False)

    def test_written_with_lf_and_utf8(self):
        target = self.exporter.to_markdown_file(89, self.out)
        raw = target.read_bytes()
        self.assertNotIn(b'\r', raw)
        raw.decode('utf-8')

    def test_no_temp_file_left_behind(self):
        self.exporter.to_markdown_file(89, self.out)
        self.assertEqual(list(self.out.glob('*.tmp')), [])

    def test_non_numeric_msg_id(self):
        self.assertRaises(LogbookExportError,
                          self.exporter.to_markdown_file, 'abc', self.out)


class TestAttachmentStub(unittest.TestCase):

    def test_urls_land_in_frontmatter_and_nothing_downloads(self):
        logbook = _FakeLogbook(message='<img src="260828_172838_plot.png">',
                               attachments=EXAMPLE_ATTACHMENTS)
        document = MarkdownExporter(logbook).to_markdown(89)
        self.assertEqual(document.frontmatter['attachments'], EXAMPLE_ATTACHMENTS)
        # The body keeps the raw server filename: the join key for a future downloader.
        self.assertEqual(document.body, '![](260828_172838_plot.png)')

    def test_custom_handler_receives_the_context(self):
        seen = {}

        class Recording(AttachmentHandler):
            def process(self, body, attachments, out_dir=None, stem=None, logbook=None):
                seen.update(stem=stem, out_dir=out_dir, logbook=logbook)
                return body, ['local/plot.png']

        logbook = _FakeLogbook(attachments=EXAMPLE_ATTACHMENTS)
        document = MarkdownExporter(logbook, attachments=Recording()).to_markdown(89)
        self.assertEqual(document.frontmatter['attachments'], ['local/plot.png'])
        self.assertEqual(seen['stem'], '0089')
        self.assertIs(seen['logbook'], logbook)


class _BatchLogbook(_FakeLogbook):
    """A fake holding several entries, some of which fail to read."""

    def __init__(self, ids=(1, 2, 89), broken=()):
        super().__init__()
        self.ids = list(ids)
        self.broken = set(broken)
        self.search_calls = []

    def search(self, term, n_results=20, scope='subtext', timeout=None):
        self.search_calls.append((term, n_results, timeout))
        # ELOG lists newest first; the exporter is expected to sort.
        return list(reversed(self.ids))

    def read(self, msg_id, timeout=None):
        self.read_calls.append((msg_id, timeout))
        if msg_id in self.broken:
            raise LogbookInvalidMessageID('no such entry {0}'.format(msg_id))
        attributes = dict(EXAMPLE_ATTRIBUTES)
        attributes['$@MID@$'] = str(msg_id)
        return '<p>entry {0}</p>'.format(msg_id), attributes, []


class TestListMessageIds(unittest.TestCase):

    def test_uses_search_with_a_large_page_size(self):
        # get_message_ids hits /page with no npp and would silently truncate.
        logbook = _BatchLogbook(ids=[1, 2, 89])
        exporter = MarkdownExporter(logbook, timeout=5)
        self.assertEqual(exporter.list_message_ids(), [1, 2, 89])
        term, n_results, timeout = logbook.search_calls[0]
        self.assertEqual(term, '')
        self.assertGreaterEqual(n_results, 1000000)
        self.assertEqual(timeout, 5)

    def test_result_is_sorted_ascending(self):
        self.assertEqual(MarkdownExporter(_BatchLogbook(ids=[5, 1, 3])
                                          ).list_message_ids(), [1, 3, 5])

    def test_page_size_is_configurable(self):
        logbook = _BatchLogbook()
        MarkdownExporter(logbook).list_message_ids(page_size=50)
        self.assertEqual(logbook.search_calls[0][1], 50)


class TestExportAll(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / 'vault'

    def test_exports_every_entry(self):
        result = MarkdownExporter(_BatchLogbook(ids=[1, 2, 89])).export_all(self.out)
        self.assertEqual(len(result.exported), 3)
        self.assertEqual(sorted(p.name for p in self.out.glob('*.md')),
                         ['0001.md', '0002.md', '0089.md'])

    def test_discovers_ids_when_not_given(self):
        logbook = _BatchLogbook(ids=[1, 2])
        MarkdownExporter(logbook).export_all(self.out)
        self.assertEqual(len(logbook.search_calls), 1)
        self.assertEqual([call[0] for call in logbook.read_calls], [1, 2])

    def test_explicit_ids_skip_discovery(self):
        logbook = _BatchLogbook(ids=[1, 2, 89])
        MarkdownExporter(logbook).export_all(self.out, msg_ids=[2])
        self.assertEqual(logbook.search_calls, [])
        self.assertEqual([call[0] for call in logbook.read_calls], [2])

    def test_one_failure_does_not_stop_the_batch(self):
        logbook = _BatchLogbook(ids=[1, 2, 89], broken=[2])
        with self.assertWarns(UserWarning):
            result = MarkdownExporter(logbook).export_all(self.out)
        self.assertEqual([msg_id for msg_id, _ in result.exported], [1, 89])
        self.assertEqual([msg_id for msg_id, _ in result.failed], [2])
        self.assertIsInstance(result.failed[0][1], LogbookInvalidMessageID)

    def test_stop_on_error_reraises(self):
        logbook = _BatchLogbook(ids=[1, 2, 89], broken=[2])
        self.assertRaises(LogbookInvalidMessageID,
                          MarkdownExporter(logbook).export_all,
                          self.out, stop_on_error=True)

    def test_skip_existing(self):
        exporter = MarkdownExporter(_BatchLogbook(ids=[1, 2]))
        exporter.export_all(self.out)
        logbook = _BatchLogbook(ids=[1, 2])
        result = MarkdownExporter(logbook).export_all(self.out, skip_existing=True)
        self.assertEqual(result.skipped, [1, 2])
        self.assertEqual(logbook.read_calls, [])   # nothing re-read

    def test_re_export_refreshes_by_default(self):
        exporter = MarkdownExporter(_BatchLogbook(ids=[1]))
        exporter.export_all(self.out)
        (self.out / '0001.md').write_text('stale', encoding='utf-8')
        exporter.export_all(self.out)
        self.assertNotIn('stale',
                         (self.out / '0001.md').read_text(encoding='utf-8'))

    def test_progress_callback(self):
        seen = []
        MarkdownExporter(_BatchLogbook(ids=[1, 2])).export_all(
            self.out, progress=lambda *a: seen.append(a))
        self.assertEqual(seen, [(1, 2, 1, 'exported'), (2, 2, 2, 'exported')])

    def test_progress_reports_failures(self):
        seen = []
        with self.assertWarns(UserWarning):
            MarkdownExporter(_BatchLogbook(ids=[1], broken=[1])).export_all(
                self.out, progress=lambda *a: seen.append(a))
        self.assertEqual(seen, [(1, 1, 1, 'failed')])

    def test_output_directory_is_created(self):
        MarkdownExporter(_BatchLogbook(ids=[1])).export_all(self.out / 'a' / 'b')
        self.assertTrue((self.out / 'a' / 'b' / '0001.md').exists())

    def test_empty_logbook(self):
        result = MarkdownExporter(_BatchLogbook(ids=[])).export_all(self.out)
        self.assertEqual(result.total, 0)
        self.assertTrue(result)

    def test_single_credential_resolution(self):
        # The Logbook is built once, so a whole batch prompts at most once.
        logbook = _BatchLogbook(ids=[1, 2, 89])
        exporter = MarkdownExporter(logbook)
        exporter.export_all(self.out)
        self.assertIs(exporter.logbook, logbook)


class TestBatchResult(unittest.TestCase):

    def test_summary_and_truthiness(self):
        result = BatchResult(exported=[(1, 'a')], skipped=[2], failed=[(3, ValueError())])
        self.assertEqual(result.total, 3)
        self.assertEqual(result.summary(), '1 exported, 1 skipped, 1 failed (of 3)')
        self.assertFalse(result)

    def test_clean_result_is_truthy(self):
        self.assertTrue(BatchResult(exported=[(1, 'a')]))


class TestLogbookSessionExport(unittest.TestCase):

    def test_session_delegates_and_builds_the_exporter_once(self):
        config = LogbookConfig.from_mapping(
            {'hostname': 'https://elog.example.com', 'logbook': 'Demo'})
        with mock.patch('elog.logbook_md.Logbook') as MockLogbook:
            MockLogbook.return_value = _FakeLogbook()
            session = LogbookSession(config, credentials=mock.Mock(user='a',
                                                                  password='b'))
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(session.to_markdown_file(89, tmp).name, '0089.md')
                session.to_markdown(89)
        self.assertEqual(MockLogbook.call_count, 1)


class TestReadAttributeParsing(unittest.TestCase):
    """Logbook.read's header parser. Offline: only requests.get is patched."""

    def _read(self, header_lines, body='body'):
        logbook = elog.logbook.Logbook('https://elog.example.com', 'demo')
        payload = ('\r\n'.join(header_lines) +
                   '\r\n' + '=' * 40 + '\r\n' + body).encode('iso-8859-1')
        # Patch requests.get specifically, not the whole module: the `except
        # requests.Timeout` clauses need real exception classes.
        with mock.patch.object(elog.logbook.Logbook, '_check_if_message_on_server'), \
                mock.patch('elog.logbook.requests.get') as m_get:
            m_get.return_value = mock.Mock(status_code=200, headers={}, content=payload)
            return logbook.read(89)

    def test_value_containing_the_separator_is_preserved(self):
        # Regression: split(': ') + ''.join produced 'Renozzle test'.
        _, attributes, _ = self._read(['Subject: Re: nozzle test'])
        self.assertEqual(attributes['Subject'], 'Re: nozzle test')

    def test_url_value_is_preserved(self):
        _, attributes, _ = self._read(['Subject: see https://x.example: page 2'])
        self.assertEqual(attributes['Subject'], 'see https://x.example: page 2')

    def test_header_with_no_value(self):
        _, attributes, _ = self._read(['Category:'])
        # The key must be 'Category', not 'Category:'.
        self.assertEqual(attributes['Category'], '')

    def test_ordinary_attributes_still_parse(self):
        _, attributes, _ = self._read(['Author: Johannes', 'Encoding: HTML'])
        self.assertEqual(attributes['Author'], 'Johannes')
        self.assertEqual(attributes['Encoding'], 'HTML')

    def test_empty_attachment_line_yields_empty_list(self):
        _, _, attachments = self._read(['Attachment: '])
        self.assertEqual(attachments, [])

    def test_attachment_urls_are_absolute(self):
        _, _, attachments = self._read(['Attachment: 260828_172838_plot.png'])
        self.assertEqual(
            attachments,
            ['https://elog.example.com/demo/260828_172838_plot.png'])

    def test_body_is_everything_after_the_delimiter(self):
        message, _, _ = self._read(['Author: x'], body='line one\r\nline two')
        self.assertEqual(message, 'line one\nline two')


class TestExportedEntryIsValidMarkdown(unittest.TestCase):
    """End-to-end: the example entry round-trips through YAML as expected."""

    def test_frontmatter_parses_back(self):
        import yaml

        logbook = _FakeLogbook(message='<p>Ran the <b>laser</b>.</p>',
                               attachments=EXAMPLE_ATTACHMENTS)
        with tempfile.TemporaryDirectory() as tmp:
            target = MarkdownExporter(logbook).to_markdown_file(89, tmp)
            text = target.read_text(encoding='utf-8')

        _, _, rest = text.partition('---\n')
        block, _, body = rest.partition('\n---\n')
        parsed = yaml.safe_load(block)

        self.assertEqual(parsed['id'], 89)
        self.assertEqual(parsed['tags'], ['Hardware', 'Calibration'])
        # Still a string after a round trip, not coerced to a datetime.
        self.assertIsInstance(parsed['date'], str)
        self.assertEqual(body.strip(), 'Ran the **laser**.')


if __name__ == '__main__':
    warnings.simplefilter('always')
    unittest.main()
