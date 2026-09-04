"""
Tests for argument expansion in expandTemplate()'s parser-function
branch: which arguments are expanded before callParserFunction() is
reached, and which are left as raw wikitext for the function itself to
decide about.

The split follows MediaWiki, and is recorded in
ex.lazyParserFunctions. The branching functions (#if, #ifeq, #iferror,
#ifexist, #ifexpr, #switch) select one of their arguments and throw
the rest away, so only the part that decides the branch is expanded up
front -- that is parts[0], which arrives expanded as part of the
template title. Whichever branch is chosen is expanded afterwards, by
the expandTemplates() call applied to the return value. '#invoke' is
lazy for a different reason: sharp_invoke() takes the module and
function names as written.

Every other parser function is a value function -- padleft, lc, #sub,
#replace, formatnum and the rest -- which reads its arguments as data:
it truncates, upper-cases, searches or pads the string it is handed.
For those, expanding the return value afterwards is too late, because
the function has already consumed whatever it was given. So all of
their arguments are expanded before the call.

Two of the lazy functions are only lazy about their results. #ifeq and
#switch also have comparison operands -- #ifeq's rvalue, and every
#switch case label -- which are data in the same way a value
function's arguments are, and which MediaWiki expands. Those are
expanded with the Extractor's own expandTemplates, which
callParserFunction() binds from the extractor it is given, on demand
as the scan reaches them -- so a #switch that matches its second case
never expands the labels of the twenty after it.

The regression this file pins down came from that second group. A real
jawiki chain ({{citation needed}} -> Template:要出典 ->
Template:Fix -> Template:要出典/dateHandler) passes a computed year to
a date handler as:

    year={{padleft:|5|{{Checkdate|{{{date|}}}}}X}}

With the padding argument left unexpanded, padleft truncated the
literal source text "{{Checkdate|{{{date|}}}}}X" to its first five
characters and returned "{{Che". That fragment carries an unbalanced
"{{", so every downstream {{{year}}} substitution injected one, brace
matching could no longer close the enclosing #switch and #if, and the
whole Fix/dateHandler scaffolding fell out of the article as literal
wikitext -- roughly sixty lines of it on the アンパサンド page.

Run with:
    python -m unittest tests.test_parser_function_argument_expansion -v
or, from the tests/ directory:
    python -m unittest test_parser_function_argument_expansion -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class RecordingTemplates(dict):
    """A templates mapping that records which titles were actually
    fetched for expansion.

    Extractor accepts any object supporting `in`/`[]` for its
    templates argument, which makes this the cheapest way to observe
    whether a given template was expanded at all. Membership testing
    (`in`) goes through dict.__contains__ and is not recorded; only
    __getitem__, which expandTemplate() calls exactly when it is about
    to expand a template body.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetched = []

    def __getitem__(self, key):
        self.fetched.append(key)
        return super().__getitem__(key)


def expand(wikitext, templates=None):
    """Run wikitext through the real expansion chain and return the
    expanded string."""
    templates = templates if templates is not None else {}
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates, templatePrefix='Template:')
    return extractor.expandTemplates(wikitext)


class LazyParserFunctionSetTests(unittest.TestCase):

    def test_the_lazy_set_is_exactly_the_branching_functions_plus_invoke(self):
        # Pinned deliberately: adding a value function here would send
        # it raw wikitext, which is the shape of the padleft bug this
        # file exists to cover.
        self.assertEqual(ex.lazyParserFunctions,
                         {'#if', '#ifeq', '#iferror', '#ifexist', '#ifexpr',
                          '#switch', '#invoke'})

    def test_no_lazy_function_is_also_a_value_function_in_the_dispatch_table(self):
        # #expr and #ifexpr are dispatched directly in
        # callParserFunction() rather than through parserFunctions, so
        # the two collections legitimately overlap on the branching
        # names that do appear in the dict (#if, #ifeq, #iferror,
        # #ifexist, #switch). What must not happen is a value function
        # appearing in the lazy set.
        value_functions = set(ex.parserFunctions) - ex.lazyParserFunctions
        self.assertIn('padleft', value_functions)
        self.assertIn('lc', value_functions)
        self.assertIn('#sub', value_functions)
        self.assertIn('#replace', value_functions)


