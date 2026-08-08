"""
Tests for sharp_expr()'s #expr evaluator in extract.py: confirms it
cannot execute arbitrary code, only the narrow arithmetic/boolean
grammar #expr actually supports (see
https://www.mediawiki.org/wiki/Help:Extension:ParserFunctions).

sharp_expr() previously called Python's own eval() directly on
wikitext-derived text, with no explicit globals/locals -- which grants
full access to Python's builtins, including __import__. Confirmed
directly (see project history) that
"{{#expr: __import__('os').system('rm -rf ...') }}" would actually run
a shell command through that path. It's now a small, explicitly
whitelisted AST walker (_sharp_expr_eval_node()) that only ever
computes the specific node types #expr's own grammar supports --
arithmetic, comparison, boolean logic, and the round/trunc/ceil/floor
special cases -- directly in Python, never via eval()/exec()/compile().

Deliberately, NONE of these tests use an actually destructive payload
(no filesystem mutation, no subprocess spawning of any kind) -- not
even as an inert string being fed in for rejection. If a future
regression somehow reintroduced eval() (or some other code-execution
path) without anyone noticing until these tests ran, the payloads
below are chosen so that "the fix is broken" is exactly what a test
failure here would mean, without a broken fix having any chance of
causing real harm as a side effect of finding that out. Two safe but
still fully diagnostic techniques are used instead of a real
destructive command:
  - A read-only, side-effect-free call (os.getcwd()) that, if it
    executed, would return real, observable data -- proving module
    import and method-call capability without changing anything.
  - A "canary": a module-level list in THIS test file, which a
    payload attempts to mutate by reaching back into this module via
    __import__ -- the same underlying mechanism (__import__ granting
    access to anything reachable, including this test's own state)
    a real attack would rely on, but with a completely inert result
    if it succeeds.

Run with:
    python -m unittest tests.test_sharp_expr_security -v
or, from the tests/ directory:
    python -m unittest test_sharp_expr_security -v
"""

import sys
import unittest
import warnings

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


# Mutated only if a payload below manages to actually execute code and
# reach back into this module -- checked but never intentionally set
# by any test itself.
CANARY = []


