"""
Tests for the doubled-link-bracket collapse fix in replaceInternalLinks()
(collapseDoubledLinkBrackets()).

Background
----------
A real, surprisingly common wikitext authoring mistake -- doubled link
brackets like [[[[title]]]] instead of [[title]] -- was found at
meaningful scale on multiple language wikis: ~150/25000 articles on
Saraiki Wikipedia, ~15000 on Urdu Wikipedia, and (on Sindhi Wikipedia)
baked directly into a widely-transcluded interwiki/sister-projects
table template (e.g. [[[[w:]]]], [[[[wiktionary:]]]], one row per
sister project) -- meaning a single buggy template can by itself
account for a large share of the total occurrences on a given wiki.

Before any fix, findBalanced()'s stack-based bracket matcher treated
the outermost bracket pair as the link delimiter and passed the inner
bracket pair through untouched as literal text in the link's
title/label -- i.e. [[[[title]]]] -> [[title]] in the final output
(brackets reduced but not eliminated).

An earlier version of this fix only collapsed the *opening* side
(runs of 3+ "[" down to exactly 2), which correctly recovered the
title but left a residue of stray trailing "]" characters behind in
the symmetric case (e.g. "title]]"). collapseDoubledLinkBrackets()
improves on this: it detects when the closing side has a *matching*
excess immediately following the link's natural close, and in that
case strips both sides symmetrically for a fully clean result with no
residue at all. This covers the two most common real-world shapes
found (fully symmetric doubling, and asymmetric doubling where only
the opening side was duplicated) with zero residue.

Where the excess doesn't resolve to a clean symmetric match -- e.g. a
doubled outer wrapper around content that itself contains several
genuinely separate real links, where the excess closing brackets only
appear at the very end, not immediately after the first inner link's
own close -- the fix safely falls back to collapsing just the opening
side. This still correctly recovers all the real inner links; it just
may leave a small trailing residue in that narrower, more ambiguous
case, rather than guessing at a merge that could misinterpret genuine
structure.

The fix deliberately never touches the closing side on its own, in
isolation: adjacent closing brackets legitimately occur in real
wikitext, e.g. [[File:x.jpg|[[real link]]]], where a nested real link
is the very last thing before the outer link's own close.

Run with:
    python -m unittest tests.test_link_bracket_collapse -v
or, from the tests/ directory:
    python -m unittest test_link_bracket_collapse -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class SymmetricDoublingTests(unittest.TestCase):
    """The common case: excess opens AND a matching excess of closes
    immediately following the link's natural close -- should resolve
    with zero residue.
    """

    def test_quadruple_bracket_link_resolves_with_no_residue(self):
        text = "کشمیر دے [[[[پاکستان]]]] دے نال"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "کشمیر دے پاکستان دے نال")

    def test_triple_bracket_variant_no_residue(self):
        text = "اوہ [[[پاکستان]]] گیا"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "اوہ پاکستان گیا")

    def test_quintuple_bracket_variant_no_residue(self):
        text = "اوہ [[[[[پاکستان]]]]] گیا"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "اوہ پاکستان گیا")

    def test_interwiki_table_entry_no_residue(self):
        # Real example from Sindhi Wikipedia's sister-projects/interwiki
        # table template -- baked into the template itself, so
        # transcluded (and repeated) across every page that includes it.
        text = "[[[[w:]]]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "w:")

    def test_symmetric_piped_link_no_residue(self):
        # Real example from Sindhi Wikipedia (a book citation).
        text = "[[[[ڀيرو مل مهرچند آڏواڻي|ڀيرومل مهرچند آڏواڻي]]]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "ڀيرومل مهرچند آڏواڻي")


class AsymmetricDoublingTests(unittest.TestCase):
    """Excess opens with no matching excess closes -- these were
    already clean with the simpler opens-only fix, and remain so here.
    """

    def test_asymmetric_airport_list_entry_no_residue(self):
        # Real example from Sindhi Wikipedia (an {{airport-dest-list}}
        # template usage): 4 opens, only 2 closes.
        text = "[[[[يوني ايئر(Uni Air)]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "يوني ايئر(Uni Air)")

    def test_asymmetric_category_link(self):
        # Real example from Sindhi Wikipedia: 4 opens, 2 closes, a
        # category link (which gets dropped entirely regardless, by
        # separate pre-existing category-handling logic).
        text = "[[[[زمرو:دستاويز سانچا]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "")


class AmbiguousComplexCaseTests(unittest.TestCase):
    """A doubled outer wrapper around content containing several
    genuinely separate real links -- the excess closes only appear at
    the very end, not right after the first inner link's own close, so
    this doesn't resolve as a clean symmetric match. All three real
    inner links must still be correctly recovered; a small residue at
    the very end is an accepted, documented limitation for this
    narrower, more ambiguous shape (safer than guessing at a merge that
    could misinterpret genuine link structure).
    """

    def test_multi_link_case_recovers_all_real_links(self):
        # Real example from Sindhi Wikipedia (a motorway infobox row).
        text = "[[[[کراچی]] تا [[لاہور]] [[موٹر وے]] (KLM)]]"
        result = ex.replaceInternalLinks(text)

        self.assertIn("کراچی", result)
        self.assertIn("لاہور", result)
        self.assertIn("موٹر وے", result)
        self.assertNotIn("[[", result)
        # Documented, accepted residue for this specific ambiguous shape.
        self.assertEqual(result, "کراچی تا لاہور موٹر وے (KLM)]]")


class NormalLinkRegressionTests(unittest.TestCase):
    """Ordinary, well-formed links must be completely unaffected."""

    def test_normal_simple_link_unaffected(self):
        text = "اوہ [[پاکستان]] گیا"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "اوہ پاکستان گیا")

    def test_normal_piped_link_unaffected(self):
        text = "اوہ [[پاکستان|ملک]] گیا"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "اوہ ملک گیا")

    def test_two_adjacent_separate_links_unaffected(self):
        # Two genuinely separate, non-nested links with nothing between
        # them -- must not be mistaken for one doubled-bracket link.
        text = "[[پاکستان]][[بھارت]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "پاکستانبھارت")

    def test_log_paste_with_space_separated_brackets_untouched(self):
        # Real example from Sindhi Wikipedia: a maintenance-script log
        # pasted into a page, containing "[[ [[" with a space between --
        # genuinely unrelated to the doubled-bracket typo, and must be
        # left completely alone (the fix only triggers on immediately
        # adjacent brackets with zero separator).
        text = "dbk=[[ [[وڪيپيڊيا:اسان_سان_رابطو_ڪريو]] -> foo"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, text)


class LegitimateNestedLinkRegressionTests(unittest.TestCase):
    """The critical regression check: genuine nested links (real-world
    pattern for file/image captions containing an actual link) must
    behave identically to the unpatched baseline.
    """

    def test_nested_link_with_trailing_caption_text_unaffected(self):
        # Baseline (unpatched) result for this input: ''
        text = "[[File:x.jpg|thumb|caption with a [[real link]] inside]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "")

    def test_nested_link_as_last_thing_before_outer_close_unaffected(self):
        # The exact case that a naive "collapse both sides blindly"
        # fix breaks: adjacent closing brackets here are genuinely two
        # separate closes (inner link, then outer link), not a typo.
        # Baseline (unpatched) result for this input: ''
        text = "[[File:x.jpg|[[real link]]]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "")


class LinkTrailTests(unittest.TestCase):
    """The "trail" mechanism (tailRE = r'\\w+') is a deliberate,
    pre-existing MediaWiki feature, not something introduced by the
    bracket-collapse fix: word characters immediately following a
    link's closing "]]" get concatenated onto the label with NO space,
    e.g. [[cat]]s -> "cats" (used constantly on English Wikipedia too).
    This is worth confirming explicitly because a real Saraiki
    Wikipedia article was found where a simple, well-formed link with a
    trail ([[پاکستان]]ی, intended to render as "پاکستانی" -- "Pakistani")
    was failing to convert at all before this fix, apparently due to a
    separate doubled-bracket instance earlier in the same article
    corrupting findBalanced()'s stack for everything downstream. The
    bracket-collapse fix resolved that as a side effect, so it's worth
    locking in that trails keep working correctly, both on their own
    and specifically when combined with a doubled-bracket link.
    """

    def test_simple_trail_concatenates_with_no_space(self):
        text = "the [[cat]]s sat down"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "the cats sat down")

    def test_real_saraiki_trail_example(self):
        # The real case found in the wild: a normal, non-doubled link
        # with a trail suffix forming the adjectival form "Pakistani".
        text = "آزاد کشمیر اسمبلی ، [[پاکستان]]ی کشمیر دا قانون"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "آزاد کشمیر اسمبلی ، پاکستانی کشمیر دا قانون")

    def test_trail_on_a_doubled_bracket_link_still_works(self):
        # Combining both mechanisms: a doubled-bracket link (which goes
        # through collapseDoubledLinkBrackets first) should still have
        # its trail suffix correctly attached afterward, with no space
        # and no leftover brackets.
        text = "the [[[[cat]]]]s sat down"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "the cats sat down")


class DoubledBracketWithGenuineNestingTests(unittest.TestCase):
    """A doubled-bracket link whose content itself contains a genuinely
    separate, properly-nested real link (e.g. a File: caption with an
    actual [[link]] inside it) -- found in the wild on a real Saraiki
    Wikipedia article (id 786, "امڑی"): a doubled File: link wrapping an
    image caption that itself links to [[غلام]].

    This is harder than the plain symmetric case: a naive "find the
    first ']]'" scan gets fooled by the inner link's own closing pair,
    mistaking it for the outer link's natural close, and ends up
    consuming only one of the outer link's several excess closing
    brackets -- leaving stray residue behind even though the whole
    thing should disappear entirely (File: links are dropped by
    existing, separate namespace-handling logic). The fix uses
    findBalanced() itself (via a small "pseudo" prefix trick) to find
    the outer link's true natural close, correctly skipping over the
    inner nested link, rather than a naive first-match scan.
    """

    def test_doubled_file_link_with_nested_real_link_fully_dropped(self):
        # Real example from Saraiki Wikipedia.
        text = "[[[[فائل:G M Lakha.jpg|thumb|[[غلام]] مرتضٰی لاکھا]]]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "")

    def test_doubled_plain_link_with_nested_real_link_resolves_cleanly(self):
        # A non-File: variant of the same shape, to confirm the fix
        # generalizes and isn't specific to File: links being dropped.
        # Note: the nested [[real link]] inside the label isn't itself
        # recursively re-converted in a single pass -- that's
        # pre-existing behavior unrelated to this fix. What matters
        # here is that the OUTER doubled brackets resolve cleanly with
        # no residue.
        text = "[[[[Some Page|caption with a [[real link]] inside]]]]"
        result = ex.replaceInternalLinks(text)
        self.assertEqual(result, "caption with a [[real link]] inside")


if __name__ == '__main__':
    unittest.main()