class ValueFunctionArgumentsAreExpandedTests(unittest.TestCase):

    def test_padleft_padding_argument_is_expanded_before_truncation(self):
        # The reduced form of the jawiki failure. Template:Year yields
        # five characters, so padleft returns them whole; handed the
        # raw source instead it would return the first five characters
        # of "{{Year}}X", i.e. "{{Yea".
        templates = {'Template:Year': '2007年'}
        self.assertEqual(expand('{{padleft:|5|{{Year}}X}}', templates), '2007年')

    def test_replace_search_and_replacement_arguments_are_expanded(self):
        templates = {'Template:Needle': 'b', 'Template:Fixed': 'X'}
        self.assertEqual(expand('{{#replace:abcabc|{{Needle}}|{{Fixed}}}}', templates),
                         'aXcaXc')

    def test_sub_offset_argument_is_expanded(self):
        templates = {'Template:Offset': '2'}
        self.assertEqual(expand('{{#sub:abcdef|{{Offset}}}}', templates), 'cdef')

    def test_padleft_width_argument_is_expanded(self):
        templates = {'Template:Width': '4'}
        self.assertEqual(expand('{{padleft:7|{{Width}}}}', templates), '0007')

    def test_a_template_in_a_later_argument_is_actually_fetched(self):
        templates = RecordingTemplates({'Template:Year': '2007年'})
        expand('{{padleft:|5|{{Year}}X}}', templates)
        self.assertIn('Template:Year', templates.fetched)


class LazyFunctionArgumentsAreNotExpandedTests(unittest.TestCase):

    def test_if_expands_only_the_branch_it_takes(self):
        templates = RecordingTemplates({'Template:Taken': 'kept',
                                        'Template:Skipped': 'discarded'})
        result = expand('{{#if:condition|{{Taken}}|{{Skipped}}}}', templates)
        self.assertEqual(result, 'kept')
        self.assertIn('Template:Taken', templates.fetched)
        self.assertNotIn('Template:Skipped', templates.fetched)

    def test_switch_expands_only_the_matching_case(self):
        templates = RecordingTemplates({'Template:Hit': 'matched',
                                        'Template:Miss': 'unmatched'})
        result = expand('{{#switch:b|a={{Miss}}|b={{Hit}}}}', templates)
        self.assertEqual(result, 'matched')
        self.assertIn('Template:Hit', templates.fetched)
        self.assertNotIn('Template:Miss', templates.fetched)

    def test_ifexpr_expands_only_the_branch_it_takes(self):
        templates = RecordingTemplates({'Template:Yes': 'true branch',
                                        'Template:No': 'false branch'})
        result = expand('{{#ifexpr:1 > 0|{{Yes}}|{{No}}}}', templates)
        self.assertEqual(result, 'true branch')
        self.assertIn('Template:Yes', templates.fetched)
        self.assertNotIn('Template:No', templates.fetched)

    def test_invoke_receives_its_arguments_as_written(self):
        # sharp_invoke() looks up module and function names in the
        # modules table; the name is data to it, not something to
        # expand first.
        templates = RecordingTemplates({'Template:FunctionName': 'somefunction'})
        expand('{{#invoke:SomeModule|{{FunctionName}}}}', templates)
        self.assertNotIn('Template:FunctionName', templates.fetched)


class ChosenBranchIsStillExpandedTests(unittest.TestCase):
    """Laziness defers expansion of the surviving branch rather than
    skipping it: expandTemplate() expands whatever the parser function
    returns."""

    def test_branch_containing_a_template_is_expanded_after_selection(self):
        templates = {'Template:Inner': 'inner text'}
        self.assertEqual(expand('{{#if:x|{{Inner}}}}', templates), 'inner text')

    def test_branch_containing_a_nested_parser_function_is_expanded_after_selection(self):
        templates = {'Template:Word': 'shout'}
        self.assertEqual(expand('{{#if:x|{{uc:{{Word}}}}}}', templates), 'SHOUT')

    def test_nested_lazy_functions_resolve_through_several_levels(self):
        templates = {'Template:Deep': 'bottom'}
        self.assertEqual(
            expand('{{#if:a|{{#switch:b|b={{#if:c|{{Deep}}}}}}}}', templates),
            'bottom')


