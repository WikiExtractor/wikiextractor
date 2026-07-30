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
        # The exact pattern that surfaced this investigation in the
        # first place: a number directly adjacent to a parenthesized
        # expression, which Python's own compile() step (not its
        # parser) flags as likely a mistake. Confirmed directly (see
        # project history) that ast.parse() alone -- what this
        # evaluator actually uses -- never reaches that compile step,
        # so this fix also happens to eliminate the original warning,
        # not just the security hole; asserted here directly rather
        # than assumed.
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

    def test_equality_and_comparison(self):
        self.assertEqual(ex.sharp_expr("5 = 5"), "1")
        self.assertEqual(ex.sharp_expr("3 < 5 and 5 < 10"), "1")

    def test_round(self):
        self.assertEqual(ex.sharp_expr("3.14159 round 2"), "3.14")


if __name__ == '__main__':
    unittest.main()
