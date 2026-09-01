"""Pytest configuration for the elog test suite.

test_logbook.py is a live integration suite: it posts entries to, and edits entries on,
the shared public logbook at https://elog.psi.ch/elogs/Linux+Demo/. Running it by
accident mutates a real archive that other people use, so it is excluded from
collection unless it is explicitly asked for:

    ELOG_LIVE_TESTS=1 pytest          # includes the live tests
    pytest                            # offline tests only

collect_ignore is used rather than a skip marker so the module is never even imported.
"""

import os

collect_ignore = [] if os.environ.get('ELOG_LIVE_TESTS') == '1' else ['test_logbook.py']