class SharpExprSecurityTests(unittest.TestCase):

    ERROR_SPAN = '<span class="error"></span>'

    def test_import_os_getcwd_does_not_execute(self):
        # Read-only, no side effects at all -- if this executed, the
        # result would be a real path string, not the error fallback.
        result = ex.sharp_expr("__import__('os').getcwd()")
        self.assertEqual(result, self.ERROR_SPAN)

    def test_canary_mutation_payload_does_not_execute(self):
        # If arbitrary code execution were possible here, this would
        # reach back into this exact test module and append to CANARY
        # -- the same "reach anything reachable via __import__"
        # capability the real os.system(...) attack depends on,
        # demonstrated with a completely inert result instead.
        module_name = __name__
        payload = f"__import__('{module_name}').CANARY.append('EXPLOITED') or 1"
        result = ex.sharp_expr(payload)
        self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(CANARY, [], "the canary was mutated -- code execution occurred")

    def test_classic_sandbox_escape_chain_does_not_execute(self):
        # The well-known "reach __builtins__ via object introspection"
        # technique, entirely without needing an explicit __import__
        # call at all.
        result = ex.sharp_expr("().__class__.__bases__[0].__subclasses__()")
        self.assertEqual(result, self.ERROR_SPAN)

    def test_direct_attribute_access_does_not_execute(self):
        result = ex.sharp_expr("().__class__")
        self.assertEqual(result, self.ERROR_SPAN)

    def test_original_syntaxwarning_trigger_handled_safely(self):
        # The specific pattern that first surfaced this investigation:
        # a number directly adjacent to a parenthesized expression.
        # Confirmed directly (see project history) that ast.parse()
        # alone -- what this evaluator actually uses -- doesn't warn
        # for THIS particular shape, unlike full compile(). It does
        # still warn for a different shape (a number directly adjacent
        # to a keyword, e.g. "3in5") -- see
        # test_keyword_adjacent_number_warning_suppressed below for
        # that one, and sharp_expr()'s own warnings.catch_warnings()
        # block, which covers both regardless of which shape triggers
        # it.
        #
        # record=True rather than simplefilter("error") deliberately:
        # sharp_expr()'s own bare except would swallow a
        # warning-turned-exception too, for an unrelated reason (it
        # catches everything), which would make this test pass either
        # way and prove nothing specific about whether a warning
        # actually fired. Recording instead lets the two be told apart.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = ex.sharp_expr("2(3+4)")
        self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(caught, [], "a warning was emitted where none should be")

    def test_keyword_adjacent_number_warning_suppressed(self):
        # A different trigger shape than the one above: a number
        # directly adjacent to a Python keyword -- "3 in 5" typed (or
        # produced by template substitution) as "3in5" -- which
        # ast.parse() itself does warn about, as a side effect of
        # tokenizing, before still correctly failing to parse right
        # after. Real-world #expr input from actual wikitext, not a
        # synthetic case: this is what a real, full-scale run surfaced.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = ex.sharp_expr("3in5")
        self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(caught, [], "a warning was emitted where none should be")

    def test_malformed_expr_logs_article_title_and_id_when_given(self):
        # The Python-level SyntaxWarning above says only "<unknown>:1"
        # -- useless for finding which real, on-wiki #expr call is
        # actually malformed. page_title/page_id, when supplied (as
        # expandTemplate() does for every real call), make a failure
        # directly traceable back to the article instead.
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            result = ex.sharp_expr("3in5", page_title="Some Article", page_id="123")
        self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("3in5", logs.output[0])
        self.assertIn("Some Article", logs.output[0])
        self.assertIn("123", logs.output[0])

    def test_malformed_expr_logs_without_article_detail_when_page_info_not_given(self):
        # A direct caller not going through expandTemplate() -- the
        # only kind that exists today -- still knows what expression
        # it passed, but a log line is still useful independent of
        # that: it's what shows up if the caller is watching logs
        # rather than inspecting the return value directly, and costs
        # nothing extra to include.
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            result = ex.sharp_expr("3in5")
        self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("3in5", logs.output[0])
        self.assertNotIn("article", logs.output[0])

    def test_valid_expr_logs_nothing_even_with_page_info(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs('wikiextractor.extract', level='WARNING'):
                ex.sharp_expr("2 + 3", page_title="Some Article", page_id="123")

    def test_string_literal_is_rejected_not_evaluated(self):
        # #expr's own real grammar has no string type at all -- a
        # quoted literal should be rejected the same as anything else
        # outside the numeric/boolean whitelist, not silently accepted.
        result = ex.sharp_expr("'harmless string'")
        self.assertEqual(result, self.ERROR_SPAN)


class SharpExprLegitimateUseTests(unittest.TestCase):
    """Positive control: confirms the security fix didn't break the
    narrow, real functionality #expr is actually meant to provide.
    """

    def test_basic_arithmetic(self):
        self.assertEqual(ex.sharp_expr("2 + 2"), "4")
        self.assertEqual(ex.sharp_expr("2 + 3 * 4"), "14")

    def test_mod_and_div_keywords(self):
        self.assertEqual(ex.sharp_expr("10 mod 3"), "1")
        self.assertEqual(ex.sharp_expr("10 div 4"), "2.5")

    def test_mod_keyword_has_a_word_boundary_like_div_and_round_already_did(self):
        # "mod" used to be substituted without \b (unlike div/round,
        # which already had it): "1 mod2" (no space before the second
        # operand) would have silently matched anyway -- 'd' and '2'
        # are both word characters, so even \b wouldn't treat that as
        # a real boundary -- producing "1 %2", which Python happily
        # evaluates to 1. That's a real, silently wrong answer for
        # input that was never actually valid #expr mod syntax at all.
        # Confirmed this now correctly fails to parse instead.
        result = ex.sharp_expr("1 mod2")
        self.assertNotEqual(result, "1")
        self.assertEqual(result, '<span class="error"></span>')

    def test_mod_as_a_substring_of_another_word_is_left_alone(self):
        # Same underlying fix, the more common real-world shape: a
        # word that merely contains "mod" (leaked-through wikitext, a
        # stray word) must not get mangled mid-word.
        self.assertEqual(ex.sharp_expr("commodity 5"), ex.sharp_expr("xyz 5"))

    def test_equality_and_comparison(self):
        self.assertEqual(ex.sharp_expr("5 = 5"), "1")
        self.assertEqual(ex.sharp_expr("3 < 5 and 5 < 10"), "1")

    def test_round(self):
        self.assertEqual(ex.sharp_expr("3.14159 round 2"), "3.14")


class SharpExprRepeatedFailureDedupTests(unittest.TestCase):
    """The same malformed #expr call is frequently invoked many times
    within a single article (e.g. once per row of a table built from
    a broken shared template) -- one genuine occurrence could
    otherwise produce hundreds of identical log lines. Same dedup
    shape as expandTemplate()'s own template-loop detection: count
    every occurrence, but only log the first one per (article,
    expression) pair -- see test_template_loop_guard.py's
    test_warning_logged_once_despite_many_repeats for the template
    side of the same pattern.
    """

    ERROR_SPAN = '<span class="error"></span>'

    def make_extractor(self):
        return ex.Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                             "Test Article", [])

    def test_direct_calls_only_log_the_first_occurrence(self):
        extractor = self.make_extractor()
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            for _ in range(5):
                result = ex.sharp_expr("3in5", page_title="Test Article", page_id="1",
                                        extractor=extractor)
                self.assertEqual(result, self.ERROR_SPAN)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(extractor.malformed_expr_errs, 5)

    def test_different_malformed_expressions_each_log_once(self):
        # Dedup is per-expression, not a blanket "one #expr warning
        # per article" -- a second, genuinely different malformed
        # expression is still worth knowing about.
        extractor = self.make_extractor()
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            ex.sharp_expr("3in5", page_title="Test Article", page_id="1", extractor=extractor)
            ex.sharp_expr("3in5", page_title="Test Article", page_id="1", extractor=extractor)
            ex.sharp_expr("4in6", page_title="Test Article", page_id="1", extractor=extractor)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(extractor.malformed_expr_errs, 3)

    def test_same_expression_in_a_different_article_logs_again(self):
        # Dedup keys on (article id, expression) together -- a fresh
        # Extractor (a new article) must not inherit suppression from
        # a previous one.
        first_extractor = self.make_extractor()
        second_extractor = ex.Extractor(2, "2", "https://test.wikipedia.org/wiki?curid=2",
                                         "Second Article", [])
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            ex.sharp_expr("3in5", page_title="Test Article", page_id="1", extractor=first_extractor)
            ex.sharp_expr("3in5", page_title="Second Article", page_id="2", extractor=second_extractor)
        self.assertEqual(len(logs.output), 2)

    def test_real_end_to_end_pipeline_dedups_across_many_template_invocations(self):
        # Not just sharp_expr() in isolation -- confirms the real
        # chain (clean_text() -> expandTemplate() ->
        # callParserFunction() -> sharp_expr()) actually threads the
        # same Extractor through on every call, the way a real,
        # multiply-transcluded broken template would in practice.
        templates = {
            'Template:BadExpr': '{{#expr: 3in5 }}',
        }
        redirects = {}
        article_text = '\n'.join(['{{BadExpr}}'] * 5)
        extractor = ex.Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                                  "Test Article", [article_text], templates=templates,
                                  redirects=redirects, templatePrefix='Template:')
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            extractor.clean_text(article_text, expand_templates=True)
        expr_warnings = [line for line in logs.output if 'Malformed #expr' in line]
        self.assertEqual(len(expr_warnings), 1)
        self.assertEqual(extractor.malformed_expr_errs, 5)


