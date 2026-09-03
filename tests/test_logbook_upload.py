"""Unit tests for the Markdown -> ELOG uploader in elog.logbook_md.

Offline: the uploader runs against a fake logbook that records post() calls, and notes
are written into a temporary directory.
"""

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from elog.logbook_exceptions import LogbookServerProblem
from elog.logbook_md import (
    LogbookUploadError,
    MarkdownUploader,
    UploadResult,
    build_attributes,
    find_linked_files,
    markdown_to_html,
    normalize_tags,
    prompt_author,
    prompt_tags,
    split_note,
    to_latin1_safe,
)

BASE = 'https://elog.physik.uzh.ch:8080/Positioners/'

NOTE = """---
subject: Laser zero-finding
author: Johannes
tags:
  - Hardware
  - Calibration
---

## Setup

Ran the **laser** in reflection mode.

![[plot.png]]
"""


class _FakeLogbook:
    """Records post() calls; serves a stored-attachment list for the inline phase."""

    def __init__(self, msg_id=91, stored=None, post_error=None, read_error=None):
        self.msg_id = msg_id
        self.stored = list(stored or [])
        self.post_error = post_error
        self.read_error = read_error
        self.posts = []

    def post(self, message, msg_id=None, attributes=None, attachments=None,
             encoding=None, timeout=None, **kwargs):
        self.posts.append({'message': message, 'msg_id': msg_id,
                           'attributes': dict(attributes or {}),
                           'attachments': list(attachments or []),
                           'encoding': encoding})
        if self.post_error is not None and len(self.posts) > 1:
            raise self.post_error
        return msg_id or self.msg_id

    def read(self, msg_id, timeout=None):
        if self.read_error is not None:
            raise self.read_error
        return 'body', {}, list(self.stored)


