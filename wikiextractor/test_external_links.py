"""
Tests for replaceExternalLinks()/makeExternalLink()/makeExternalImage()
in extract.py -- previously entirely uncovered, which is exactly how a
real, production-crashing bug got through undetected: makeExternalImage()'s
signature had `alt=''` sitting between `url` and `extractor`, so its one
real call site's second positional argument (the real Extractor) silently
filled `alt` instead, leaving `extractor` at its own None default and
crashing on `extractor.keepLinks` with an AttributeError -- but only on
the rare path where an external link's label is itself an image URL
(MediaWiki historically turned this into an <img> tag by accident; see
the comment on replaceExternalLinks() itself). Every ordinary external
link skips that branch entirely, so nothing in this codebase's earlier,
real-pipeline testing ever exercised it.

Fixed by giving makeExternalImage() the same required-second-positional-
argument shape as makeExternalLink() already had (extractor right after
the value being formatted, no default sitting in between) -- the whole
class of "a keyword-defaulted parameter sits between two required ones"
mistake is what let the wrong value land in the wrong slot silently
rather than raising a clear TypeError immediately.

Run with:
    python -m unittest tests.test_external_links -v
or, from the tests/ directory:
    python -m unittest test_external_links -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex
from wikiextractor.extract import Extractor


class PlainExternalLinkTests(unittest.TestCase):
    """Baseline coverage: an ordinary external link, neither an image
    URL as its own label nor missing one -- never touches
    makeExternalImage() at all.
    """

    def make_extractor(self, **kwargs):
        return Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                          "Test Article", [], **kwargs)

    def test_labeled_link_stripped_by_default(self):
        extractor = self.make_extractor()
        result = ex.replaceExternalLinks("[http://example.com Some Label]", extractor)
        self.assertEqual(result, "Some Label")

    def test_labeled_link_kept_as_anchor_with_keep_links(self):
        extractor = self.make_extractor(keepLinks=True)
        result = ex.replaceExternalLinks("[http://example.com Some Label]", extractor)
        self.assertEqual(result, '<a href="http%3A//example.com">Some Label</a>')

    def test_unlabeled_link_produces_empty_label_by_default(self):
        # A bare [url] with no label: MediaWiki auto-numbers these in
        # its own real renderer; this extractor's own, simpler
        # handling just yields no visible label at all when links
        # aren't being kept -- confirmed pre-existing, unrelated to
        # this file's own regression coverage, just documented here
        # as a baseline since nothing else in the suite covers it.
        extractor = self.make_extractor()
        result = ex.replaceExternalLinks("[http://example.com]", extractor)
        self.assertEqual(result, "")


class ExternalImageLabelRegressionTests(unittest.TestCase):
    """The specific crash: an external link whose label is itself an
    image URL (matches EXT_IMAGE_REGEX), routing through
    makeExternalImage() -- the function whose parameter order used to
    silently misroute the extractor argument into alt instead.
    """

    def make_extractor(self, **kwargs):
        return Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                          "Test Article", [], **kwargs)

    def test_image_labeled_link_does_not_crash_with_keep_links_false(self):
        # This exact shape crashed in production before the fix:
        # AttributeError: 'NoneType' object has no attribute 'keepLinks',
        # since extractor itself silently landed in alt's slot instead.
        extractor = self.make_extractor()
        wikitext = "[http://example.com/photo.jpg http://example.com/photo.jpg]"
        result = ex.replaceExternalLinks(wikitext, extractor)
        self.assertEqual(result, "")

    def test_image_labeled_link_produces_nested_img_in_anchor_with_keep_links_true(self):
        extractor = self.make_extractor(keepLinks=True)
        wikitext = "[http://example.com/photo.jpg http://example.com/photo.jpg]"
        result = ex.replaceExternalLinks(wikitext, extractor)
        self.assertEqual(
            result,
            '<a href="http%3A//example.com/photo.jpg">'
            '<img src="http://example.com/photo.jpg" alt="">'
            '</a>')

    def test_makeExternalImage_called_directly_does_not_crash(self):
        # Direct unit coverage of the function itself, not just via
        # the full replaceExternalLinks() pipeline -- confirms the
        # fixed signature's argument order is actually correct, not
        # just that the one real call site happens to work.
        extractor = self.make_extractor(keepLinks=True)
        result = ex.makeExternalImage("http://example.com/x.png", extractor)
        self.assertEqual(result, '<img src="http://example.com/x.png" alt="">')

    def test_makeExternalImage_respects_keep_links_false(self):
        extractor = self.make_extractor()
        result = ex.makeExternalImage("http://example.com/x.png", extractor)
        self.assertEqual(result, "")


if __name__ == '__main__':
    unittest.main()
