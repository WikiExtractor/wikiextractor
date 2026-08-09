"""
Tests for extract_process()'s worker-level aggregate error summary.

Real, full-wiki runs can have a large fraction of all articles report
some template/#expr issue -- confirmed directly against a real
ur.wikipedia.org run: a single common, broken shared template (a
date-navigation infobox) reached enough articles that the per-article
WARNING-level summary line alone (extract.py's own "Template errors
in article...") produced over 160,000 log lines, drowning out
everything else in the output.

The fix: that per-article line moved to DEBUG (see
test_template_loop_guard.ErrorSummaryReportingTests, updated for
this), and extract_process() now accumulates the same six counters
(title, recursion x3, loop, expr) across every article a single
worker processes, logging ONE WARNING-level aggregate line per worker
when its job queue is exhausted -- a handful of lines total (one per
worker process) instead of one per problematic article, while still
preserving the total counts needed to gauge scope.

These tests call extract_process() directly, in-process, using plain
queue.Queue objects rather than spawning real OS processes -- it only
ever calls .get()/.put() on them, so a plain Queue is a faithful,
much faster stand-in for these purposes.

Run with:
    python -m unittest tests.test_extract_process_worker_summary -v
or, from the tests/ directory:
    python -m unittest test_extract_process_worker_summary -v
"""

import logging
import queue
import sys
import unittest
from io import StringIO

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex
import wikiextractor.WikiExtractor as we


def run_extract_process(jobs, templates=None, redirects=None, template_prefix=''):
    """Feeds `jobs` (a list of (id, revid, urlbase, title, page) tuples,
    ordinal assigned automatically) through the real extract_process(),
    then a sentinel None to make it exit its loop. Returns (log_output,
    output_queue_contents).
    """
    jobs_queue = queue.Queue()
    for ordinal, job in enumerate(jobs):
        jobs_queue.put(job + (ordinal,))
    jobs_queue.put(None)  # sentinel: tells the worker to stop

    output_queue = queue.Queue()
    extractor_kwargs = {'templatePrefix': template_prefix} if template_prefix else {}

    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.WARNING)
    try:
        we.extract_process(jobs_queue, output_queue, html_safe=True,
                            template_blob_names=None, redirects_blob_names=None,
                            extractor_kwargs={**extractor_kwargs, 'templates': templates,
                                               'redirects': redirects} if templates is not None
                            else extractor_kwargs)
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(original_level)

    outputs = []
    while not output_queue.empty():
        outputs.append(output_queue.get())
    return log_stream.getvalue(), outputs


class WorkerAggregateSummaryTests(unittest.TestCase):

    def test_worker_with_no_problem_articles_logs_nothing(self):
        jobs = [
            (1, "1", "https://x", "Clean Article", ["just plain text"]),
            (2, "2", "https://x", "Another Clean Article", ["more plain text"]),
        ]
        log_output, outputs = run_extract_process(jobs)
        self.assertEqual(log_output, "")
        self.assertEqual(len(outputs), 2)

    def test_worker_with_problem_articles_logs_exactly_one_aggregate_line(self):
        jobs = [
            (1, "1", "https://x", "Bad Article One", ["{{#expr: 1 + }}"]),
            (2, "2", "https://x", "Clean Article", ["just plain text"]),
            (3, "3", "https://x", "Bad Article Two", ["{{#expr: 2 + }}"]),
        ]
        log_output, outputs = run_extract_process(jobs)
        lines = [line for line in log_output.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1,
                          "one aggregate WARNING line per worker, not one per article")
        self.assertIn("2/3 articles had template errors", log_output)

    def test_aggregate_counts_sum_correctly_across_articles(self):
        # Two malformed #expr calls in the first article, one in the
        # second -- the aggregate expr(...) count should be 3, not 2
        # (the article count) or 1 (a single article's own count).
        jobs = [
            (1, "1", "https://x", "Bad Article One",
             ["{{#expr: 1 + }}", "{{#expr: 2 + }}"]),
            (2, "2", "https://x", "Bad Article Two", ["{{#expr: 3 + }}"]),
        ]
        log_output, outputs = run_extract_process(jobs)
        self.assertIn("expr(3)", log_output)

    def test_per_article_detail_not_present_at_worker_summary_warning_level(self):
        # The per-article "Template errors in article '...'" line
        # lives at DEBUG now (see test_template_loop_guard.py) -- it
        # must not also appear at WARNING, or the whole point of this
        # aggregation is defeated.
        jobs = [
            (1, "1", "https://x", "Bad Article One", ["{{#expr: 1 + }}"]),
        ]
        log_output, outputs = run_extract_process(jobs)
        self.assertNotIn("Template errors in article", log_output)
        self.assertIn("Worker pid=", log_output)


if __name__ == '__main__':
    unittest.main()
