"""
Tests for the DETAIL log level -- a custom level between INFO (20)
and DEBUG (10), registered via logging.addLevelName() in extract.py.

Motivation: extract.py's per-article "Template errors in article..."
summary needed a home between two extremes. WARNING was too broad --
on a real, full-wiki run, a single common broken shared template can
leave a large fraction of all articles with some nonzero error count,
turning "one WARNING line per article" into hundreds of thousands of
lines (see test_extract_process_worker_summary.py for the aggregate
fix that came first). But DEBUG was too narrow a gate to put it
behind -- DEBUG in extract.py also includes low-level extraction-
mechanics tracing (template invocation, parameter substitution) that
is entirely unrelated to "which pages have errors" and vastly more
voluminous, confirmed directly: on a small, 313-page real-data test,
--debug produced 76,202 log lines versus --verbose's 16.

DETAIL is that middle ground: enabled via WikiExtractor.py's
--verbose flag, shows per-article error detail without the mechanics
tracing that only --debug enables.
"""

import logging
import sys
import unittest
from io import StringIO

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class DetailLevelDefinitionTests(unittest.TestCase):

    def test_detail_is_between_info_and_debug(self):
        self.assertLess(logging.DEBUG, ex.DETAIL)
        self.assertLess(ex.DETAIL, logging.INFO)

    def test_detail_has_a_registered_level_name(self):
        # Confirms addLevelName() was actually called -- otherwise a
        # DETAIL-level record renders with a bare numeric level
        # ("Level 15: ...") instead of "DETAIL: ...".
        self.assertEqual(logging.getLevelName(ex.DETAIL), 'DETAIL')


class DetailLevelFilteringTests(unittest.TestCase):
    """Confirms each threshold shows exactly what it should: the
    per-article summary is reachable at DETAIL without requiring full
    DEBUG, and stays hidden at the plain INFO default.
    """

    def make_extractor(self):
        return ex.Extractor(1, "1", "https://x", "Test Article", ["{{BadExpr}}"],
                             templates={'Template:BadExpr': '{{#expr: 1 + }}'},
                             templatePrefix='Template:')

    def run_at_level(self, level):
        extractor = self.make_extractor()
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        # 'wikiextractor.extract' specifically, not the root logger --
        # wikiextractor's own logging no longer touches the root
        # logger at all (see configure_wikiextractor_logging() in
        # WikiExtractor.py).
        extract_logger = logging.getLogger('wikiextractor.extract')
        original_level = extract_logger.level
        original_propagate = extract_logger.propagate
        extract_logger.addHandler(handler)
        extract_logger.setLevel(level)
        # Capture only: with propagate left on, a handler that another
        # test file put on an ancestor logger also renders every record
        # produced here, to stderr.
        extract_logger.propagate = False
        try:
            out = StringIO()
            extractor.extract(out, html_safe=True)
        finally:
            extract_logger.removeHandler(handler)
            extract_logger.setLevel(original_level)
            extract_logger.propagate = original_propagate
        return log_stream.getvalue()

    def test_not_shown_at_default_info_level(self):
        log_output = self.run_at_level(logging.INFO)
        self.assertNotIn("Template errors in article", log_output)

    def test_shown_at_detail_level(self):
        log_output = self.run_at_level(ex.DETAIL)
        self.assertIn("Template errors in article", log_output)

    def test_shown_at_debug_level_too(self):
        # DEBUG (10) is more permissive than DETAIL (15), so anything
        # visible at DETAIL must still be visible at DEBUG.
        log_output = self.run_at_level(logging.DEBUG)
        self.assertIn("Template errors in article", log_output)

    def test_extraction_mechanics_tracing_not_shown_at_detail_level(self):
        # The actual point of DETAIL existing as a separate level from
        # DEBUG: low-level invocation/substitution tracing must NOT
        # leak through at DETAIL, only the per-article summary should.
        templates = {'Template:Simple': 'hello world'}
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                  templates=templates, templatePrefix='Template:')
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        extract_logger = logging.getLogger('wikiextractor.extract')
        original_level = extract_logger.level
        original_propagate = extract_logger.propagate
        extract_logger.addHandler(handler)
        extract_logger.setLevel(ex.DETAIL)
        extract_logger.propagate = False
        try:
            extractor.clean_text('{{Simple}}', expand_templates=True)
        finally:
            extract_logger.removeHandler(handler)
            extract_logger.setLevel(original_level)
            extract_logger.propagate = original_propagate
        log_output = log_stream.getvalue()
        self.assertNotIn("INVOCATION", log_output)
        self.assertNotIn("TITLE", log_output)


if __name__ == '__main__':
    unittest.main()
