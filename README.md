[![conda_publish](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/conda_publish.yaml/badge.svg)](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/conda_publish.yaml)
[![pypi_publish](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/pypi_publish.yaml/badge.svg)](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/pypi_publish.yaml)
[![python_test](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/python_test.yaml/badge.svg)](https://github.com/paulscherrerinstitute/py_elog/actions/workflows/python_test.yaml)

# Overview
This Python module provides a native interface [electronic logbooks](https://midas.psi.ch/elog/). It is compatible with Python versions 3.5 and higher.

# Usage

For accessing a logbook at ```http[s]://<hostename>:<port>/[<subdir>/]<logbook>/[<msg_id>]``` a logbook handle must be retrieved.

```python
import elog

# Open GFA SwissFEL test logbook
logbook = elog.open('https://elog-gfa.psi.ch/SwissFEL+test/')

# Contstructor using detailed arguments
# Open demo logbook on local host: http://localhost:8080/demo/
logbook = elog.open('localhost', 'demo', port=8080, use_ssl=False)
```

Once you have hold of the logbook handle one of its public methods can be used to read, create, reply to, edit or delete the message.

## Configuration File

Rather than hardcoding the connection parameters and credentials, the connection can be
described in a YAML file and the credentials prompted for on the terminal:

```yaml
# elog.yaml
hostname: https://elog.psi.ch   # required; a bare host is fine, the scheme is added
logbook: Linux+Demo             # optional, default ''
subdir: elogs                   # optional, default ''
port: 8080                      # optional, default 80 (http) / 443 (https)
use_ssl: true                   # optional, default true
```

```python
from elog.logbook_md import open_from_config

# Prompts for the username and password, then returns a normal Logbook
logbook = open_from_config('elog.yaml')
logbook.post('This is message text', author='me', type='Routine')
```

Credentials are never read from the config file, so it is safe to commit. See
[elog.example.yaml](elog.example.yaml) for an annotated template.

For more control, use the underlying pieces directly:

```python
from elog.logbook_md import load_config, CredentialPrompter, LogbookSession

config = load_config('elog.yaml')            # parse + validate, no I/O beyond the read
session = LogbookSession(config, prompter=CredentialPrompter())
logbook = session.connect_interactive()      # verifies, re-prompting on auth failure
```

Unknown or conflicting keys are reported rather than silently ignored, and the config
layer fills in a missing URL scheme — without it, a bare `hostname` produces the URL
`https:///<logbook>/`, which only fails later with an opaque error.

## Export an Entry as Markdown

An entry can be exported as a Markdown note with YAML frontmatter, suitable for an
Obsidian vault:

```python
from elog.logbook_md import export_from_config

export_from_config('elog.yaml', 89, 'elog_export')   # writes elog_export/0089.md
```

Or against a logbook you already hold:

```python
from elog.logbook_md import MarkdownExporter

exporter = MarkdownExporter(logbook)
exporter.to_markdown_file(89, 'elog_export')   # -> Path('elog_export/0089.md')
document = exporter.to_markdown(89)            # in memory, no file written
```

The three parts of `logbook.read(89)` map onto the note as follows:

| `read()` returns | becomes |
| --- | --- |
| `attributes` | YAML frontmatter — `Type` becomes an Obsidian `tags` list, `Date` an ISO 8601 timestamp, `$@MID@$` an integer `id`. Attributes the library does not know are kept under a slugified key rather than dropped. |
| `message` | the note body — HTML is converted with [markdownify](https://pypi.org/project/markdownify/), ELCode gets a best-effort conversion, plain text passes through. The `Encoding` attribute selects the branch; when it is missing the content is sniffed. |
| `attachments` | by default the server URLs are listed in the frontmatter and nothing is downloaded; pass a `DownloadingAttachmentHandler` to fetch them (below). |

The filename is the message ID zero-padded to four digits, so notes sort correctly.

```markdown
---
id: 89
date: '2026-08-31T11:45:14+02:00'
author: NotMe
subject: Measurement of test setup Number 1919
tags:
  - Hardware
  - Calibration
attachments:
  - https://elog.example.org:8080/Positioners/260828_172838_plot.png
---

## Setup

Ran the **laser** in reflection mode.

![](260828_172838_plot.png)
```

### Attachments

Attachments are downloaded by default, into an `attachments/` folder beside the notes,
and linked as Obsidian wikilinks. This applies to single and batch exports alike:

```python
MarkdownExporter(logbook).to_markdown_file(89, 'elog_export')
MarkdownExporter(logbook).export_all('elog_export')
```

```
elog_export/
  0089.md
  attachments/260828_193233_Laser_OnVGroove.JPG
  attachments/260828_193337_20260828_Laser_ZeroFinding.pdf
```

```markdown
attachments:
  - attachments/260828_193233_Laser_OnVGroove.JPG
  - attachments/260828_193337_20260828_Laser_ZeroFinding.pdf
---

# Prologue

Tested the laser in **reflection mode**.

## Attachments

![[260828_193233_Laser_OnVGroove.JPG]]
[[260828_193337_20260828_Laser_ZeroFinding.pdf]]
```

- **The server filename is kept verbatim**, timestamp prefix and all. That prefix is
  what makes the name unique — Obsidian resolves wikilinks by filename across the whole
  vault, so two entries each attaching a `Screen.png` would otherwise collide into one
  file and an ambiguous link.
- **Images are embedded** with `![[name]]`; PDFs, CSVs and anything else get a plain
  `[[name]]` link so they do not render as a giant inline preview.
- **Most entries never reference their attachments in the body** — the files are simply
  attached — so the links are appended as an `## Attachments` section. An attachment
  that *is* referenced inline (including ELOG's linked-thumbnail markup) is rewritten in
  place instead and left out of that section.
- **Already-downloaded files are skipped** on a re-run, since ELOG's timestamped names
  are effectively immutable. Pass `skip_existing=False` to force a re-fetch.
- A file that cannot be downloaded leaves its URL in the frontmatter and warns; the rest
  of the note is still written.

To turn downloading **off** and keep only the remote URLs in the frontmatter, pass the
link-only base handler:

```python
from elog.logbook_md import AttachmentHandler

MarkdownExporter(logbook, attachments=AttachmentHandler())   # no downloads at all
```

To tune it, pass a configured handler:

```python
from elog.logbook_md import DownloadingAttachmentHandler

MarkdownExporter(logbook, attachments=DownloadingAttachmentHandler(
    subdir='assets', skip_existing=False, heading='## Files'))
```

The same applies from a config file, which prompts once and exports everything:

```python
export_all_from_config('elog.yaml', 'elog_export', progress=print_progress)
```

__Note:__ a first full export downloads every attachment in the logbook, so expect it to
take noticeably longer and use disk than a metadata-only run. Later runs re-download
nothing, because the timestamped filenames are stable and already-present files are
skipped.

## Export the Whole Logbook

Exports every entry, prompting for credentials **once** regardless of how many entries
there are:

```python
from elog.logbook_md import export_all_from_config, print_progress

result = export_all_from_config('elog.yaml', 'elog_export', progress=print_progress)
print(result.summary())      # '412 exported, 0 skipped, 3 failed (of 415)'
```

Or against a logbook you already hold:

```python
exporter = MarkdownExporter(logbook)
exporter.list_message_ids()                       # every id, ascending
exporter.export_all('elog_export')                # -> BatchResult
exporter.export_all('elog_export', msg_ids=[1, 2, 89])
exporter.export_all('elog_export', skip_existing=True)   # resume an interrupted run
```

IDs are discovered with `search('', n_results=1_000_000)` rather than
`get_message_ids()`. Only `search` passes `npp` (entries per page) to the server;
`get_message_ids()` requests `<url>page` with no page size and so returns just one
page, which would silently truncate the batch on a large logbook. Deleted entries never
appear in the listing, so every discovered id is valid.

A failing entry is recorded and the batch continues — one unreadable entry should not
cost you the rest. Pass `stop_on_error=True` for the strict behaviour. The returned
`BatchResult` carries `exported` (`[(msg_id, Path)]`), `skipped`, and `failed`
(`[(msg_id, exception)]` — the exception object, not a string), and is falsey when
anything failed:

```python
result = exporter.export_all('elog_export')
if not result:
    for msg_id, error in result.failed:
        print(msg_id, error)
```

By default every entry is re-exported so edits made on the server propagate. Use
`skip_existing=True` to only fetch entries with no `.md` file yet.

## Upload a Markdown Note to ELOG

The reverse of the export: point at one hand-written Markdown note and it becomes a new
ELOG entry, with its linked images attached and embedded.

```python
from elog.logbook_md import upload_from_config

result = upload_from_config('elog.yaml', 'D:/notes/20260730-pos23.md')
print(result.summary())
# entry 91: 1 attachment(s), 0 unresolved link(s), 3 character(s) transliterated,
# inline images yes
```

On Windows, resolve a pasted path first — see
[`elog.path_utils_win`](elog/path_utils_win.py):

```python
import os
from elog.path_utils_win import to_path

raw = input('Markdown file: ')
note = to_path(raw, must_exist=True) if os.name == 'nt' else Path(raw.strip())
```

Frontmatter maps onto ELOG attributes:

| Frontmatter | ELOG | Rule |
| --- | --- | --- |
| `subject` | `Subject` | falls back to the note's **filename stem** |
| `tags` | `Type` | joined with ` \| `; **prompted** (comma-separated) when absent |
| `author` | `Author` | **prompted** when absent |
| — | `When` | not sent; ELOG stamps the current time on a new entry |

Tags are capitalised on their first letter only, so `adc` becomes `Adc` but `ADC` stays
`ADC`. Bookkeeping keys the exporter writes (`id`, `date`, `attachments`, `reply_to`) are
dropped; a note carrying `id` warns that upload creates a **new** entry rather than
updating that one. Anything else needs `extra_attributes={...}`.

The body is rendered to HTML (python-markdown: fenced code, tables, sane lists) and posted
with `encoding='HTML'`, so the exporter converts it back to equivalent Markdown.

Linked files — `![[x.png]]`, `[[x.png]]`, `![](x.png)`, `[](x.png)` — are resolved beside
the note, then in `<note_dir>/attachments/`. External URLs are skipped; links pointing at
nothing are reported in `result.unresolved` rather than silently dropped.

__Note:__ ELOG stores the body and every attribute as latin-1, which cannot represent en
and em dashes, `−`, `≈`, `→` or `✓` — characters ordinary lab notes pick up. Those are
transliterated (`—`→`--`, `≈`→`~=`, `→`→`->`), reported in `result.transliterated`, and
warned about once. Without this, `post()` raises `UnicodeEncodeError` mid-upload.

### Inline images

ELOG renames each upload to `<YYMMDD>_<HHMMSS>_<name>` and only reveals that name *after*
the post, so linking images inline takes two steps: post, read the stored names back, then
edit the body. This is automatic; pass `inline_images=False` to skip it, leaving the files
attached but not embedded.

Because the entry already exists once the first post returns, a failure in that second step
never raises — `upload()` returns the `msg_id` with `inlined=False` and warns, so a retry
cannot create a duplicate.

```python
from elog.logbook_md import MarkdownUploader

uploader = MarkdownUploader(logbook, author='Guandi', tags=['Hardware'])
result = uploader.upload('D:/notes/20260730-pos23.md')
```

Uploading is one note per call, always as a new entry. Replying to or editing an existing
entry is not wired up yet.

## Get Existing Message Ids
Get all the existing message ids of a logbook

```python
message_ids = logbook.get_message_ids()
```

To get if of the last inserted message
```python
last_message_id = logbook.get_last_message_id()
```

## Read Message

```python
# Read message with with message ID = 23
message, attributes, attachments = logbook.read(23)
```

## Create Message

```python
# Create new message with some text, attributes (dict of attributes + kwargs) and attachments
new_msg_id = logbook.post('This is message text', attributes=dict_of_attributes, attachments=list_of_attachments,
                          attribute_as_param='value')
```
 
What attributes are required is determined by the configuration of the elog server (keywork `Required Attributes`).
If the configuration looks like this:
 
```
Required Attributes = Author, Type
```
 
You have to provide author and type when posting a message.
 
In case type need to be specified, the supported keywords can as well be found in the elog configuration with the key `Options Type`.
 
If the config looks like this:
```
Options Type = Routine, Software Installation, Problem Fixed, Configuration, Other
```

A working create call would look like this:

```python
new_msg_id = logbook.post('This is message text', author='me', type='Routine')
```

 

## Reply to Message

```python
# Reply to message with ID=23
new_msg_id = logbook.post('This is a reply', msg_id=23, reply=True, attributes=dict_of_attributes,
                          attachments=list_of_attachments, attribute_as_param='value')
```

## Edit Message

```python
# Edit message with ID=23. Changed message text, some attributes (dict of edited attributes + kwargs) and new attachments
edited_msg_id = logbook.post('This is new message text', msg_id=23, attributes=dict_of_changed_attributes,
                             attachments=list_of_new_attachments, attribute_as_param='new value')
```

## Search Messages

```python
# Search for text in messages or specify attributes for search, returns list of message ids
logbook.search('Hello World')
logbook.search('Hello World', n_results=20, scope='attribname')
logbook.search({'attribname': 'Hello World', ...})
```

## Delete Message (and all its replies)

```python
# Delete message with ID=23. All its replies will also be deleted.
logbook.delete(23)
```

__Note:__ Due to the way elog implements delete this function is only supported on english logbooks.

# Installation

Clone the repository and install it with pip:

```bash
git clone https://github.com/Zguandi/py_elog_msslab.git
cd py_elog_msslab
pip install .
```

For development, install in editable mode so changes to the source take effect
immediately:

```bash
pip install -e .
```

Python 3.10 or newer is required. Dependencies are declared in `pyproject.toml` and
installed automatically:

| Package | Used for |
| --- | --- |
| `requests` | http(s) communication |
| `passlib` | password encryption |
| `lxml` | parsing logbook listing pages |
| `PyYAML` | reading configuration files, writing note frontmatter |
| `markdownify` | converting HTML entries to Markdown |

## Installing into another project

To use this library from a project of your own, point the dependency at a local
checkout rather than PyPI. With [uv](https://docs.astral.sh/uv/):

```bash
uv add --editable ../path/to/py_elog_msslab
```

which records the checkout in your `pyproject.toml`:

```toml
[tool.uv.sources]
py-elog = { path = "../path/to/py_elog_msslab", editable = true }
```

The plain-pip equivalent is `pip install -e ../path/to/py_elog_msslab`, or install
straight from GitHub without a local clone:

```bash
pip install git+https://github.com/Zguandi/py_elog_msslab.git
```

# Running the Tests

```bash
pytest                      # offline tests only (tests/test_logbook_md.py)
ELOG_LIVE_TESTS=1 pytest    # additionally runs the live integration tests
```

__Note:__ `tests/test_logbook.py` runs against the real, shared, public logbook at
`https://elog.psi.ch/elogs/Linux+Demo/` and **writes** to it — it posts entries and
overwrites the body of the most recent message, which may belong to somebody else. It is
therefore excluded from collection unless `ELOG_LIVE_TESTS=1` is set. The offline suite
in `tests/test_logbook_md.py` never opens a socket.
