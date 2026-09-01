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
| `attachments` | **currently a stub** — the server URLs are listed in the frontmatter but nothing is downloaded. See `AttachmentHandler` for the seam that adds downloading. |

The filename is the message ID zero-padded to four digits, so notes sort correctly.

```markdown
---
id: 89
date: '2026-08-28T17:28:38+02:00'
author: Johannes
subject: Measurement with Laser in Reflection Mode with Sanded PTFE Screen
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
The Elog module depends on `passlib` and `requests` (password encryption and http(s) communication), `lxml` (parsing listing pages) and `PyYAML` (reading configuration files). It is packed as [anaconda package](https://anaconda.org/paulscherrerinstitute/elog) and can be installed as follows:

```bash
conda install -c paulscherrerinstitute elog
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
