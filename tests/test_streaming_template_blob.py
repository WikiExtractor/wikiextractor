"""
Tests for template_blob.py's StreamingTemplateBlobBuilder -- built to
avoid ever holding a full {title: text} dict in memory while also
holding the compacted blob built from it, which at full EN scale
(~900K templates) was confirmed, on a real production run, to double
the mapper's peak RSS just before forking every worker (~1.85GB for
the dict, ~4.2GB once the blob was also built alongside it) -- and
much of that first ~1.85GB never came back afterward either, even
after the dict went out of scope and an explicit gc.collect(), since
CPython's allocator only returns a fully-empty arena to the OS.

No prior test file covers template_blob.py at all -- this is the
first, covering both the streaming builder directly and
load_templates()'s blob_builder= path that uses it in practice.

Run with:
    python -m unittest tests.test_streaming_template_blob -v
or, from the tests/ directory:
    python -m unittest test_streaming_template_blob -v
"""

import io
import random
import string
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we
import wikiextractor.extract as ex
from wikiextractor import template_blob as tb


class StreamingBuilderMatchesDictBasedTests(unittest.TestCase):
    """The streaming builder must produce byte-identical blobs to the
    existing dict-based build_template_blobs(), for the same input --
    it's a different way of building the same three blobs, not a
    different format.
    """

    def test_byte_identical_output_for_random_templates(self):
        random.seed(5)
        templates = {}
        for i in range(2000):
            templates[f'Template:T{i}'] = ''.join(
                random.choices(string.ascii_letters + ' ', k=random.randint(10, 300)))

        old_records, old_titles, old_content = tb.build_template_blobs(templates)

        builder = tb.StreamingTemplateBlobBuilder()
        for title, text in templates.items():
            builder.add(title, text)
        new_records, new_titles, new_content = builder.finish()

        self.assertEqual(old_records, new_records)
        self.assertEqual(old_titles, new_titles)
        self.assertEqual(old_content, new_content)

    def test_empty_builder_produces_empty_blobs(self):
        builder = tb.StreamingTemplateBlobBuilder()
        records, titles, content = builder.finish()
        self.assertEqual(records, b'')
        self.assertEqual(titles, b'')
        self.assertEqual(content, b'')

    def test_content_order_independent_of_add_order_records_still_sorted(self):
        # Content is appended in whatever order add() is called;
        # only the (much smaller) records index gets sorted by title.
        # Adding out of title order must still produce a correctly
        # sorted, binary-searchable index.
        builder = tb.StreamingTemplateBlobBuilder()
        builder.add('Template:Zebra', 'zebra content')
        builder.add('Template:Apple', 'apple content')
        builder.add('Template:Mango', 'mango content')
        records, titles, content = builder.finish()

        with tb.compact_blobs(records, titles, content) as (wrapper, _names):
            self.assertEqual(wrapper['Template:Zebra'], 'zebra content')
            self.assertEqual(wrapper['Template:Apple'], 'apple content')
            self.assertEqual(wrapper['Template:Mango'], 'mango content')


class LoadTemplatesBlobBuilderPathTests(unittest.TestCase):
    """load_templates(..., blob_builder=...) must resolve redirects
    and noinclude/includeonly exactly like the dict-based path
    (define_template()) does -- same underlying resolve_template_page()
    either way, but worth confirming end to end through the real
    parsing loop, not just at the resolver level.
    """

    def setUp(self):
        we.templateNamespace = ''
        ex.Extractor.templatePrefix = ''

    def load(self, xml_text):
        builder = tb.StreamingTemplateBlobBuilder()
        redirects_builder = tb.StreamingTemplateBlobBuilder()
        with io.StringIO(xml_text) as f:
            for line in f:
                m = we.tagRE.search(line)
                if not m:
                    continue
                tag = m.group(2)
                if tag == 'namespace' and 'key="10"' in line:
                    we.templateNamespace = m.group(3)
                    ex.Extractor.templatePrefix = we.templateNamespace + ':'
                elif tag == '/siteinfo':
                    break
        with io.StringIO(xml_text) as f:
            count = we.load_templates(f, blob_builder=builder,
                                       redirects_blob_builder=redirects_builder)
        return count, builder, redirects_builder

    def test_plain_template_streamed_correctly(self):
        xml = """<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Template:Greeting</title>
<ns>10</ns>
<id>1</id>
<revision>
<id>1</id>
<text>Hello, {{{1}}}!</text>
</revision>
</page>
</mediawiki>
"""
        count, builder, redirects_builder = self.load(xml)
        self.assertEqual(count, 1)
        records, titles, content = builder.finish()
        with tb.compact_blobs(records, titles, content) as (wrapper, _names):
            self.assertEqual(wrapper['Template:Greeting'], 'Hello, {{{1}}}!')

    def test_redirect_goes_to_the_redirects_blob_not_the_templates_blob(self):
        xml = """<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Template:Old</title>
<ns>10</ns>
<id>1</id>
<revision>
<id>1</id>
<text>#REDIRECT [[Template:New]]</text>
</revision>
</page>
</mediawiki>
"""
        count, builder, redirects_builder = self.load(xml)
        self.assertEqual(count, 1)
        records, titles, content = builder.finish()
        self.assertEqual(records, b'')  # nothing streamed into the templates blob
        r_records, r_titles, r_content = redirects_builder.finish()
        with tb.compact_blobs(r_records, r_titles, r_content) as (wrapper, _names):
            self.assertEqual(wrapper['Template:Old'], 'Template:New')

    def test_noinclude_stripped_includeonly_kept_same_as_dict_based_path(self):
        xml = """<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Template:WithDocs</title>
<ns>10</ns>
<id>1</id>
<revision>
<id>1</id>
<text>
<includeonly>real content</includeonly>
<noinclude>
documentation that must never appear
</noinclude>
</text>
</revision>
</page>
</mediawiki>
"""
        count, builder, redirects_builder = self.load(xml)
        records, titles, content = builder.finish()
        with tb.compact_blobs(records, titles, content) as (wrapper, _names):
            stored = wrapper['Template:WithDocs']
        self.assertIn('real content', stored)
        self.assertNotIn('documentation', stored)


if __name__ == '__main__':
    unittest.main()