class DateHandlerChainRegressionTests(unittest.TestCase):
    """The jawiki shape that produced the leaked wikitext, reduced to
    its three participating templates: a validator, a wrapper that
    computes a year by padding the validator's output, and a handler
    that switches on that year."""

    templates = {
        'Template:Checkdate': '{{{1|}}}',
        'Template:Fix': '{{DateHandler|year={{padleft:|5|{{Checkdate|{{{date|}}}}}X}}}}',
        'Template:DateHandler': '{{#switch:{{{year|}}}|2007年=quarterly|#default=plain}}',
    }

    def test_computed_year_reaches_the_switch_and_matches(self):
        self.assertEqual(expand('{{Fix|date=2007年}}', self.templates), 'quarterly')

    def test_unmatched_year_falls_through_to_default(self):
        self.assertEqual(expand('{{Fix|date=2011年}}', self.templates), 'plain')

    def test_no_unbalanced_braces_leak_into_the_result(self):
        result = expand('{{Fix|date=2007年}}', self.templates)
        self.assertNotIn('{{', result)
        self.assertNotIn('#switch', result)
        self.assertNotIn('Che', result)

    def test_full_pipeline_through_clean_text_emits_no_wikitext(self):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=self.templates, templatePrefix='Template:')
        result = '\n'.join(extractor.clean_text('A sentence.{{Fix|date=2007年}}',
                                                expand_templates=True))
        self.assertIn('quarterly', result)
        self.assertNotIn('{{', result)
        self.assertNotIn('#switch', result)


class ComparisonOperandExpansionTests(unittest.TestCase):
    """#ifeq's rvalue and #switch's case labels decide the comparison,
    so they are expanded even though the branches around them are
    not."""

    def test_ifeq_rvalue_containing_a_template_compares_by_value(self):
        templates = {'Template:B': 'b'}
        self.assertEqual(expand('{{#ifeq:b|{{B}}|same|different}}', templates), 'same')

    def test_ifeq_rvalue_that_does_not_match_takes_the_false_branch(self):
        templates = {'Template:B': 'b'}
        self.assertEqual(expand('{{#ifeq:z|{{B}}|same|different}}', templates), 'different')

    def test_ifeq_compares_two_parser_function_results(self):
        # The Fix/title shape: padleft on both sides, one of them
        # reached through a template.
        templates = {'Template:Pad': '{{padleft:|15|X}}'}
        self.assertEqual(
            expand('{{#ifeq:{{padleft:|15|X}}|{{Pad}}|same|different}}', templates),
            'same')

    def test_ifeq_branches_are_still_lazy(self):
        templates = RecordingTemplates({'Template:B': 'b',
                                        'Template:Taken': 'kept',
                                        'Template:Skipped': 'discarded'})
        result = expand('{{#ifeq:b|{{B}}|{{Taken}}|{{Skipped}}}}', templates)
        self.assertEqual(result, 'kept')
        self.assertIn('Template:B', templates.fetched)
        self.assertIn('Template:Taken', templates.fetched)
        self.assertNotIn('Template:Skipped', templates.fetched)

    def test_switch_case_label_containing_a_template_matches(self):
        templates = {'Template:Case': 'yes'}
        self.assertEqual(
            expand('{{#switch:yes|{{Case}}=matched|#default=fell through}}', templates),
            'matched')

    def test_switch_case_label_that_does_not_match_falls_through_to_default(self):
        templates = {'Template:Case': 'yes'}
        self.assertEqual(
            expand('{{#switch:no|{{Case}}=matched|#default=fell through}}', templates),
            'fell through')

    def test_switch_stops_expanding_labels_once_a_case_matches(self):
        templates = RecordingTemplates({'Template:Early': 'hit',
                                        'Template:Late': 'never reached',
                                        'Template:Result': 'R'})
        result = expand('{{#switch:hit|{{Early}}={{Result}}|{{Late}}=other}}', templates)
        self.assertEqual(result, 'R')
        self.assertIn('Template:Early', templates.fetched)
        self.assertIn('Template:Result', templates.fetched)
        self.assertNotIn('Template:Late', templates.fetched)

    def test_switch_result_is_not_expanded_when_its_case_does_not_match(self):
        templates = RecordingTemplates({'Template:Wanted': 'W',
                                        'Template:Unwanted': 'U'})
        result = expand('{{#switch:a|a={{Wanted}}|b={{Unwanted}}}}', templates)
        self.assertEqual(result, 'W')
        self.assertNotIn('Template:Unwanted', templates.fetched)

    def test_switch_fall_through_label_containing_a_template_matches(self):
        # A bare case label with no "=" falls through to the next
        # labeled result; it is a comparison operand too.
        templates = {'Template:Weekend': 'Sunday'}
        self.assertEqual(
            expand('{{#switch:Sunday|{{Weekend}}|Saturday=weekend|#default=weekday}}',
                   templates),
            'weekend')

    def test_pipe_separated_case_labels_are_unaffected_by_the_expand_parameter(self):
        # The multiple-values-share-one-result path: expansion is
        # added around it, not into it, so it behaves the same with a
        # callback in place as without one.
        self.assertEqual(ex.sharp_switch('case5', '1|case5=result3', '#default=nope'),
                         'result3')
        self.assertEqual(ex.sharp_switch('case5', '1|case5=result3', '#default=nope',
                                         expand=lambda text: text),
                         'result3')

    def test_fall_through_across_separate_plain_labels_still_works(self):
        # "| Saturday | Sunday = weekend" reaches sharp_switch as two
        # separate parameters, since splitParts() consumes the
        # top-level pipe. The bare one sets the fall-through flag.
        self.assertEqual(
            expand('{{#switch:Saturday| Saturday | Sunday = weekend | #default = weekday}}'),
            'weekend')


