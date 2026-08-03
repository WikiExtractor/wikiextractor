"""
Tests for the template self-inclusion loop guard in Extractor.expandTemplate().

Background
----------
wikiextractor bounds template expansion recursion by *depth*
(maxTemplateRecursionLevels), but has no protection against a template
whose own body re-invokes itself. On real wikis this happens in practice
when a template's documentation subpage contains worked usage examples
that literally re-invoke the template with fixed, hardcoded parameters
(see e.g. the Urdu Wikipedia "Redirect-multi" template). Each level of
expansion re-encounters those same examples, branching combinatorially
(e.g. 4 examples per level -> ~4^30 expansions), which never completes in
practice even though it is technically bounded by recursion depth.

The fix in expandTemplate() detects when a template title is about to be
invoked again with the *same* parameters as an active ancestor in
self.frame -- genuine zero-progress recursion -- and stops immediately
instead of re-expanding, mirroring MediaWiki's own "Template loop
detected" behavior.

Crucially, the guard compares (title, params), NOT title alone. Comparing
title alone breaks a common, legitimate MediaWiki idiom: a template that
recurses into itself with *different* (e.g. progressively shrinking)
parameters to process a variable-length list of arguments (templates have
no native loop construct). That recursion makes real progress each step
and terminates on its own -- it must not be blocked. An earlier version
of this fix that compared title alone was verified to turn correct output
into completely empty output for exactly this pattern; that regression
is captured here as test_legitimate_self_recursion_is_not_blocked.

Run with:
    python -m unittest tests.test_template_loop_guard -v
or, from the tests/ directory:
    python -m unittest test_template_loop_guard -v
"""

import logging
import sys
import time
import unittest
from io import StringIO

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class TemplateLoopGuardTestCase(unittest.TestCase):
    """Base class that resets wikiextractor's module-level template state
    before each test. `templates`, `templateCache`, and `redirects` are
    plain module globals in extract.py (populated by define_template),
    so tests must not leak state into one another.
    """

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        # Normally set by load_templates() when scanning a real dump's
        # first Template-namespace page; tests define templates directly
        # via define_template(), so it must be set explicitly here.
        ex.Extractor.templatePrefix = "Template:"
        # Normally set by WikiExtractor.py's main() (Extractor.to_json =
        # args.json) before any Extractor.extract() call; tests that call
        # .extract() directly need it set too, or it raises AttributeError
        # (note: the class also defines an unused `toJson` attribute --
        # a pre-existing, unrelated naming mismatch in extract.py).
        ex.Extractor.to_json = False

    def make_extractor(self, article_text, article_id=1, title="Test Article"):
        return Extractor(article_id, str(article_id),
                          f"https://test.wikipedia.org/wiki?curid={article_id}",
                          title, [article_text])


class LegitimateSelfRecursionTests(TemplateLoopGuardTestCase):
    """A template recursing into itself with different/shrinking
    parameters is a real MediaWiki idiom (there is no native loop
    construct) and must fully resolve, not be blocked.
    """

    def test_legitimate_self_recursion_is_not_blocked(self):
        # Template:P2 formats its first argument, then recurses on the
        # rest of the argument list, shifted left by one, terminating
        # when no more arguments remain. Each recursive call uses
        # DIFFERENT parameters than its caller -- genuine progress.
        ex.define_template(
            "Template:P2",
            ["{{{1}}}{{#if:{{{2|}}}|, {{P2|{{{2|}}}|{{{3|}}}|{{{4|}}}|{{{5|}}}}}|}}"]
        )
        article_text = "Population list: {{P2|CityA|CityB|CityC|CityD}}"
        extractor = self.make_extractor(article_text)

        cleaned = extractor.clean_text(article_text, expand_templates=True)

        self.assertIn("CityA, CityB, CityC, CityD", cleaned[0])
        self.assertEqual(extractor.template_loop_errs, 0,
                          "legitimate varying-parameter recursion must not "
                          "be flagged as a loop")

    def test_deeper_legitimate_recursion_terminates(self):
        # A longer list, to make sure the guard doesn't fire partway
        # through a longer (but still legitimate, still-progressing)
        # recursive chain.
        ex.define_template(
            "Template:P2",
            ["{{{1}}}{{#if:{{{2|}}}|, {{P2|{{{2|}}}|{{{3|}}}|{{{4|}}}|{{{5|}}}"
             "|{{{6|}}}|{{{7|}}}|{{{8|}}}}}|}}"]
        )
        article_text = ("{{P2|A|B|C|D|E|F|G|H}}")
        extractor = self.make_extractor(article_text)

        cleaned = extractor.clean_text(article_text, expand_templates=True)

        self.assertIn("A, B, C, D, E, F, G, H", cleaned[0])
        self.assertEqual(extractor.template_loop_errs, 0)


