"""
Tests for the try/finally guard around self.frame's append/pop pair in
expandTemplate() (extract.py).

self.frame is a stack tracking the currently-enclosing template
invocation(s) -- pushed right before expanding a template's body,
popped right after (see sharp_invoke()'s own use of frame[-1], and
test_sharp_invoke_alias.py). Previously, that pop() was a plain,
unguarded statement following the expansion call: if anything raised
during template.subst() or self.expandTemplates(), the pop() was
skipped, leaving a stale, un-popped entry on the stack rather than
restoring it to what it was before that invocation was attempted.

Given the current architecture (a brand-new Extractor instance per
page, never reused -- confirmed directly at both call sites in
WikiExtractor.py), an uncaught exception here aborts that page's
extract() call entirely, so this gap doesn't currently produce wrong
output in practice. But it's exactly the kind of invariant ("frame
always accurately reflects the current call stack") that's easy to
silently violate if the surrounding architecture ever changes --
instance reuse across pages, or an added catch-and-continue somewhere
in the middle of this chain -- so the try/finally protects the
invariant itself, not just today's specific call graph.

Run with:
    python -m unittest tests.test_frame_cleanup_on_exception -v
or, from the tests/ directory:
    python -m unittest test_frame_cleanup_on_exception -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class FrameCleanupOnExceptionTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_extractor(self, article_text):
        return Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                          "Test Article", [article_text])


class FrameStaysCleanAfterExceptionTests(FrameCleanupOnExceptionTestCase):

    def test_frame_is_empty_after_a_failing_template_expansion(self):
        # A poison template whose expansion is made to raise partway
        # through, simulating some genuine bug in subst()/
        # expandTemplates() that isn't a #invoke-specific failure
        # (those are already caught locally by callParserFunction's
        # own bare except, before ever reaching this append/pop pair).
        ex.define_template('Template:Poison', ['poisoned body'])

        extractor = self.get_extractor('Before {{Poison}} after')

        original_expand_templates = extractor.expandTemplates

        def poisoned_expand_templates(wikitext):
            if 'poisoned body' in wikitext:
                raise RuntimeError("simulated failure during expansion")
            return original_expand_templates(wikitext)

        extractor.expandTemplates = poisoned_expand_templates

        self.assertEqual(extractor.frame, [])
        with self.assertRaises(RuntimeError):
            extractor.clean_text('Before {{Poison}} after', expand_templates=True)

        # The key assertion: frame was pushed once, for the Poison
        # invocation, then the raise happened -- with the fix, it must
        # still have been correctly popped back to empty, not left
        # with that invocation's entry stranded on the stack.
        self.assertEqual(extractor.frame, [],
                          "frame was left dirty after an exception during expansion")

    def test_frame_stays_correct_for_a_sibling_invocation_after_a_prior_failure(self):
        # Belt-and-suspenders version of the above: after a failed
        # expansion (caught at the test level, simulating some
        # hypothetical future caller that catches and continues),
        # a LATER, unrelated template invocation on the same
        # extractor must not see any leftover state from the failed
        # one. This only matters if some future change catches such
        # an exception and continues on the same Extractor instance --
        # not true today, but this is what the fix actually protects
        # against.
        ex.define_template('Template:Poison', ['poisoned body'])
        ex.define_template('Template:Fine', ['fine body'])

        extractor = self.get_extractor('x')
        original_expand_templates = extractor.expandTemplates

        def poisoned_expand_templates(wikitext):
            if 'poisoned body' in wikitext:
                raise RuntimeError("simulated failure during expansion")
            return original_expand_templates(wikitext)

        extractor.expandTemplates = poisoned_expand_templates

        try:
            extractor.expandTemplate('Poison')
        except RuntimeError:
            pass

        self.assertEqual(extractor.frame, [])

        # a subsequent, unrelated invocation should behave normally,
        # with no leftover frame entry from the earlier failure
        result = extractor.expandTemplate('Fine')
        self.assertEqual(result, 'fine body')
        self.assertEqual(extractor.frame, [])


if __name__ == '__main__':
    unittest.main()