class CallParserFunctionBindsExpandTests(unittest.TestCase):
    """callParserFunction() takes an Extractor, not a separate
    expansion callback: expandTemplates is bound from that same
    Extractor, so there is no way to pair one Extractor's state with
    another's expansion."""

    def _extractor(self, templates):
        return ex.Extractor(1, "1", "https://x", "Test Article", [],
                            templates=templates, templatePrefix='Template:')

    def test_switch_operands_are_expanded_when_an_extractor_is_given(self):
        extractor = self._extractor({'Template:Case': 'yes'})
        self.assertEqual(
            ex.callParserFunction('#switch', ['yes', '{{Case}}=matched', '#default=no'],
                                  extractor.frame, extractor=extractor),
            'matched')

    def test_ifeq_operands_are_expanded_when_an_extractor_is_given(self):
        extractor = self._extractor({'Template:B': 'b'})
        self.assertEqual(
            ex.callParserFunction('#ifeq', ['b', '{{B}}', 'same', 'different'],
                                  extractor.frame, extractor=extractor),
            'same')

    def test_operands_are_compared_as_given_without_an_extractor(self):
        self.assertEqual(
            ex.callParserFunction('#switch', ['yes', '{{Case}}=matched', '#default=no'], []),
            'no')


class DirectCallCompatibilityTests(unittest.TestCase):
    """sharp_ifeq and sharp_switch are called directly by other tests
    in this suite, with no expand callback. Their operands are then
    used as given."""

    def test_ifeq_without_an_expand_callback_compares_literally(self):
        self.assertEqual(ex.sharp_ifeq('a', 'a', 'yes', 'no'), 'yes')
        self.assertEqual(ex.sharp_ifeq('a', '{{B}}', 'yes', 'no'), 'no')

    def test_switch_without_an_expand_callback_compares_literally(self):
        self.assertEqual(ex.sharp_switch('b', 'a=A', 'b=B'), 'B')
        self.assertEqual(ex.sharp_switch('b', '{{B}}=A', '#default=D'), 'D')

    def test_expand_is_keyword_only_so_positional_calls_are_unaffected(self):
        # A fourth positional argument to sharp_ifeq is valueIfFalse,
        # not the callback.
        self.assertEqual(ex.sharp_ifeq('a', 'z', 'yes', 'no'), 'no')


class FixTitleChainRegressionTests(unittest.TestCase):
    """Template:Fix/title on jawiki compares two padleft results to
    decide how to join a date onto a tooltip. The comparison only
    reaches the right answer if the operand after the pipe is
    expanded."""

    templates = {
        'Template:Padded': '{{padleft:|15|{{{1|}}}}}',
        'Template:FixTitle': ('{{#ifeq:{{Padded|{{{1|}}}}}|{{Padded|{{{2|}}}}}'
                              '|identical|combined}}'),
    }

    def test_matching_operands_take_the_true_branch(self):
        self.assertEqual(expand('{{FixTitle|abc|abc}}', self.templates), 'identical')

    def test_differing_operands_take_the_false_branch(self):
        self.assertEqual(expand('{{FixTitle|abc|xyz}}', self.templates), 'combined')

    def test_no_wikitext_leaks_from_the_comparison(self):
        result = expand('{{FixTitle|abc|abc}}', self.templates)
        self.assertNotIn('{{', result)
        self.assertNotIn('padleft', result)


if __name__ == '__main__':
    unittest.main()