class SharpExprCascadeSuppressionTests(unittest.TestCase):
    """A second, distinct source of noise the (article, expression)
    dedup above can't catch on its own: nested #expr calls where an
    inner failure's own error-span output gets substituted as literal
    text into the enclosing #expr's input, which then also fails --
    producing a genuinely different, unique expr string at every
    level, so exact-string dedup correctly does NOT collapse them.
    Confirmed directly against a real, reported case: a chain of a
    dozen distinct expr strings, each differing only by an appended
    "+N"/"-N", every one already containing a prior error span before
    it even started.
    """

    def make_extractor(self, page_id=1):
        return ex.Extractor(page_id, str(page_id), f"https://test.wikipedia.org/wiki?curid={page_id}",
                             "Test Article", [])

    def test_chain_of_distinct_cascade_expressions_logs_only_once(self):
        # The exact real-world shape reported: each expression is
        # textually unique (so the plain dedup key alone wouldn't
        # catch it), but each one already embeds the previous
        # failure's error span.
        extractor = self.make_extractor()
        cascade_exprs = [
            '- ' + ex._SHARP_EXPR_ERROR_SPAN,
            ex._SHARP_EXPR_ERROR_SPAN + ' + 1',
            ex._SHARP_EXPR_ERROR_SPAN + ' + 2',
            ex._SHARP_EXPR_ERROR_SPAN + ' + 3',
        ]
        self.assertEqual(len(set(cascade_exprs)), len(cascade_exprs),
                          "test setup sanity check: every cascade expression must be distinct, "
                          "or this test wouldn't be distinguishing cascade suppression from the "
                          "ordinary exact-match dedup tested elsewhere")
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            for e in cascade_exprs:
                ex.sharp_expr(e, page_title="Test Article", page_id="1", extractor=extractor)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("chain of nested #expr calls", logs.output[0])
        self.assertEqual(extractor.malformed_expr_errs, 4)

    def test_root_failure_before_a_cascade_still_logs_its_own_warning(self):
        # The first, standalone failure that actually starts a chain
        # doesn't itself contain an error span -- it's the genuinely
        # useful, actionable one, and must not be swallowed by cascade
        # suppression (which only applies to the *downstream* links).
        extractor = self.make_extractor()
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            ex.sharp_expr("3in5", page_title="Test Article", page_id="1", extractor=extractor)
            ex.sharp_expr(ex._SHARP_EXPR_ERROR_SPAN + " + 1", page_title="Test Article",
                          page_id="1", extractor=extractor)
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("chain of nested", logs.output[0])
        self.assertIn("chain of nested", logs.output[1])

    def test_unrelated_non_cascade_failure_in_same_article_still_logs_separately(self):
        # Cascade suppression must not become a blanket "one #expr
        # warning per article" -- a completely unrelated, standalone
        # malformed expression elsewhere in the same article is still
        # worth its own warning.
        extractor = self.make_extractor()
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            ex.sharp_expr(ex._SHARP_EXPR_ERROR_SPAN + " + 1", page_title="Test Article",
                          page_id="1", extractor=extractor)
            ex.sharp_expr("4in6", page_title="Test Article", page_id="1", extractor=extractor)
        self.assertEqual(len(logs.output), 2)

    def test_cascade_in_a_different_article_logs_again(self):
        # Same (article, cascade) keying discipline as the plain
        # dedup case -- a fresh Extractor (a new article) must not
        # inherit cascade suppression from a previous one.
        first_extractor = self.make_extractor(page_id=1)
        second_extractor = self.make_extractor(page_id=2)
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            ex.sharp_expr(ex._SHARP_EXPR_ERROR_SPAN + " + 1", page_title="Test Article",
                          page_id="1", extractor=first_extractor)
            ex.sharp_expr(ex._SHARP_EXPR_ERROR_SPAN + " + 1", page_title="Test Article",
                          page_id="2", extractor=second_extractor)
        self.assertEqual(len(logs.output), 2)

    def test_real_end_to_end_pipeline_suppresses_a_genuine_nested_expr_cascade(self):
        # Not just sharp_expr() in isolation -- a real, nested #expr
        # call (the actual on-wiki shape that produces this pattern)
        # run through the full clean_text() pipeline.
        extractor = self.make_extractor()
        wikitext = "{{#expr: {{#expr: {{#expr: 3in5 }} + 1 }} + 2 }}"
        with self.assertLogs('wikiextractor.extract', level='WARNING') as logs:
            extractor.clean_text(wikitext, expand_templates=True)
        expr_warnings = [line for line in logs.output if 'Malformed #expr' in line]
        # The innermost failure (3in5) is the root, standalone one;
        # the two outer levels each fail on the inner one's error-span
        # output and get suppressed as cascade continuations.
        self.assertEqual(len(expr_warnings), 2)
        self.assertNotIn("chain of nested", expr_warnings[0])
        self.assertIn("chain of nested", expr_warnings[1])
        self.assertEqual(extractor.malformed_expr_errs, 3)


if __name__ == '__main__':
    unittest.main()