class NoteTestCase(unittest.TestCase):
    """Base providing a scratch directory and a note-writing helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write_note(self, text=NOTE, name='20260730-pos23.md'):
        path = self.dir / name
        path.write_text(text, encoding='utf-8')
        return path

    def write_file(self, name, data=b'bytes'):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class TestSplitNote(unittest.TestCase):

    def test_frontmatter_and_body(self):
        frontmatter, body = split_note(NOTE)
        self.assertEqual(frontmatter['subject'], 'Laser zero-finding')
        self.assertEqual(frontmatter['tags'], ['Hardware', 'Calibration'])
        self.assertTrue(body.lstrip().startswith('## Setup'))

    def test_note_without_frontmatter(self):
        frontmatter, body = split_note('# Just a note\n\ntext\n')
        self.assertEqual(frontmatter, {})
        self.assertEqual(body, '# Just a note\n\ntext\n')

    def test_crlf_frontmatter(self):
        frontmatter, body = split_note('---\r\nsubject: X\r\n---\r\nbody\r\n')
        self.assertEqual(frontmatter['subject'], 'X')
        self.assertEqual(body.strip(), 'body')

    def test_empty_frontmatter_block(self):
        frontmatter, body = split_note('---\n\n---\nbody\n')
        self.assertEqual(frontmatter, {})
        self.assertEqual(body.strip(), 'body')

    def test_empty_note(self):
        self.assertEqual(split_note(''), ({}, ''))

    def test_leading_bom_is_tolerated(self):
        frontmatter, _ = split_note('﻿---\nsubject: X\n---\nbody\n')
        self.assertEqual(frontmatter['subject'], 'X')

    def test_malformed_yaml(self):
        self.assertRaises(LogbookUploadError, split_note, '---\nsubject: [\n---\nbody\n')

    def test_non_mapping_frontmatter(self):
        self.assertRaises(LogbookUploadError, split_note, '---\n- a\n- b\n---\nbody\n')

    def test_a_horizontal_rule_is_not_frontmatter(self):
        frontmatter, body = split_note('text\n\n---\n\nmore\n')
        self.assertEqual(frontmatter, {})
        self.assertIn('more', body)


class TestNormalizeTags(unittest.TestCase):

    def test_first_letter_is_capitalised(self):
        self.assertEqual(normalize_tags(['hardware', 'software']),
                         ['Hardware', 'Software'])

    def test_rest_of_the_tag_is_left_alone(self):
        # str.capitalize would give 'Adc' / 'Ph'; only the first letter may change.
        self.assertEqual(normalize_tags(['ADC', 'pH meter']), ['ADC', 'PH meter'])

    def test_comma_separated_string(self):
        self.assertEqual(normalize_tags('hardware, calibration'),
                         ['Hardware', 'Calibration'])

    def test_blank_entries_are_dropped(self):
        self.assertEqual(normalize_tags('hardware, , ,calibration'),
                         ['Hardware', 'Calibration'])

    def test_duplicates_are_removed_preserving_order(self):
        self.assertEqual(normalize_tags(['b', 'a', 'B']), ['B', 'A'])

    def test_empty(self):
        self.assertEqual(normalize_tags(None), [])
        self.assertEqual(normalize_tags(''), [])


class TestPrompts(unittest.TestCase):

    def test_tags_are_prompted_and_split(self):
        self.assertEqual(prompt_tags(input_fn=lambda _: 'hardware, calibration'),
                         ['Hardware', 'Calibration'])

    def test_empty_tags_reprompt(self):
        reader = mock.Mock(side_effect=['', '  ', 'general'])
        self.assertEqual(prompt_tags(input_fn=reader), ['General'])
        self.assertEqual(reader.call_count, 3)

    def test_tags_attempts_exhausted(self):
        self.assertRaises(LogbookUploadError, prompt_tags,
                          max_attempts=2, input_fn=lambda _: '')

    def test_tags_eof(self):
        self.assertRaises(LogbookUploadError, prompt_tags,
                          input_fn=mock.Mock(side_effect=EOFError))

    def test_author_is_prompted_and_stripped(self):
        self.assertEqual(prompt_author(input_fn=lambda _: '  Johannes '), 'Johannes')

    def test_author_attempts_exhausted(self):
        self.assertRaises(LogbookUploadError, prompt_author,
                          max_attempts=2, input_fn=lambda _: '')

    def test_keyboard_interrupt_propagates(self):
        self.assertRaises(KeyboardInterrupt, prompt_tags,
                          input_fn=mock.Mock(side_effect=KeyboardInterrupt))


class TestBuildAttributes(unittest.TestCase):

    def test_subject_from_frontmatter(self):
        attributes = build_attributes({'subject': 'Laser test'}, 'notes/0089.md')
        self.assertEqual(attributes['Subject'], 'Laser test')

    def test_subject_falls_back_to_the_filename_stem(self):
        attributes = build_attributes({}, 'notes/20260730-pos23-betafix-rbl.md')
        self.assertEqual(attributes['Subject'], '20260730-pos23-betafix-rbl')

    def test_blank_subject_falls_back_too(self):
        attributes = build_attributes({'subject': '   '}, 'notes/fallback.md')
        self.assertEqual(attributes['Subject'], 'fallback')

    def test_tags_become_a_pipe_joined_type(self):
        attributes = build_attributes({'tags': ['Hardware', 'Calibration']}, 'n.md')
        self.assertEqual(attributes['Type'], 'Hardware | Calibration')

    def test_no_tags_means_no_type_key(self):
        self.assertNotIn('Type', build_attributes({}, 'n.md'))

    def test_author_override_wins(self):
        attributes = build_attributes({'author': 'A'}, 'n.md', author='B')
        self.assertEqual(attributes['Author'], 'B')

    def test_when_is_never_set(self):
        # Logbook.post stamps datetime.now() itself for a new entry.
        self.assertNotIn('When', build_attributes({'date': '2026-08-28'}, 'n.md'))

    def test_bookkeeping_keys_are_dropped(self):
        attributes = build_attributes(
            {'id': 89, 'date': 'x', 'attachments': ['a'], 'reply_to': [1]}, 'n.md')
        self.assertEqual(set(attributes), {'Subject'})

    def test_extra_attributes_pass_through(self):
        attributes = build_attributes({}, 'n.md',
                                      extra_attributes={'Category': 'General'})
        self.assertEqual(attributes['Category'], 'General')


class TestMarkdownToHtml(unittest.TestCase):

    def test_heading_and_bold(self):
        html = markdown_to_html('## Setup\n\nRan the **laser**.\n')
        self.assertIn('<h2>Setup</h2>', html)
        self.assertIn('<strong>laser</strong>', html)

    def test_list(self):
        self.assertIn('<li>x</li>', markdown_to_html('- x\n- y\n'))

    def test_fenced_code(self):
        self.assertIn('<code>', markdown_to_html('```\nx = 1\n```\n'))

    def test_table(self):
        html = markdown_to_html('| a | b |\n| --- | --- |\n| 1 | 2 |\n')
        self.assertIn('<table>', html)

    def test_empty_body(self):
        self.assertEqual(markdown_to_html(''), '')


class TestFindLinkedFiles(NoteTestCase):

    def test_wikilink_beside_the_note(self):
        self.write_file('plot.png')
        files, unresolved = find_linked_files('![[plot.png]]', self.dir)
        self.assertEqual([p.name for p in files], ['plot.png'])
        self.assertEqual(unresolved, [])

    def test_attachments_subfolder_fallback(self):
        self.write_file('attachments/plot.png')
        files, _ = find_linked_files('![[plot.png]]', self.dir)
        self.assertEqual([p.name for p in files], ['plot.png'])

    def test_markdown_image_link(self):
        self.write_file('plot.png')
        files, _ = find_linked_files('![alt](plot.png)', self.dir)
        self.assertEqual([p.name for p in files], ['plot.png'])

    def test_plain_markdown_link(self):
        self.write_file('data.csv')
        files, _ = find_linked_files('[the data](data.csv)', self.dir)
        self.assertEqual([p.name for p in files], ['data.csv'])

    def test_wikilink_with_alias(self):
        self.write_file('plot.png')
        files, _ = find_linked_files('![[plot.png|300]]', self.dir)
        self.assertEqual([p.name for p in files], ['plot.png'])

    def test_percent_encoded_space(self):
        self.write_file('my plot.png')
        files, _ = find_linked_files('![](my%20plot.png)', self.dir)
        self.assertEqual([p.name for p in files], ['my plot.png'])

    def test_absolute_path(self):
        target = self.write_file('abs.png')
        files, _ = find_linked_files('![]({0})'.format(target.as_posix()), self.dir)
        self.assertEqual([p.name for p in files], ['abs.png'])

    def test_external_urls_are_skipped(self):
        files, unresolved = find_linked_files('![](https://x.example/a.png)', self.dir)
        self.assertEqual(files, [])
        self.assertEqual(unresolved, [])

    def test_missing_file_is_reported_not_raised(self):
        files, unresolved = find_linked_files('![[nope.png]]', self.dir)
        self.assertEqual(files, [])
        self.assertEqual(unresolved, ['nope.png'])

    def test_duplicate_links_upload_once(self):
        self.write_file('plot.png')
        files, _ = find_linked_files('![[plot.png]] and ![[plot.png]]', self.dir)
        self.assertEqual(len(files), 1)


class TestLatin1Safe(unittest.TestCase):

    def test_ascii_is_untouched(self):
        text, replaced = to_latin1_safe('plain ascii')
        self.assertEqual(text, 'plain ascii')
        self.assertEqual(replaced, [])

    def test_latin1_range_is_untouched(self):
        # 'e-acute' and the degree sign are representable; do not mangle them.
        text, replaced = to_latin1_safe('café 20°C')
        self.assertEqual(text, 'café 20°C')
        self.assertEqual(replaced, [])

    def test_mapped_characters(self):
        for char, expected in [('–', '-'), ('—', '--'), ('−', '-'),
                               ('≈', '~='), ('→', '->'), ('✓', '[x]'),
                               ('’', "'"), ('“', '"')]:
            with self.subTest(char=char):
                self.assertEqual(to_latin1_safe(char)[0], expected)

    def test_replacements_are_reported(self):
        _, replaced = to_latin1_safe('a — b → c')
        self.assertEqual(replaced, [('—', '--'), ('→', '->')])

    def test_result_is_always_latin1_encodable(self):
        text, _ = to_latin1_safe('—→✓中文\U0001f600')
        text.encode('iso-8859-1')          # must not raise

    def test_unmappable_becomes_a_question_mark(self):
        self.assertEqual(to_latin1_safe('中')[0], '?')

    def test_accented_character_decomposes(self):
        self.assertEqual(to_latin1_safe('ā')[0], 'a')   # a with macron


class TestUpload(NoteTestCase):

    def upload(self, logbook=None, note=NOTE, **kwargs):
        path = self.write_note(note)
        kwargs.setdefault('inline_images', False)
        uploader = MarkdownUploader(logbook or _FakeLogbook(), **kwargs)
        return uploader, uploader.upload(path)

    def test_posts_once_as_html(self):
        logbook = _FakeLogbook()
        _, result = self.upload(logbook)
        self.assertEqual(len(logbook.posts), 1)
        self.assertEqual(logbook.posts[0]['encoding'], 'HTML')
        self.assertEqual(result.msg_id, 91)

    def test_body_is_converted(self):
        logbook = _FakeLogbook()
        self.upload(logbook)
        self.assertIn('<h2>Setup</h2>', logbook.posts[0]['message'])
        self.assertIn('<strong>laser</strong>', logbook.posts[0]['message'])

    def test_attributes(self):
        logbook = _FakeLogbook()
        self.upload(logbook)
        attributes = logbook.posts[0]['attributes']
        self.assertEqual(attributes['Subject'], 'Laser zero-finding')
        self.assertEqual(attributes['Type'], 'Hardware | Calibration')
        self.assertEqual(attributes['Author'], 'Johannes')
        self.assertNotIn('When', attributes)

    def test_attachments_are_passed_as_strings(self):
        # _prepare_attachments rejects Path with LogbookInvalidAttachmentType.
        self.write_file('plot.png')
        logbook = _FakeLogbook()
        self.upload(logbook)
        sent = logbook.posts[0]['attachments']
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(isinstance(a, str) for a in sent))

    def test_subject_from_filename_when_absent(self):
        logbook = _FakeLogbook()
        path = self.write_note('body only\n', name='20260730-pos23.md')
        MarkdownUploader(logbook, author='A', tags=['General'],
                         inline_images=False).upload(path)
        self.assertEqual(logbook.posts[0]['attributes']['Subject'], '20260730-pos23')

    def test_author_is_prompted_when_absent(self):
        logbook = _FakeLogbook()
        path = self.write_note('---\ntags: [General]\n---\nbody\n')
        MarkdownUploader(logbook, inline_images=False,
                         input_fn=lambda _: 'Guandi').upload(path)
        self.assertEqual(logbook.posts[0]['attributes']['Author'], 'Guandi')

    def test_tags_are_prompted_when_absent(self):
        logbook = _FakeLogbook()
        path = self.write_note('---\nauthor: A\n---\nbody\n')
        MarkdownUploader(logbook, inline_images=False,
                         input_fn=lambda _: 'hardware, general').upload(path)
        self.assertEqual(logbook.posts[0]['attributes']['Type'], 'Hardware | General')

    def test_note_with_no_frontmatter_prompts_for_both(self):
        logbook = _FakeLogbook()
        path = self.write_note('# Hand written\n\ntext\n', name='freehand.md')
        answers = iter(['Guandi', 'general'])
        MarkdownUploader(logbook, inline_images=False,
                         input_fn=lambda _: next(answers)).upload(path)
        attributes = logbook.posts[0]['attributes']
        self.assertEqual(attributes['Author'], 'Guandi')
        self.assertEqual(attributes['Type'], 'General')
        self.assertEqual(attributes['Subject'], 'freehand')

    def test_transliteration_warns_and_is_reported(self):
        logbook = _FakeLogbook()
        note = NOTE.replace('reflection mode', 'reflection mode — 45° → ok')
        with self.assertWarns(UserWarning):
            _, result = self.upload(logbook, note=note)
        self.assertTrue(result.transliterated)
        logbook.posts[0]['message'].encode('iso-8859-1')     # must not raise

    def test_unresolved_link_warns_and_is_reported(self):
        logbook = _FakeLogbook()
        with self.assertWarns(UserWarning):
            _, result = self.upload(logbook)                 # plot.png does not exist
        self.assertEqual(result.unresolved, ['plot.png'])
        self.assertEqual(logbook.posts[0]['attachments'], [])

    def test_exported_note_warns_about_creating_a_new_entry(self):
        logbook = _FakeLogbook()
        note = '---\nid: 89\nsubject: S\nauthor: A\ntags: [General]\n---\nbody\n'
        with self.assertWarns(UserWarning):
            self.upload(logbook, note=note)

    def test_missing_file(self):
        uploader = MarkdownUploader(_FakeLogbook())
        self.assertRaises(LogbookUploadError, uploader.upload, self.dir / 'nope.md')

    def test_posts_a_new_entry_not_an_edit(self):
        logbook = _FakeLogbook()
        self.upload(logbook)
        self.assertIsNone(logbook.posts[0]['msg_id'])


class TestInlineImages(NoteTestCase):

    def test_links_are_rewritten_to_the_stored_names(self):
        self.write_file('plot.png')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_plot.png'])
        path = self.write_note()
        result = MarkdownUploader(logbook).upload(path)

        self.assertTrue(result.inlined)
        self.assertEqual(len(logbook.posts), 2)
        edit = logbook.posts[1]
        self.assertEqual(edit['msg_id'], 91)
        self.assertIn('260903_110412_plot.png', edit['message'])
        self.assertIn('<img', edit['message'])

    def test_filename_with_a_space_still_maps(self):
        # _prepare_attachments replaces spaces with underscores when storing.
        self.write_file('my plot.png')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_my_plot.png'])
        path = self.write_note(NOTE.replace('plot.png', 'my plot.png'))
        result = MarkdownUploader(logbook).upload(path)

        self.assertTrue(result.inlined)
        self.assertIn('260903_110412_my_plot.png', logbook.posts[1]['message'])

    def test_non_image_becomes_a_link_not_an_embed(self):
        self.write_file('data.csv')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_data.csv'])
        path = self.write_note(NOTE.replace('![[plot.png]]', '[[data.csv]]'))
        MarkdownUploader(logbook).upload(path)
        edit = logbook.posts[1]['message']
        self.assertIn('<a href="260903_110412_data.csv"', edit)
        self.assertNotIn('<img src="260903_110412_data.csv"', edit)

    def test_edit_sends_no_attachments(self):
        # The edit branch rebuilds attachment0..N from the server, so resending would
        # be wasteful; and passing none must not remove them.
        self.write_file('plot.png')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_plot.png'])
        MarkdownUploader(logbook).upload(self.write_note())
        self.assertEqual(logbook.posts[1]['attachments'], [])

    def test_read_failure_still_returns_the_msg_id(self):
        # The entry already exists; raising would invite a duplicate on retry.
        self.write_file('plot.png')
        logbook = _FakeLogbook(stored=[], read_error=LogbookServerProblem('boom'))
        with self.assertWarns(UserWarning):
            result = MarkdownUploader(logbook).upload(self.write_note())
        self.assertEqual(result.msg_id, 91)
        self.assertFalse(result.inlined)

    def test_edit_failure_still_returns_the_msg_id(self):
        self.write_file('plot.png')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_plot.png'],
                               post_error=LogbookServerProblem('boom'))
        with self.assertWarns(UserWarning):
            result = MarkdownUploader(logbook).upload(self.write_note())
        self.assertEqual(result.msg_id, 91)
        self.assertFalse(result.inlined)

    def test_no_attachments_means_no_second_post(self):
        logbook = _FakeLogbook()
        path = self.write_note('---\nauthor: A\ntags: [General]\n---\nno links\n')
        result = MarkdownUploader(logbook).upload(path)
        self.assertEqual(len(logbook.posts), 1)
        self.assertFalse(result.inlined)

    def test_inline_images_can_be_disabled(self):
        self.write_file('plot.png')
        logbook = _FakeLogbook(stored=[BASE + '260903_110412_plot.png'])
        MarkdownUploader(logbook, inline_images=False).upload(self.write_note())
        self.assertEqual(len(logbook.posts), 1)


class TestUploadResult(unittest.TestCase):

    def test_summary(self):
        result = UploadResult(msg_id=91, attachments=[Path('a.png')],
                              unresolved=['b.png'], transliterated=[('—', '--')],
                              inlined=True)
        self.assertIn('entry 91', result.summary())
        self.assertIn('1 attachment', result.summary())
        self.assertIn('inline images yes', result.summary())


if __name__ == '__main__':
    warnings.simplefilter('always')
    unittest.main()