class PathologicalSelfReferenceTests(TemplateLoopGuardTestCase):
    """A template whose stored body contains fixed, hardcoded
    re-invocations of itself (e.g. worked examples from a documentation
    block that wasn't properly stripped) makes zero progress on each
    repeat and must be caught quickly rather than left to branch
    combinatorially for up to maxTemplateRecursionLevels.
    """

    def test_self_referencing_doc_examples_are_caught(self):
        # Mirrors the real-world case: Template:Redirect-multi's stored
        # body contains several literal re-invocations of itself with
        # fixed example parameters (as if from an unstripped /doc
        # section). Without the guard this branches ~4x per level for
        # up to 30 levels and never completes in practice.
        #
        # Note: the guard bounds this to a fast, finite amount of work by
        # cutting off each distinct (title, params) branch as soon as it
        # repeats -- it does not guarantee minimal or "clean" output for
        # the pathological template itself (some repeated fragments from
        # the doc examples may still appear before each branch is cut
        # off). What matters is that it completes quickly and the real
        # surrounding article content survives intact.
        ex.define_template("Template:Redirect-multi", ["""A redirect hatnote for {{{1}}}.

Usage examples:
{{Redirect-multi|3|A|B|C}}
{{Redirect-multi|3|A|B|C|use=x}}
{{Redirect-multi|3|A|B|C|use=y}}
{{Redirect-multi|3|A|B|C|use=z}}
"""])
        article_text = "Some article text.\n\n{{Redirect-multi|2|X|Y}}\n\nMore text."
        extractor = self.make_extractor(article_text)

        start = time.time()
        cleaned = extractor.clean_text(article_text, expand_templates=True)
        elapsed = time.time() - start

        self.assertGreater(extractor.template_loop_errs, 0,
                            "self-referencing doc examples should be detected")
        # Generous bound: this should complete in well under a second.
        # Without the guard, this pattern does not complete in any
        # practical amount of time (bounded only by maxTemplateRecursionLevels,
        # ~4**30 expansions in the worst case).
        self.assertLess(elapsed, 5.0,
                         "template with self-referencing doc examples took "
                         "too long -- loop guard may not be firing")
        full_text = "\n".join(cleaned)
        self.assertIn("Some article text.", full_text)
        self.assertIn("More text.", full_text)

    def test_direct_immediate_self_reference_is_caught(self):
        # The simplest possible case: a template whose body invokes
        # itself with the exact same parameters, unconditionally.
        ex.define_template("Template:Self", ["before {{Self|x}} after"])
        article_text = "{{Self|x}}"
        extractor = self.make_extractor(article_text)

        start = time.time()
        extractor.clean_text(article_text, expand_templates=True)
        elapsed = time.time() - start

        self.assertGreater(extractor.template_loop_errs, 0)
        self.assertLess(elapsed, 5.0)


class WarningLogDeduplicationTests(TemplateLoopGuardTestCase):
    """The per-occurrence warning must be logged once per (article,
    title), not once per detected repeat -- a single stuck article can
    otherwise generate hundreds of identical log lines.
    """

    def test_warning_logged_once_despite_many_repeats(self):
        ex.define_template("Template:Redirect-multi", ["""A redirect hatnote for {{{1}}}.

{{Redirect-multi|3|A|B|C}}
{{Redirect-multi|3|A|B|C|use=x}}
{{Redirect-multi|3|A|B|C|use=y}}
{{Redirect-multi|3|A|B|C|use=z}}
"""])
        article_text = "{{Redirect-multi|2|X|Y}}"
        extractor = self.make_extractor(article_text)

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        original_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.WARNING)
        try:
            extractor.clean_text(article_text, expand_templates=True)
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(original_level)

        log_output = log_stream.getvalue()
        occurrences = log_output.count("Template loop detected")

        self.assertGreater(extractor.template_loop_errs, 1,
                            "test setup should produce multiple detections")
        self.assertEqual(occurrences, 1,
                          "warning should be logged exactly once per "
                          "(article, title) regardless of total repeat count")


class ErrorSummaryReportingTests(TemplateLoopGuardTestCase):
    """The per-article error summary line (used for the title/recursion
    counters) must also include the new loop counter.
    """

    def test_loop_errs_included_in_summary_when_present(self):
        ex.define_template("Template:Self", ["{{Self|x}}"])
        article_text = "{{Self|x}}"
        extractor = self.make_extractor(article_text)

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        original_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.WARNING)
        try:
            out = StringIO()
            extractor.extract(out, html_safe=True)
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(original_level)

        log_output = log_stream.getvalue()
        self.assertIn("loop(", log_output,
                      "per-article error summary should report the loop count")

    def test_no_spurious_errors_reported_for_clean_article(self):
        ex.define_template("Template:Plain", ["just plain text"])
        article_text = "{{Plain}}"
        extractor = self.make_extractor(article_text)

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        original_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.WARNING)
        try:
            out = StringIO()
            extractor.extract(out, html_safe=True)
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(original_level)

        self.assertEqual(log_stream.getvalue(), "",
                          "a clean article with no template errors should "
                          "produce no warnings")
        self.assertEqual(extractor.template_loop_errs, 0)


if __name__ == '__main__':
    unittest.main()
