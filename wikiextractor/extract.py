# -*- coding: utf-8 -*-

# =============================================================================
#  Copyright (c) 2020. Giuseppe Attardi (attardi@di.unipi.it).
# =============================================================================
#  This file is part of Tanl.
#
#  Tanl is free software; you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License, version 3,
#  as published by the Free Software Foundation.
#
#  Tanl is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

import re
import calendar
import datetime
import html
import json
import ast
import operator
import warnings
from functools import lru_cache
from itertools import zip_longest
from urllib.parse import quote as urlencode
from urllib.parse import unquote as urldecode
from html.entities import name2codepoint
import logging
import time

# Named separately from WikiExtractor.py's own 'wikiextractor.mapreduce'
# logger: this one covers extraction mechanics specifically -- template
# substitution, invocation, link processing -- gated by --debug, while
# the mapreduce one covers pipeline coordination under
# --debug_map_reduce. Named and configured independently so either can
# be enabled without the other.
logger = logging.getLogger('wikiextractor.extract')

# A level between INFO (20) and DEBUG (10), for output that's too
# granular for every run (per-article detail: which specific page had
# which specific issue) but far too much to require full DEBUG
# tracing to see at all (DEBUG here also includes low-level extraction
# mechanics -- template invocation, parameter substitution -- entirely
# unrelated to "which pages have errors" and vastly more voluminous).
# Currently used for exactly one thing: the per-article error summary
# in Extractor.extract() below. Registered globally via
# addLevelName() so logger.log(DETAIL, ...) renders as "DETAIL: ..."
# rather than a bare, unlabeled number; WikiExtractor.py's own CLI
# (--verbose) sets a logger's threshold to this value the same way it
# does for the standard levels.
DETAIL = 15
logging.addLevelName(DETAIL, 'DETAIL')

# ----------------------------------------------------------------------

# <nowiki>...</nowiki> marks its contents as literal text, exempt from
# all wikitext parsing -- but findMatchingBraces() (used by
# expandTemplates() to find {{...}} template-call boundaries, and by
# splitParts() to split a call's own argument list on |) doesn't know
# that: it just scans raw text for brace/bracket characters. A
# template that uses <nowiki>}}</nowiki> to *display* the literal
# characters "}}" -- a real, unremarkable pattern, e.g. inside an
# error message explaining correct call syntax -- can prematurely
# terminate an enclosing template call, silently truncating everything
# after that point into unprocessed, literal leftover text. Confirmed
# on real, live Wikipedia data: Template:Metadata population AT-1 (via
# Template:Infobox settlement, one of the most widely-used templates
# on the whole project) does exactly this in its own error-message
# branch, which is reached by an easy-to-hit, unremarkable case
# (calling it with an argument it doesn't recognize).
#
# Real MediaWiki avoids this by protecting <nowiki> regions during its
# own preprocessing stage, before any brace-matching happens at all.
# _mask_nowiki()/_unmask_nowiki() do the same here: replace each
# <nowiki>...</nowiki> span (tags included) with an opaque placeholder
# containing no brace, bracket, or pipe character at all, so it can't
# be misread as real template/link syntax or a real argument
# separator, then restore the original span verbatim afterward.
#
# This has to run inside expandTemplates() itself, not once on the
# original article text -- a <nowiki> sequence can arrive *mid-
# expansion*, introduced by template substitution (exactly what
# happens in the real Metadata population AT-1 case above), not just
# by being present in the wikitext being scanned at the top of the
# call stack. expandTemplate() and splitParts() never see raw text
# directly -- only slices of whatever expandTemplates() already
# scanned -- so masking in that one place protects every downstream
# caller, at every level of recursion, without needing to change
# expandTemplate(), splitParts(), or any of their own call sites.
_NOWIKI_RE = re.compile(r'<nowiki\s*>(.*?)</nowiki\s*>', re.IGNORECASE | re.DOTALL)
# A separate, cheaper pattern for the common-case presence check below
# -- just the opening tag, no DOTALL/closing-tag search needed for
# that. re.search() with IGNORECASE doesn't need to allocate a
# lowercased copy of `text` the way `'<nowiki' not in text.lower()`
# does, which matters here since this runs on every expandTemplates()
# call, the vast majority of which have no <nowiki> at all.
_NOWIKI_PRESENCE_RE = re.compile(r'<nowiki', re.IGNORECASE)


def _mask_nowiki(text):
    """Replaces each <nowiki>...</nowiki> span in `text` (tags
    included) with an opaque placeholder token -- a NUL-delimited
    string containing no brace, bracket, or pipe character, so it
    can't be misread as template/link syntax or an argument separator
    by anything downstream. NUL bytes are never valid content in a
    well-formed XML-sourced dump (XML itself forbids raw NUL bytes),
    so collision with real article text is not a practical concern.

    Skips the substitution regex entirely (a cheap, common-case fast
    path, via the separate _NOWIKI_PRESENCE_RE above) when `text` has
    no "<nowiki" substring at all -- true for the overwhelming
    majority of expandTemplates() calls, so this keeps the added cost
    close to zero except where it's actually needed.

    :return: (masked_text, placeholders) where placeholders is a dict
        mapping each placeholder token back to the exact original
        <nowiki>...</nowiki> text it replaced, or None if nothing was
        masked (the common case) -- pass this straight through to
        _unmask_nowiki() either way; it treats None as "nothing to
        restore".
    """
    if not _NOWIKI_PRESENCE_RE.search(text):
        return text, None
    placeholders = {}

    def replace(m):
        token = '\x00NOWIKI%d\x00' % len(placeholders)
        placeholders[token] = m.group(0)
        return token

    return _NOWIKI_RE.sub(replace, text), placeholders



def _unmask_nowiki(text, placeholders):
    """Restores every placeholder token _mask_nowiki() produced back
    to its original, exact <nowiki>...</nowiki> text. `placeholders`
    being None (nothing was masked) or empty is a correct, cheap no-op.
    """
    if not placeholders:
        return text
    for token, original in placeholders.items():
        text = text.replace(token, original)
    return text


# match tail after wikilink
tailRE = re.compile(r'\w+')
syntaxhighlight = re.compile('&lt;syntaxhighlight .*?&gt;(.*?)&lt;/syntaxhighlight&gt;', re.DOTALL)

## PARAMS ####################################################################

##
# Defined in <siteinfo>
# We include as default Template, when loading external template file.
#
# Static, import-time-only default -- never mutated after this point.
# WikiExtractor.py used to reassign its own separately-imported copy of
# this name from real siteinfo data, which never actually reached this,
# the one Extractor instances really read (see Extractor.knownNamespaces
# for the fix and the bug this used to be). Real, per-run values are
# built explicitly in WikiExtractor.py and passed into each Extractor.
_DEFAULT_KNOWN_NAMESPACES = frozenset(['Template'])

##
# The #REDIRECT keyword, localized. MediaWiki's real redirect magic
# word has a per-wiki-language translation (e.g. Sindhi uses "چوريو"
# instead of "REDIRECT"), separate from the interface language --
# matching only the English form meant a redirect page in a non-
# English wiki wasn't recognized as a redirect at all, and its entire
# (often stale, pre-redirect) body text got treated as real content.
#
# Extensible: add further confirmed, per-language keywords here as
# they turn up on other wikis, rather than guessing translations
# preemptively for languages not yet actually encountered.
redirectKeywords = ['REDIRECT', 'چوريو', 'رجوع_مکرر']
redirectRE = re.compile(
    r'#(?:%s)\b.*?\[\[([^\]]*)]]' % '|'.join(redirectKeywords),
    re.IGNORECASE)

##
# Drop these elements from article text
#
discardElements = [
    'gallery', 'timeline', 'noinclude', 'pre',
    'table', 'tr', 'td', 'th', 'caption', 'div',
    'form', 'input', 'select', 'option', 'textarea',
    'ul', 'li', 'ol', 'dl', 'dt', 'dd', 'menu', 'dir',
    'ref', 'references', 'img', 'imagemap', 'source', 'small',
    'inputbox',
    # includeonly is the direct counterpart of noinclude above, and the
    # same reasoning applies: it's a template-specific construct, its
    # content is only ever meant to be visible when the page is
    # TRANSCLUDED elsewhere (the opposite of noinclude), never on a
    # direct/standalone view -- which is exactly the only context a
    # regular (ns=0) article is ever extracted in. Its appearance in a
    # regular article is the same kind of copy-paste artifact noinclude
    # already accounts for. Confirmed on a real pnb.wikipedia.org page
    # (id 38683) whose entire visible body was just stray
    # "<includeonly> · </includeonly>" markup, left untouched (and
    # HTML-escaped, since it wasn't a tag the rest of the pipeline
    # recognized) rather than discarded like noinclude already is.
    'includeonly',
    # A MediaWiki extension tag (from the CategoryTree extension) that
    # dynamically renders a live category hierarchy from the wiki's own
    # database -- nothing meaningful for a static text extractor to
    # produce from it either way, same category as gallery/timeline
    # above. Confirmed leaking through verbatim (also HTML-escaped) on
    # a real pnb.wikipedia.org page (id 40471).
    'categorytree',
    # The reading half of an East Asian ruby annotation (furigana):
    # rt holds the reading, rtc a second one, and rp the parentheses a
    # renderer without ruby support falls back to. The base text they
    # annotate is kept -- ruby and rb are in ignoredTags -- so
    # "加藤{{ruby|由美|よしみ}}" extracts as "加藤由美". Keeping the
    # reading would splice a second spelling of the same word into the
    # running text, and ruby markup that omits rp (most of it) would
    # fuse base and reading with nothing between them.
    'rp', 'rt', 'rtc',
]

##
# Recognize only these namespaces
# w: Internal links to the Wikipedia
# wiktionary: Wiki dictionary
# wikt: shortcut for Wiktionary
#
# Static, import-time-only default -- never mutated after this point.
# WikiExtractor.py used to reassign its own separately-imported copy of
# this name from --namespaces, which (via `from .extract import
# acceptedNamespaces` followed by a later reassignment) never actually
# reached this, the one Extractor instances really read -- a plain
# rebind of an imported name never propagates back to the defining
# module. Real, per-run values are built explicitly in WikiExtractor.py
# and passed into each Extractor.
_DEFAULT_ACCEPTED_NAMESPACES = ('w', 'wiktionary', 'wikt')


def get_url(urlbase, uid):
    return "%s?curid=%s" % (urlbase, uid)


# ======================================================================


def clean(extractor, text, expand_templates=False, html_safe=True):
    """
    Transforms wiki markup. If the command line flag --escapedoc is set then the text is also escaped
    @see https://www.mediawiki.org/wiki/Help:Formatting
    :param extractor: the Extractor t use.
    :param text: the text to clean.
    :param expand_templates: whether to perform template expansion.
    :param html_safe: whether to convert reserved HTML characters to entities.
    @return: the cleaned text.
    """

    if expand_templates:
        # expand templates
        # See: http://www.mediawiki.org/wiki/Help:Templates
        text = extractor.expandTemplates(text)
    else:
        # Drop transclusions (template, parser functions)
        text = dropNested(text, r'{{', r'}}')

    # Drop tables
    text = dropNested(text, r'{\|', r'\|}')

    # replace external links
    text = replaceExternalLinks(text, extractor)

    # replace internal links
    text = replaceInternalLinks(text, extractor)

    # drop MagicWords behavioral switches
    text = magicWordsRE.sub('', text)

    # ############### Process HTML ###############

    # turn into HTML, except for the content of <syntaxhighlight>
    res = ''
    cur = 0
    for m in syntaxhighlight.finditer(text):
        end = m.end()
        res += unescape(text[cur:m.start()]) + m.group(1)
        cur = end
    text = res + unescape(text[cur:])

    # Handle bold/italic/quote
    if extractor.HtmlFormatting:
        text = bold_italic.sub(r'<b>\1</b>', text)
        text = bold.sub(r'<b>\1</b>', text)
        text = italic.sub(r'<i>\1</i>', text)
    else:
        text = bold_italic.sub(r'\1', text)
        text = bold.sub(r'\1', text)
        text = italic_quote.sub(r'"\1"', text)
        text = italic.sub(r'"\1"', text)
        text = quote_quote.sub(r'"\1"', text)
    # residuals of unbalanced quotes
    text = text.replace("'''", '').replace("''", '"')

    # Collect spans

    # br/hr carry genuine line-break semantics: unlike a comment or a
    # citation marker, deleting one with nothing in its place can
    # merge two adjacent words together if there was no surrounding
    # whitespace in the source. Substitute with a space, but only
    # where there's actually something to merge with on both sides --
    # a tag at the very start/end of a line needs no separator, since
    # adding one there just clutters every diff against that line.
    #
    # This MUST run before any of the span-collecting steps below:
    # substituteLineBreakTag() changes text's length, so any span
    # collected beforehand (comments, self-closing tags, ignored tags)
    # would hold stale positions once dropSpans() later runs against
    # the shifted text.
    text = substituteLineBreakTag(lineBreak_tag_pattern, text)

    # Same must-run-before-span-collection reasoning as above: this
    # also mutates text's length.
    text = substituteLineBreakTag(block_separator_tag_pattern, text, separator='\n')

    spans = []
    # Drop HTML comments
    for m in comment.finditer(text):
        spans.append((m.start(), m.end()))

    # Drop self-closing tags
    for pattern in selfClosing_tag_patterns:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    # Drop ignored tags
    for left, right in extractor.ignored_tag_patterns:
        for m in left.finditer(text):
            spans.append((m.start(), m.end()))
        for m in right.finditer(text):
            spans.append((m.start(), m.end()))

    # Bulk remove all spans
    text = dropSpans(spans, text)

    # Drop discarded elements
    for tag in discardElements:
        close_pattern = r'<\s*/\s*%s\s*>' % tag
        # [^>]*(?<!/) rather than [^>/]*: attribute values can
        # legitimately contain a literal '/' (e.g. a ref name like
        # "geo/18aug2018-1"), so only a '/' immediately before the
        # final '>' is excluded -- that's a genuine self-closing tag
        # (already handled separately by selfClosing_tag_patterns
        # above), not a wrapping open for discardElements to pair up.
        # (?=(...))\1 emulates an atomic/possessive match for the
        # quoted alternatives (see lineBreak_tag_pattern above) --
        # needed here specifically because without it, the (?<!/)
        # exclusion can force a backtrack that falls back to plain
        # [^>] matching, wrongly ending at a quoted value's own inner
        # '>' instead of correctly failing to match a genuine self-
        # closing tag like <ref style="a > b" />.
        text = dropNested(text, r'''<\s*%s\b(?:(?=("[^"]*"|'[^']*'|[^>]))\1)*(?<!/)>''' % tag,
                           close_pattern)
        # dropNested only removes a close tag as part of a matched
        # pair -- an unpaired one (its own open consumed or malformed
        # elsewhere) is left untouched by its pairing logic. So
        # anything still matching close_pattern at this point is
        # genuinely orphaned -- same "strip the stray tag" approach as
        # the noinclude handling below.
        text = re.sub(close_pattern, '', text, flags=re.IGNORECASE)

    # Any <noinclude>/</noinclude> still remaining at this point is
    # genuinely unmatched within this page's own text -- a properly
    # paired instance would already have been removed, tags and
    # content together, by the loop above. noinclude is a
    # template-specific construct; its most likely source in a
    # REGULAR article (not a template page) is misplaced markup a
    # human editor accidentally copy-pasted directly from a template,
    # sometimes with the closing tag appearing BEFORE its "opening"
    # counterpart -- structurally out of order, so they can never be
    # matched to each other as a pair at all, no matter how the
    # matching logic works. Rather than guess at what content was
    # "supposed" to be wrapped (which the malformed, out-of-order
    # source gives no reliable way to determine), just strip the
    # literal tag text and leave everything else untouched --
    # eliminates the visible raw-markup clutter without risking
    # discarding real article content on a guess.
    text = re.sub(r'<\s*noinclude\s*/?\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/\s*noinclude\s*>', '', text, flags=re.IGNORECASE)

    if not extractor.HtmlFormatting:
        # Turn into text what is left (&amp;nbsp;) and <syntaxhighlight>
        text = unescape(text)

    # Expand placeholders
    for pattern, placeholder in placeholder_tag_patterns:
        index = 1
        for match in pattern.finditer(text):
            text = text.replace(match.group(), '%s_%d' % (placeholder, index))
            index += 1

    text = text.replace('<<', u'«').replace('>>', u'»')

    #############################################

    # Cleanup text
    text = text.replace('\t', ' ')
    text = spaces.sub(' ', text)
    text = dots.sub('...', text)
    # remove space before closing / after opening punctuation
    text = re.sub(r' ([,:.\)\]»])', r'\1', text)
    text = re.sub(r'([\[\(«]) ', r'\1', text)
    text = re.sub(r'\n\W+?\n', '\n', text, flags=re.U)  # lines with only punctuations
    text = text.replace(',,', ',').replace(',.', '.')
    if html_safe:
        text = html.escape(text, quote=False)
    return text


# skip level 1, it is page name level
section = re.compile(r'(==+)\s*(.*?)\s*\1')

listOpen = {'*': '<ul>', '#': '<ol>', ';': '<dl>', ':': '<dl>'}
listClose = {'*': '</ul>', '#': '</ol>', ';': '</dl>', ':': '</dl>'}
listItem = {'*': '<li>%s</li>', '#': '<li>%s</<li>', ';': '<dt>%s</dt>',
            ':': '<dd>%s</dd>'}


def compact(text, mark_headers=False, extractor=None):
    """Deal with headers, lists, empty sections, residuals of tables.
    :param text: convert to HTML
    :param extractor: the calling Extractor, for its own
        HtmlFormatting/keepSections settings.
    """

    page = []  # list of paragraph
    headers = {}  # Headers for unfilled sections
    emptySection = False  # empty sections are discarded
    listLevel = ''  # nesting of lists

    for line in text.split('\n'):

        if not line.strip():
            if len(listLevel):    # implies extractor.HtmlFormatting
                for c in reversed(listLevel):
                    page.append(listClose[c])
                    listLevel = ''
            continue

        # Handle section titles
        m = section.match(line)
        if m:
            title = m.group(2)
            lev = len(m.group(1))
            if extractor.HtmlFormatting:
                page.append("<h%d>%s</h%d>" % (lev, title, lev))
            if title and title[-1] not in '!?':
                title += '.'

            if mark_headers:
                title = "## " + title

            headers[lev] = title
            # drop previous headers
            headers = { k:v for k,v in headers.items() if k <= lev }
            emptySection = True
            continue
        # Handle page title
        if line.startswith('++'):
            title = line[2:-2]
            if title:
                if title[-1] not in '!?':
                    title += '.'
                page.append(title)
        # handle indents
        elif line[0] == ':':
            page.append(line.lstrip(':'))
        # handle lists
        # @see https://www.mediawiki.org/wiki/Help:Formatting
        elif line[0] in '*#;':
            if extractor.HtmlFormatting:
                # close extra levels
                l = 0
                for c in listLevel:
                    if l < len(line) and c != line[l]:
                        for extra in reversed(listLevel[l:]):
                            page.append(listClose[extra])
                        listLevel = listLevel[:l]
                        break
                    l += 1
                if l < len(line) and line[l] in '*#;:':
                    # add new level (only one, no jumps)
                    # FIXME: handle jumping levels
                    type = line[l]
                    page.append(listOpen[type])
                    listLevel += type
                    line = line[l+1:].strip()
                else:
                    # continue on same level
                    type = line[l-1]
                    line = line[l:].strip()
                page.append(listItem[type] % line)
            else:
                continue
        elif len(listLevel):    # implies extractor.HtmlFormatting
            for c in reversed(listLevel):
                page.append(listClose[c])
            listLevel = []

        # Drop residuals of lists
        elif line[0] in '{|' or line[-1] == '}':
            continue
        # Drop irrelevant lines
        elif (line[0] == '(' and line[-1] == ')') or line.strip('.-') == '':
            continue
        elif len(headers):
            if extractor.keepSections:
                items = sorted(headers.items())
                for (i, v) in items:
                    page.append(v)
            headers.clear()
            page.append(line)  # first line
            emptySection = False
        elif not emptySection:
            page.append(line)
            # dangerous
            # # Drop preformatted
            # elif line[0] == ' ':
            #     continue

    return page


# ----------------------------------------------------------------------

def dropNested(text, openDelim, closeDelim):
    """
    A matching function for nested expressions, e.g. namespaces and tables.
    """
    openRE = re.compile(openDelim, re.IGNORECASE)
    closeRE = re.compile(closeDelim, re.IGNORECASE)
    # partition text in separate blocks { } { }
    spans = []  # pairs (s, e) for each partition
    nest = 0  # nesting level
    start = openRE.search(text, 0)
    if not start:
        return text
    end = closeRE.search(text, start.end())
    next = start
    while end:
        next = openRE.search(text, next.end())
        if not next:  # termination
            while nest:  # close all pending
                nest -= 1
                end0 = closeRE.search(text, end.end())
                if end0:
                    end = end0
                else:
                    break
            spans.append((start.start(), end.end()))
            break
        while end.end() < next.start():
            # { } {
            if nest:
                nest -= 1
                # try closing more
                last = end.end()
                end = closeRE.search(text, end.end())
                if not end:  # unbalanced
                    if spans:
                        span = (spans[0][0], last)
                    else:
                        span = (start.start(), last)
                    spans = [span]
                    break
            else:
                spans.append((start.start(), end.end()))
                # advance start, find next close
                start = next
                end = closeRE.search(text, next.end())
                break  # { }
        if next != start:
            # { { }
            nest += 1
    # collect text outside partitions
    return dropSpans(spans, text)


def dropSpans(spans, text):
    """
    Drop from text the blocks identified in :param spans:, possibly nested.
    """
    spans.sort()
    res = ''
    offset = 0
    for s, e in spans:
        if offset <= s:  # handle nesting
            if offset < s:
                res += text[offset:s]
            offset = e
    res += text[offset:]
    return res


# ----------------------------------------------------------------------
# External links

# from: https://doc.wikimedia.org/mediawiki-core/master/php/DefaultSettings_8php_source.html

wgUrlProtocols = [
    'bitcoin:', 'ftp://', 'ftps://', 'geo:', 'git://', 'gopher://', 'http://',
    'https://', 'irc://', 'ircs://', 'magnet:', 'mailto:', 'mms://', 'news:',
    'nntp://', 'redis://', 'sftp://', 'sip:', 'sips:', 'sms:', 'ssh://',
    'svn://', 'tel:', 'telnet://', 'urn:', 'worldwind://', 'xmpp:', '//'
]

# from: https://doc.wikimedia.org/mediawiki-core/master/php/Parser_8php_source.html

# Constants needed for external link processing
# Everything except bracket, space, or control characters
# \p{Zs} is unicode 'separator, space' category. It covers the space 0x20
# as well as U+3000 is IDEOGRAPHIC SPACE for bug 19052
EXT_LINK_URL_CLASS = r'[^][<>"\x00-\x20\x7F\s]'
ExtLinkBracketedRegex = re.compile(
    r'(?i)\[((' + '|'.join(wgUrlProtocols) + ')' + EXT_LINK_URL_CLASS + r'+)\s*([^\]\x00-\x08\x0a-\x1F]*?)\]',
    re.S | re.U)
EXT_IMAGE_REGEX = re.compile(
    r"""(?i)^(http://|https://)([^][<>"\x00-\x20\x7F\s]+)
    /([A-Za-z0-9_.,~%\-+&;#*?!=()@\x80-\xFF]+)\.(gif|png|jpg|jpeg)$""",
    re.X | re.S | re.U)


def replaceExternalLinks(text, extractor):
    s = ''
    cur = 0
    for m in ExtLinkBracketedRegex.finditer(text):
        s += text[cur:m.start()]
        cur = m.end()

        url = m.group(1)
        label = m.group(3)

        # # The characters '<' and '>' (which were escaped by
        # # removeHTMLtags()) should not be included in
        # # URLs, per RFC 2396.
        # m2 = re.search('&(lt|gt);', url)
        # if m2:
        #     link = url[m2.end():] + ' ' + link
        #     url = url[0:m2.end()]

        # If the link text is an image URL, replace it with an <img> tag
        # This happened by accident in the original parser, but some people used it extensively
        m = EXT_IMAGE_REGEX.match(label)
        if m:
            label = makeExternalImage(label, extractor)

        # Use the encoded URL
        # This means that users can paste URLs directly into the text
        # Funny characters like ö aren't valid in URLs anyway
        # This was changed in August 2004
        s += makeExternalLink(url, label, extractor)  # + trail

    return s + text[cur:]


def makeExternalLink(url, anchor, extractor):
    """Function applied to wikiLinks"""
    if extractor.keepLinks:
        return '<a href="%s">%s</a>' % (urlencode(url), anchor)
    else:
        return anchor


def makeExternalImage(url, extractor, alt=''):
    """Function applied to wikiLinks whose label is itself an image URL."""
    if extractor.keepLinks:
        return '<img src="%s" alt="%s">' % (url, alt)
    else:
        return alt



# ----------------------------------------------------------------------
# WikiLinks
# See https://www.mediawiki.org/wiki/Help:Links#Internal_links

# Can be nested [[File:..|..[[..]]..|..]], [[Category:...]], etc.
# Also: [[Help:IPA for Catalan|[andora]]]


def collapseDoubledLinkBrackets(text):
    """
    Collapse a real, surprisingly common wikitext authoring mistake:
    doubled link brackets, e.g. [[[[title]]]] instead of [[title]].
    Found at meaningful scale on multiple language wikis -- sometimes
    as an isolated typo, sometimes baked into a widely-transcluded
    template (e.g. a sister-projects/interwiki table), in which case a
    single buggy template can produce very many instances.

    A valid link target can never legitimately start with "[[" as its
    own first two characters, so a run of 3+ opening brackets is never
    genuine nesting -- only ever this mistake. Three distinct cases:

    1. The closing side has a matching excess immediately after the
       first natural close (the common case: someone doubled the whole
       [[...]] wrapper symmetrically) -- both sides are stripped and
       the result is fully clean with no residue.

    2. No natural close is found at all anywhere in the rest of the
       text (an asymmetric mistake: fewer closing brackets than opens,
       e.g. the real Sindhi Wikipedia "[[[[يوني ايئر(Uni Air)]]" case:
       4 opens, only 2 closes). Collapsing just the opening side is
       safe here and comes out fully clean, since there's nothing else
       to strand.

    3. A natural close IS found, but nothing extra immediately follows
       it -- there's some other, different structure in between
       before wherever a true excess (if any) actually sits, e.g. the
       real Saraiki Wikipedia case [[[[سرائیکی]] زبان|سرائیکی]] (the
       true outer close is on the far side of a whole separate
       "زبان|..." phrase, not immediately after the first inner
       close). This is the one case where this function does NOT
       modify anything, leaving the run entirely untouched instead of
       collapsing just the opens. Collapsing just the opens here would
       be unsafe: findBalanced() on the original, fully-unmodified
       text already matches the whole (globally-balanced) span as one
       piece, and when a pipe happens to hide embedded garbage behind
       a clean label, that already produces correct output on its own
       -- but collapsing just the opens would make the first inner
       closing pair look like a complete, separate match, ending too
       early and stranding everything after it as newly-visible
       leftover text that was never visible before. Leaving it
       untouched means this function can only ever improve on the
       unmodified behavior, never make a previously-correct case
       worse -- at the cost of not attempting to also recover the
       individual real links in more complex versions of this shape
       (e.g. a doubled wrapper around several separate real links),
       which remain exactly as findBalanced() would have handled them
       without this function at all.

    Deliberately does not touch the closing side on its own: adjacent
    closing brackets legitimately occur in real nesting, e.g.
    [[File:x.jpg|[[real link]]]] where a nested real link is the last
    thing before the outer link's own close.
    """
    # Fast path: the vast majority of documents contain no run of 3+
    # consecutive opening brackets at all (measured: a small fraction
    # of articles even on the wikis where this pattern shows up).
    # "[[[" appearing as a substring is exactly equivalent to "a run of
    # 3+ consecutive '[' exists somewhere" (if such a run exists, its
    # first 3 characters are that substring; if that substring exists,
    # those are 3 consecutive '[' by definition) -- so this is a fully
    # safe, exact short-circuit, not an approximation, and lets the
    # (relatively expensive) per-character scan below be skipped
    # entirely for documents that don't need it.
    if '[[[' not in text:
        return text

    result = []
    cur = 0
    n = len(text)
    pos = 0
    while True:
        m = _tripleOpenRE.search(text, pos)
        if not m:
            break
        i, j = m.start(), m.end()
        if i < cur:
            # This run sits inside content already emitted as part of
            # a previous occurrence's processed output (e.g. a nested
            # run inside a caption we already consumed) -- skip it
            # rather than reprocessing already-handled text.
            pos = cur
            continue
        result.append(text[cur:i])
        open_run = j - i
        excess = open_run - 2
        # Find where a normal, correctly-nested single link
        # starting at position j would naturally close, using
        # findBalanced() itself rather than a naive first-']]'
        # scan -- this correctly skips over any genuinely
        # nested real link in between (e.g. a File: link whose
        # caption itself contains an actual [[link]]), rather
        # than mistaking that inner link's own close for the
        # outer one's.
        #
        # text[j-2:j] is already "[[" (the last two characters
        # of the run of 3+ opens we just matched), so slicing
        # from j-2 gives the same "starts with [[" property as
        # concatenating a synthetic '[[' prefix onto text[j:]
        # would, but as a single slice rather than a second
        # string-building step.
        pseudo = text[j - 2:]
        match = next(findBalanced(pseudo, _LINK_OPEN_DELIM, _LINK_CLOSE_DELIM), None)
        if match is not None:
            _, pseudo_end = match
            close_pos = j + (pseudo_end - 2) - 2
            k = close_pos + 2
            trailing_closes = 0
            while k < n and text[k] == ']' and trailing_closes < excess:
                trailing_closes += 1
                k += 1
            if trailing_closes == excess:
                result.append('[[')
                result.append(text[j:close_pos + 2])
                cur = close_pos + 2 + excess
                pos = cur
                continue
            if close_pos + 2 == n:
                # The found natural close is the very last thing in
                # the text -- nothing follows it at all, e.g. the real
                # Sindhi Wikipedia "[[[[يوني ايئر(Uni Air)]]" case: 4
                # opens, only 2 closes, and the found close ends
                # exactly at the end of the text. Collapsing just the
                # opens is safe here: there's nothing left afterward
                # that could be stranded.
                result.append('[[')
                cur = j
                pos = j
                continue
            # A natural close WAS found here, but nothing extra
            # immediately follows it, AND there's still more text
            # after it -- some other, different structure exists in
            # between before wherever a true excess (if any) actually
            # sits, e.g. the real Saraiki Wikipedia case
            # [[[[سرائیکی]] زبان|سرائیکی]] (the true outer close is on
            # the far side of a whole separate "زبان|..." phrase, not
            # immediately after the first inner close). Collapsing
            # just the opens here is unsafe: it can turn a case that
            # was ACCIDENTALLY working correctly on the original,
            # fully-unmodified text into something worse. findBalanced()
            # on the original text would match the whole (globally-
            # balanced) span as one piece, and if a pipe happens to
            # hide embedded garbage behind a clean label, that already
            # produces correct output -- but collapsing just the opens
            # makes the first inner closing pair look like a complete,
            # separate match on its own, ending too early and
            # stranding everything after it as newly-visible leftover
            # text that was never visible before. Leave this run
            # completely untouched instead, and defer entirely to what
            # findBalanced() would already do.
            result.append(text[i:j])
            cur = j
            pos = j
            continue
        # No natural close found at all anywhere in the rest of the
        # text. Collapsing just the opens is safe here too, for the
        # same reason as the "nothing follows" case above: there's
        # nothing left afterward that could be stranded.
        result.append('[[')
        cur = j
        pos = j
    result.append(text[cur:])
    return ''.join(result)


_bracketPairRE = re.compile(r'\[\[|\]\]')
_tripleOpenRE = re.compile(r'\[{3,}')

# Reused by collapseDoubledLinkBrackets()'s pseudo-prefix findBalanced()
# call below -- allocated once at module load rather than as fresh list
# objects on every call.
_LINK_OPEN_DELIM = ['[[']
_LINK_CLOSE_DELIM = [']]']


def findUnclosedLinkOpenPositions(text):
    """
    Return the character positions of "[[" occurrences that never find
    a matching "]]" within the given text, using simple stack logic
    that mirrors findBalanced()'s own semantics (an opening delimiter
    that's still on the stack once the text ends never gets yielded as
    part of any match).

    Implemented via a single compiled-regex scan (finditer) rather than
    a manual character-by-character loop: measured ~9x faster on
    realistic article-length text with identical results, since regex
    scanning runs as compiled code rather than interpreted per-
    character indexing.
    """
    stack = []
    for m in _bracketPairRE.finditer(text):
        if m.group() == '[[':
            stack.append(m.start())
        elif stack:
            stack.pop()
    return stack


def neutralizeUnclosedLinkOpens(text):
    """
    A single genuinely unclosed link opening -- e.g. [[1198 (a real,
    very ordinary wikitext typo: someone forgot the closing "]]") --
    would otherwise silently disable ALL SUBSEQUENT link conversion for
    the rest of the article. This happens because findBalanced()'s
    stack-based matcher only yields a match once the stack returns to
    completely empty; one permanently-unmatched opening means the
    stack can never empty again for anything that follows, even
    several unrelated, perfectly well-formed links later in the same
    article.

    This neutralizes exactly the genuinely-unclosed openings (found via
    findUnclosedLinkOpenPositions(), not merely "the first N excess
    opens" -- a naive count-based heuristic could misidentify an
    earlier, properly-closed link as the broken one) by temporarily
    replacing them with a placeholder that findBalanced() won't
    recognize as an opening delimiter. The placeholder is restored back
    to a literal "[[" afterward, so the malformed link itself is left
    exactly as broken as it was -- only its blast radius on later,
    unrelated links is contained.

    Deliberately NOT bounded to a single paragraph: findBalanced() and
    findUnclosedLinkOpenPositions() use the exact same LIFO stack
    logic, so this never "blames" a bracket that findBalanced() itself
    wouldn't already treat the same way on the raw, uncollapsed text --
    it only prevents one permanently-unmatched opening from poisoning
    everything after it. Paragraph-bounding was tried first, but
    causes a worse problem: a File: link whose citation/caption
    content spans a blank line (legitimate, if untidy, wikitext) gets
    its true closing "]]" treated as out of reach, so the whole link
    survives as literal text instead of being dropped normally.
    Whole-text detection avoids this, since the true close is still
    found and paired the same way findBalanced() would pair it anyway.
    """
    unclosed = findUnclosedLinkOpenPositions(text)
    if not unclosed:
        return text
    result = []
    cur = 0
    for pos in unclosed:
        result.append(text[cur:pos])
        result.append(LINK_OPEN_PLACEHOLDER)
        cur = pos + 2
    result.append(text[cur:])
    return ''.join(result)


# Placeholder used by neutralizeUnclosedLinkOpens(); chosen to be a
# control-character sequence that can never legitimately appear in
# wikitext, and restored back to a literal "[[" once link processing
# for the rest of the text has completed.
LINK_OPEN_PLACEHOLDER = '\x00\x00'


def replaceInternalLinks(text, extractor):
    """
    Replaces external links of the form:
    [[title |...|label]]trail

    with title concatenated with trail, when present, e.g. 's' for plural.
    """
    # call this after removal of external links, so we need not worry about
    # triple closing ]]].
    text = collapseDoubledLinkBrackets(text)
    text = neutralizeUnclosedLinkOpens(text)

    cur = 0
    res = ''
    for s, e in findBalanced(text, ['[['], [']]']):
        m = tailRE.match(text, e)
        if m:
            trail = m.group(0)
            end = m.end()
        else:
            trail = ''
            end = e
        inner = text[s + 2:e - 2]
        # find first |
        pipe = inner.find('|')
        if pipe < 0:
            title = inner
            label = title
        else:
            title = inner[:pipe].rstrip()
            # find last |
            curp = pipe + 1
            for s1, e1 in findBalanced(inner, ['[['], [']]']):
                last = inner.rfind('|', curp, s1)
                if last >= 0:
                    pipe = last  # advance
                curp = e1
            label = inner[pipe + 1:].strip()
        res += text[cur:s] + makeInternalLink(title, label, extractor) + trail
        cur = end
    return (res + text[cur:]).replace(LINK_OPEN_PLACEHOLDER, '[[')


def makeInternalLink(title, label, extractor):
    colon = title.find(':')
    if colon > 0 and title[:colon] not in extractor.acceptedNamespaces:
        return ''
    if colon == 0:
        # drop also :File:
        colon2 = title.find(':', colon + 1)
        if colon2 > 1 and title[colon + 1:colon2] not in extractor.acceptedNamespaces:
            return ''
    if extractor.keepLinks:
        return '<a href="%s">%s</a>' % (urlencode(title), label)
    else:
        return label


# ----------------------------------------------------------------------
# variables


class MagicWords():

    """
    One copy in each Extractor.

    @see https://doc.wikimedia.org/mediawiki-core/master/php/MagicWord_8php_source.html
    """
    names = [
        '!',
        'currentmonth',
        'currentmonth1',
        'currentmonthname',
        'currentmonthnamegen',
        'currentmonthabbrev',
        'currentday',
        'currentday2',
        'currentdayname',
        'currentyear',
        'currenttime',
        'currenthour',
        'localmonth',
        'localmonth1',
        'localmonthname',
        'localmonthnamegen',
        'localmonthabbrev',
        'localday',
        'localday2',
        'localdayname',
        'localyear',
        'localtime',
        'localhour',
        'numberofarticles',
        'numberoffiles',
        'numberofedits',
        'articlepath',
        'pageid',
        'sitename',
        'server',
        'servername',
        'scriptpath',
        'stylepath',
        'pagename',
        'pagenamee',
        'fullpagename',
        'fullpagenamee',
        'namespace',
        'namespacee',
        'namespacenumber',
        'currentweek',
        'currentdow',
        'localweek',
        'localdow',
        'revisionid',
        'revisionday',
        'revisionday2',
        'revisionmonth',
        'revisionmonth1',
        'revisionyear',
        'revisiontimestamp',
        'revisionuser',
        'revisionsize',
        'subpagename',
        'subpagenamee',
        'talkspace',
        'talkspacee',
        'subjectspace',
        'subjectspacee',
        'talkpagename',
        'talkpagenamee',
        'subjectpagename',
        'subjectpagenamee',
        'numberofusers',
        'numberofactiveusers',
        'numberofpages',
        'currentversion',
        'rootpagename',
        'rootpagenamee',
        'basepagename',
        'basepagenamee',
        'currenttimestamp',
        'localtimestamp',
        'directionmark',
        'contentlanguage',
        'numberofadmins',
        'cascadingsources',
    ]

    def __init__(self):
        self.values = {'!': '|'}

    def __getitem__(self, name):
        return self.values.get(name)

    def __setitem__(self, name, value):
        self.values[name] = value

    switches = (
        '__NOTOC__',
        '__FORCETOC__',
        '__TOC__',
        '__TOC__',
        '__NEWSECTIONLINK__',
        '__NONEWSECTIONLINK__',
        '__NOGALLERY__',
        '__HIDDENCAT__',
        '__NOCONTENTCONVERT__',
        '__NOCC__',
        '__NOTITLECONVERT__',
        '__NOTC__',
        '__START__',
        '__END__',
        '__INDEX__',
        '__NOINDEX__',
        '__STATICREDIRECT__',
        '__DISAMBIG__',
        '__NOEDITSECTION__',
    )


magicWordsRE = re.compile('|'.join(MagicWords.switches))


# =========================================================================
#
# MediaWiki Markup Grammar
# https://www.mediawiki.org/wiki/Preprocessor_ABNF

# xml-char = %x9 / %xA / %xD / %x20-D7FF / %xE000-FFFD / %x10000-10FFFF
# sptab = SP / HTAB

# ; everything except ">" (%x3E)
# attr-char = %x9 / %xA / %xD / %x20-3D / %x3F-D7FF / %xE000-FFFD / %x10000-10FFFF

# literal         = *xml-char
# title           = wikitext-L3
# part-name       = wikitext-L3
# part-value      = wikitext-L3
# part            = ( part-name "=" part-value ) / ( part-value )
# parts           = [ title *( "|" part ) ]
# tplarg          = "{{{" parts "}}}"
# template        = "{{" parts "}}"
# link            = "[[" wikitext-L3 "]]"

# comment         = "<!--" literal "-->"
# unclosed-comment = "<!--" literal END
# ; the + in the line-eating-comment rule was absent between MW 1.12 and MW 1.22
# line-eating-comment = LF LINE-START *SP +( comment *SP ) LINE-END

# attr            = *attr-char
# nowiki-element  = "<nowiki" attr ( "/>" / ( ">" literal ( "</nowiki>" / END ) ) )

# wikitext-L2     = heading / wikitext-L3 / *wikitext-L2
# wikitext-L3     = literal / template / tplarg / link / comment /
#                   line-eating-comment / unclosed-comment / xmlish-element /
#                   *wikitext-L3

# ------------------------------------------------------------------------------

lineBreakTags = ('br', 'hr')
selfClosingTags = ('nobr', 'ref', 'references', 'nowiki', 'templatestyles', 'section')

# Block-level by default HTML semantics (a real browser renders an
# implicit line break around each of these), unlike the rest of
# ignoredTags below, which are genuinely inline (span, b, i, etc. --
# no implied break at all). Stripped the same tag-syntax-removed,
# content-kept way, but via a newline substitution (see
# substituteLineBreakTag()) rather than plain deletion -- otherwise
# two adjacent blocks with no whitespace between them in the source
# fuse into one run-on string, the same class of bug as the br/hr
# word-merging fix, just for a different set of tags. A newline
# specifically (not just a space) to match how compact() already
# treats section/paragraph boundaries elsewhere in this file: wikitext
# "==heading==" is only recognized when it's on its own line
# (section.match(line), applied line-by-line via text.split('\n')) --
# an HTML heading should end up the same way, as its own line, not
# merged onto the same line as surrounding prose.
#
# div is deliberately NOT included here yet, despite also being
# block-level: it's registered in BOTH ignoredTags and
# discardElements (see the comment there), and working out how that
# interacts with a newline-substitution step is a separate piece of
# work. blockquote isn't included either, out of the same deliberate,
# narrow scope -- just p, center, and the headers for now.
blockSeparatorTags = ('p', 'center', 'h1', 'h2', 'h3', 'h4')

# These tags are dropped, keeping their content.
# handle 'a' separately, depending on keepLinks
ignoredTags = (
    'abbr', 'b', 'bdi', 'big', 'blockquote', 'cite', 'div', 'em',
    'font', 'hiero', 'i', 'kbd', 'nowiki',
    'plaintext', 'poem',
    # ruby wraps an East Asian ruby annotation and rb the base text
    # inside it; the reading is discarded separately (see
    # discardElements).
    'rb', 'ruby',
    's', 'section', 'span', 'strike', 'strong',
    'sub', 'sup', 'tt', 'u', 'var'
)

placeholder_tags = {'math': 'formula', 'code': 'codice'}


def normalizeTitle(title, known_namespaces=None):
    """Normalize title"""
    known_namespaces = known_namespaces if known_namespaces is not None else _DEFAULT_KNOWN_NAMESPACES
    # remove leading/trailing whitespace and underscores
    title = title.strip(' _')
    # replace sequences of whitespace and underscore chars with a single space
    title = re.sub(r'[\s_]+', ' ', title)

    m = re.match(r'([^:]*):(\s*)(\S(?:.*))', title)
    if m:
        prefix = m.group(1)
        if m.group(2):
            optionalWhitespace = ' '
        else:
            optionalWhitespace = ''
        rest = m.group(3)

        ns = normalizeNamespace(prefix)
        if ns in known_namespaces:
            # If the prefix designates a known namespace, then it might be
            # followed by optional whitespace that should be removed to get
            # the canonical page name
            # (e.g., "Category:  Births" should become "Category:Births").
            title = ns + ":" + ucfirst(rest)
        else:
            # No namespace, just capitalize first letter.
            # If the part before the colon is not a known namespace, then we
            # must not remove the space after the colon (if any), e.g.,
            # "3001: The_Final_Odyssey" != "3001:The_Final_Odyssey".
            # However, to get the canonical page name we must contract multiple
            # spaces into one, because
            # "3001:   The_Final_Odyssey" != "3001: The_Final_Odyssey".
            title = ucfirst(prefix) + ":" + optionalWhitespace + ucfirst(rest)
    else:
        # no namespace, just capitalize first letter
        title = ucfirst(title)
    return title


def unescape(text):
    """
    Removes HTML or XML character references and entities from a text string.

    :param text The HTML (or XML) source text.
    :return The plain text, as a Unicode string, if necessary.
    """

    def fixup(m):
        text = m.group(0)
        code = m.group(1)
        try:
            if text[1] == "#":  # character reference
                if text[2] == "x":
                    return chr(int(code[1:], 16))
                else:
                    return chr(int(code))
            else:  # named entity
                return chr(name2codepoint[code])
        except:
            return text  # leave as is

    return re.sub(r"&#?(\w+);", fixup, text)


# Match HTML comments
# The buggy template {{Template:T}} has a comment terminating with just "->"
comment = re.compile(r'<!--.*?-->', re.DOTALL)


def ignoreTag(tag):
    """Compiles and returns the (open, close) regex pair for a tag to
    be dropped from output, including its content. A pure function --
    doesn't append anywhere itself, unlike its old behavior -- so the
    caller decides what list this belongs in (an Extractor's own
    per-instance ignored_tag_patterns, most commonly)."""
    left = re.compile(r'<%s\b.*?>' % tag, re.IGNORECASE | re.DOTALL)  # both <ref> and <reference>
    right = re.compile(r'</\s*%s\s*>' % tag, re.IGNORECASE)  # space allowed, such as </span >
    return (left, right)


# Match ignored tags. Static, import-time-only default -- never mutated
# after this point (ignoreTag() itself no longer mutates anything).
# WikiExtractor.py builds its own per-run list, starting from a copy of
# this default and adding 'a' when --links isn't given, and passes that
# explicitly into each Extractor -- see clean.py's own clean_markup()
# for the same pattern at smaller scale (add 'a' or don't, no shared
# state to save/restore around it either way).
_DEFAULT_IGNORED_TAG_PATTERNS = [ignoreTag(tag) for tag in ignoredTags]

# Match selfClosing HTML tags
selfClosing_tag_patterns = [
    # nobr is treated the same permissive way as br/hr (optional
    # trailing slash) -- a bare, unclosed <nobr> is a stray tag like
    # them, but "no line break" doesn't call for inserting a space
    # where the tag was, so it stays here rather than moving to
    # lineBreak_tag_pattern.
    #
    # ref/references/nowiki/templatestyles are NOT treated this way:
    # for ref specifically, self-closing has a distinct, real meaning
    # (<ref name="x" /> reuses an earlier-defined reference) from the
    # paired form (<ref name="x">real citation text</ref>) -- making
    # the slash optional would misidentify a real paired tag's opening
    # as self-closing. templatestyles is always self-closing in real
    # usage, so the strict pattern loses nothing there either.
    # (?=(...))\1 emulates an atomic/possessive match for the quoted
    # alternatives (see lineBreak_tag_pattern below) -- without it, a
    # literal '>' inside a quoted attribute value (e.g.
    # <ref style="a > b" />) would prevent matching at all, since
    # [^>]* alone stops at that inner '>' and never finds the real,
    # required trailing '/'.
    re.compile(r'''<\s*%s\b(?:(?=("[^"]*"|'[^']*'|[^>]))\1)*/?\s*>''' % tag if tag == 'nobr'
               else r'''<\s*%s\b(?:(?=("[^"]*"|'[^']*'|[^>]))\1)*/\s*>''' % tag,
               re.DOTALL | re.IGNORECASE)
    for tag in selfClosingTags
]

# br/hr carry genuine line-break semantics (see the substitution site
# in clean() above): matched the same permissive way as nobr (trailing
# slash optional, since old-style HTML4 syntax like <br clear=all> --
# or even a bare <br> -- is just as valid a "line break" instance as
# <br/>), but substituted with a space instead of bulk-deleted.
#
# Combined into ONE pattern (all tags' opening and closing forms
# joined with '|') rather than a separate compiled pattern per tag,
# each looped over its own substituteLineBreakTag() call: measured
# directly on realistic article text that substituteLineBreakTag()'s
# own per-call Python overhead (building a result list, checking
# whitespace boundaries per match) dominates over the regex engine's
# own matching cost here, unlike a plain re.sub() -- one combined call
# was ~2.6x faster than four separate ones for lineBreak_tag_pattern,
# and ~6.6x faster for the larger block_separator_tag_pattern below,
# with verified identical output on real data either way.
#
# Only the opening-tag half needs the group-per-tag naming below: its
# (?=(...))\1 atomic-group-emulation trick uses a capturing group,
# and naively joining multiple copies of that trick with '|' breaks
# it -- confirmed directly: alternative N's own \1, once combined,
# ends up referring to alternative 0's group (renumbered from that
# alternative's own local group 1 to the combined pattern's own,
# different numbering), which never participated in a match where a
# later alternative is what actually matched, so the backreference
# silently fails and that entire alternative stops matching anything
# at all. Named groups (?P<lb0>...)/(?P=lb0), one name per tag, avoid
# this entirely. The closing-tag half has no groups at all, so it
# doesn't need this.
lineBreak_tag_pattern = re.compile(
    '|'.join(
        [r'''<\s*%s\b(?:(?=(?P<lb%d>"[^"]*"|'[^']*'|[^>]))(?P=lb%d))*>''' % (tag, i, i)
         for i, tag in enumerate(lineBreakTags)]
        # br/hr are void elements -- a closing tag is invalid HTML,
        # but a real, if malformed, editing mistake (someone writing
        # </br> as if it needed a matching close, same instinct as
        # XHTML-style self-closing syntax). No attributes are possible
        # on a closing tag, so no quote-aware matching is needed here.
        + [r'</\s*%s\s*>' % tag for tag in lineBreakTags]
    ),
    re.DOTALL | re.IGNORECASE)

# blockSeparatorTags (see the comment there) are substituted with a
# newline rather than a space, via the same substituteLineBreakTag()
# mechanism -- each tag contributes its own opening AND closing half,
# since (unlike br/hr, which are single, self-closing tags) these have
# two distinct halves, each appearing at a different position and
# needing its own independent substitution. All halves for all tags
# are joined into one combined pattern -- see lineBreak_tag_pattern
# above for why. No capturing groups are involved in any of these
# (unlike lineBreak's opening half), so no group-naming is needed here.
# Same shapes as ignoreTag()'s own left/right patterns, for
# consistency: opening requires the tag name immediately after '<',
# matching real HTML tokenizer behavior (a bare '< p>' is not treated
# as a tag at all by real parsers, so this shouldn't either); closing
# tolerates whitespace on either side of the name.
block_separator_tag_pattern = re.compile(
    '|'.join(
        part
        for tag in blockSeparatorTags
        for part in (r'<%s\b.*?>' % tag, r'</\s*%s\s*>' % tag)
    ),
    re.IGNORECASE | re.DOTALL)


def substituteLineBreakTag(pattern, text, separator=' '):
    """
    Replace each match of a line-break-like tag pattern (br/hr, or a
    blockSeparatorTags opening/closing half) with `separator`, EXCEPT
    when the match sits at the very start/end of the text or is
    already adjacent to whitespace (any kind -- space, tab, newline,
    non-breaking space -- not just newline specifically) on one side
    -- in that case, omit the separator on that side entirely, since
    there's already something separating it from whatever's there, or
    nothing at all to separate it from. Checking for any whitespace
    rather than just the specific separator character matters:
    without it, this function can produce a doubled-up separator on
    its own when the source already had one adjacent to the tag
    (verified directly for the space case), and isn't otherwise
    self-sufficient -- relying on some other, unrelated part of the
    pipeline to clean up after it is fragile compared to just not
    creating the extra separator to begin with.

    Verified this doesn't over-match invisible RTL-script formatting
    characters that aren't real separators (e.g. zero-width
    non-joiner/joiner, the Arabic letter mark, all common in the
    Perso-Arabic-script wikis this project works with) -- Python's
    str.isspace() correctly excludes those while still recognizing a
    non-breaking space as a genuine (if non-wrapping) separator.
    """
    result = []
    cur = 0
    n = len(text)
    for m in pattern.finditer(text):
        if m.start() < cur:
            continue  # overlapping match already consumed
        result.append(text[cur:m.start()])
        before_is_boundary = (m.start() == 0) or text[m.start() - 1].isspace()
        after_is_boundary = (m.end() == n) or text[m.end()].isspace()
        if not (before_is_boundary or after_is_boundary):
            result.append(separator)
        cur = m.end()
    result.append(text[cur:])
    return ''.join(result)

# Match HTML placeholder tags
placeholder_tag_patterns = [
    (re.compile(r'<\s*%s(\s*| [^>]+?)>.*?<\s*/\s*%s\s*>' % (tag, tag), re.DOTALL | re.IGNORECASE),
     repl) for tag, repl in placeholder_tags.items()
]

# Match preformatted lines
preformatted = re.compile(r'^ .*?$')

# Match external links (space separates second optional parameter)
externalLink = re.compile(r'\[\w+[^ ]*? (.*?)]')
externalLinkNoAnchor = re.compile(r'\[\w+[&\]]*\]')

# Matches bold/italic
bold_italic = re.compile(r"'''''(.*?)'''''")
bold = re.compile(r"'''(.*?)'''")
italic_quote = re.compile(r"''\"([^\"]*?)\"''")
italic = re.compile(r"''(.*?)''")
quote_quote = re.compile(r'""([^"]*?)""')

# Matches space
spaces = re.compile(r' {2,}')

# Matches dots
dots = re.compile(r'\.{4,}')

# ======================================================================

class Template(list):
    """
    A Template is a list of TemplateText or TemplateArgs
    """

    @staticmethod
    def parse(body):
        tpl = Template()
        # we must handle nesting, s.a.
        # {{{1|{{PAGENAME}}}
        # {{{italics|{{{italic|}}}
        # {{#if:{{{{{#if:{{{nominee|}}}|nominee|candidate}}|}}}|
        #
        start = 0
        for s, e in findMatchingBraces(body, 3):
            # findMatchingBraces() only resolves one level of the
            # more-than-3-braces ambiguity, leaving further,
            # immediately-adjacent brace layers as opaque text. Real
            # MediaWiki syntax allows indefinite nesting here -- a
            # parameter whose name is itself a parameter, arbitrarily
            # deep. So widen the span outward by 3 for each additional
            # adjacent "{"/"}" layer before treating what's left as
            # this tplarg's own content -- the normal recursive
            # Template.parse() call on that (wider) content then
            # discovers each further nested layer in turn.
            while (s >= 3 and e + 3 <= len(body)
                   and body[s - 3:s] == '{{{' and body[e:e + 3] == '}}}'):
                s -= 3
                e += 3
            tpl.append(TemplateText(body[start:s]))
            tpl.append(TemplateArg(body[s+3:e-3]))
            start = e
        tpl.append(TemplateText(body[start:])) # leftover
        return tpl

    def subst(self, params, extractor, depth=0):
        # We perform parameter substitutions recursively.
        # We also limit the maximum number of iterations to avoid too long or
        # even endless loops (in case of malformed input).

        # :see: http://meta.wikimedia.org/wiki/Help:Expansion#Distinction_between_variables.2C_parser_functions.2C_and_templates
        #
        # Parameter values are assigned to parameters in two (?) passes.
        # Therefore a parameter name in a template can depend on the value of
        # another parameter of the same template, regardless of the order in
        # which they are specified in the template call, for example, using
        # Template:ppp containing "{{{{{{p}}}}}}", {{ppp|p=q|q=r}} and even
        # {{ppp|q=r|p=q}} gives r, but using Template:tvvv containing
        # "{{{{{{{{{p}}}}}}}}}", {{tvvv|p=q|q=r|r=s}} gives s.

        logger.debug('subst tpl (%d, %d) %s', len(extractor.frame), depth, self)

        if depth > extractor.maxParameterRecursionLevels:
            extractor.recursion_exceeded_3_errs += 1
            return ''

        return ''.join([tpl.subst(params, extractor, depth) for tpl in self])

    def __str__(self):
        return ''.join([str(x) for x in self])


class TemplateText(str):
    """Fixed text of template"""

    def subst(self, params, extractor, depth):
        return self


class TemplateArg():
    """
    parameter to a template.
    Has a name and a default value, both of which are Templates.
    """
    def __init__(self, parameter):
        """
        :param parameter: the parts of a tplarg.
        """
        # the parameter name itself might contain templates, e.g.:
        #   appointe{{#if:{{{appointer14|}}}|r|d}}14|
        #   4|{{{{{subst|}}}CURRENTYEAR}}

        # any parts in a tplarg after the first (the parameter default) are
        # ignored, and an equals sign in the first part is treated as plain text.
        #logger.debug('TemplateArg %s', parameter)

        parts = splitParts(parameter)
        self.name = TemplateArg._parse_template(parts[0])
        if len(parts) > 1:
            # This parameter has a default value
            self.default = TemplateArg._parse_template(parts[1])
        else:
            self.default = None

    @staticmethod
    @lru_cache(maxsize=10000)
    def _parse_template(arg):
        return Template.parse(arg)

    def __str__(self):
        if self.default:
            return '{{{%s|%s}}}' % (self.name, self.default)
        else:
            return '{{{%s}}}' % self.name

    def subst(self, params, extractor, depth):
        """
        Substitute value for this argument from dict :param params:
        Use :param extractor: to evaluate expressions for name and default.
        Limit substitution to the maximun :param depth:.
        """
        # the parameter name itself might contain templates, e.g.:
        # appointe{{#if:{{{appointer14|}}}|r|d}}14|
        paramName = self.name.subst(params, extractor, depth+1)
        paramName = extractor.expandTemplates(paramName)
        res = ''
        if paramName in params:
            res = params[paramName]  # use parameter value specified in template invocation
        elif self.default:            # use the default value
            defaultValue = self.default.subst(params, extractor, depth+1)
            res =  extractor.expandTemplates(defaultValue)
        #logger.debug('subst arg %d %s -> %s' % (depth, paramName, res))
        return res


# ======================================================================

substWords = 'subst:|safesubst:'
# Pre-compiled once here rather than passing the raw substWords string
# to module-level re.match()/re.sub() on every expandTemplate() call
# (once per template invocation -- confirmed via profiling a real
# extraction run to be a real, avoidable cost, same underlying issue
# as findMatchingBraces()'s own pattern recompilation). One compiled
# Pattern object supports both .match() and .sub().
_SUBST_WORDS_RE = re.compile(substWords, re.IGNORECASE)
# Same reasoning, for templateParams()'s own per-parameter split --
# called once per parameter of every template invocation, so this one
# runs even more often than _SUBST_WORDS_RE above.
_TEMPLATE_PARAM_RE = re.compile(r" *([^=']*?) *=(.*)", re.DOTALL)


def escapeDocAttribute(value):
    """Escape a value for use inside a double-quoted attribute of the
    <doc> tag.

    Article titles arrive as written -- '"Weird Al" Yankovic', 'AT&T',
    'Nokia 3310 <> 3410' -- and each of ", &, < and > ends the
    attribute or the tag early for anything that parses the output as
    markup.

    Apostrophes are left alone. They are safe between double quotes,
    and escaping them would rewrite a large share of all titles for no
    gain, which is also why html.escape's quote=True is not used here.
    """
    return html.escape(str(value), quote=False).replace('"', '&quot;')


class Extractor():
    """
    An extraction task on a article.
    """

    def __init__(self, id, revid, urlbase, title, page, templates=None, redirects=None,
                 templatePrefix='', knownNamespaces=None, acceptedNamespaces=None,
                 ignored_tag_patterns=None, keepLinks=False, keepSections=True,
                 HtmlFormatting=False, to_json=False, to_text=False, discard_empty=False):
        """
        :param page: a list of lines.
        :param templates: the {title: text} template lookup this
            extraction should use -- a plain dict, populated via
            define_template(), or anything else supporting `in`/`[]`,
            e.g. a template_blob.CompactedTemplates view. Defaults to
            a fresh, empty dict when not given -- not to any shared
            global -- so a caller that doesn't care about templates
            (most unit tests) simply gets none, isolated from
            whatever any other caller or test elsewhere in the same
            process has loaded; there is no longer a module-level
            `templates` for this to silently depend on. A caller that
            DOES need real templates (extract_process(), the
            --article path) passes its own dict or CompactedTemplates
            view here explicitly.
        :param redirects: the {title: target_title} redirect lookup,
            same shape and same defaulting behavior as templates
            above -- also no longer a module-level global.
        :param templatePrefix: :param knownNamespaces: :param
            acceptedNamespaces: :param ignored_tag_patterns: :param
            keepLinks: :param keepSections: :param HtmlFormatting:
            :param to_json: :param to_text: :param discard_empty:
            all formerly module- or class-level shared state (some of
            it, for knownNamespaces/acceptedNamespaces, genuinely
            broken shared state -- see the module-level defaults
            below for why), now plain constructor arguments instead,
            for the same reason templates/redirects are: no global,
            of any kind, for a spawned worker process to silently miss
            inheriting. Each defaults to a fresh copy of this module's
            own static, never-mutated-after-import default when not
            given, so existing callers that don't care about these
            (most unit tests) see unchanged behavior. A caller that
            DOES need real, CLI-configured values (extract_process(),
            the --article path) builds and passes its own values
            explicitly -- see WikiExtractor.py's own build_extractor_kwargs().
        """
        self.id = id
        self.revid = revid
        self.url = get_url(urlbase, id)
        self.title = title
        self.page = page
        self.templates = templates if templates is not None else {}
        self.redirects = redirects if redirects is not None else {}
        self.templatePrefix = templatePrefix
        self.knownNamespaces = knownNamespaces if knownNamespaces is not None else set(_DEFAULT_KNOWN_NAMESPACES)
        self.acceptedNamespaces = (acceptedNamespaces if acceptedNamespaces is not None
                                    else list(_DEFAULT_ACCEPTED_NAMESPACES))
        self.ignored_tag_patterns = (ignored_tag_patterns if ignored_tag_patterns is not None
                                      else list(_DEFAULT_IGNORED_TAG_PATTERNS))
        self.keepLinks = keepLinks
        self.keepSections = keepSections
        self.HtmlFormatting = HtmlFormatting
        self.to_json = to_json
        self.to_text = to_text
        self.discard_empty = discard_empty
        self.magicWords = MagicWords()
        self.frame = []
        self.recursion_exceeded_1_errs = 0  # template recursion within expandTemplates()
        self.recursion_exceeded_2_errs = 0  # template recursion within expandTemplate()
        self.recursion_exceeded_3_errs = 0  # parameter recursion
        self.template_title_errs = 0
        self.template_loop_errs = 0  # same (title, params) reappearing in its own expansion chain
        self.warned_loop_keys = set()  # (id, title) pairs already warned about, to avoid log spam
        self.malformed_expr_errs = 0
        self.warned_expr_keys = set()  # (id, expr) pairs already warned about, to avoid log spam

    def clean_text(self, text, mark_headers=False, expand_templates=True,
                   html_safe=True):
        """
        :param mark_headers: True to distinguish headers from paragraphs
          e.g. "## Section 1"
        """
        self.magicWords['namespace'] = self.title[:max(0, self.title.find(":"))]
        #self.magicWords['namespacenumber'] = '0' # for article, 
        self.magicWords['pagename'] = self.title
        self.magicWords['fullpagename'] = self.title
        self.magicWords['currentyear'] = time.strftime('%Y')
        self.magicWords['currentmonth'] = time.strftime('%m')
        self.magicWords['currentday'] = time.strftime('%d')
        self.magicWords['currenthour'] = time.strftime('%H')
        self.magicWords['currenttime'] = time.strftime('%H:%M:%S')

        text = clean(self, text, expand_templates=expand_templates,
                     html_safe=html_safe)

        text = compact(text, mark_headers=mark_headers, extractor=self)
        return text

    def extract(self, out, html_safe=True):
        """
        :param out: a memory file.
        :param html_safe: whether to escape HTML entities.
        """
        logger.debug("%s\t%s", self.id, self.title)
        text = ''.join(self.page)
        text = self.clean_text(text, html_safe=html_safe)

        if self.discard_empty and not any(t.strip() for t in text):
            pass
        elif self.to_json:
            json_data = {
		'id': self.id,
                'revid': self.revid,
                'url': self.url,
                'title': self.title,
                'text': "\n".join(text)
            }
            out_str = json.dumps(json_data)
            out.write(out_str)
            out.write('\n')
        elif self.to_text:
            out.write('\n'.join(text))
            out.write('\n\n\n')
        else:
            header = '<doc id="%s" url="%s" title="%s">\n' % (
                escapeDocAttribute(self.id),
                escapeDocAttribute(self.url),
                escapeDocAttribute(self.title))
            # The title also opens the document as ordinary text, so
            # it is escaped the way clean_text() escapes the body:
            # &, < and > become entities, quotes stay as they are.
            title_line = html.escape(self.title, quote=False) if html_safe else self.title
            # Separate header from text with a newline.
            header += title_line + '\n\n'
            footer = "\n</doc>\n"
            out.write(header)
            out.write('\n'.join(text))
            out.write('\n')
            out.write(footer)

        errs = (self.template_title_errs,
                self.recursion_exceeded_1_errs,
                self.recursion_exceeded_2_errs,
                self.recursion_exceeded_3_errs,
                self.template_loop_errs,
                self.malformed_expr_errs)
        if any(errs):
            # DETAIL, not WARNING and not DEBUG: on a real, full-wiki
            # run, a single common broken shared template can leave a
            # large fraction of all articles with some nonzero count
            # here, which turns "one WARNING line per article" into
            # hundreds of thousands of lines -- individually genuine,
            # but collectively drowning out everything else in the
            # log. extract_process() in WikiExtractor.py now
            # aggregates these same six counters across every article
            # a worker processes and logs one WARNING-level summary
            # per worker when it finishes; that's the line to look at
            # first for gauging scope. This per-article line is for
            # the next step down -- digging into which specific pages
            # have issues -- without requiring full DEBUG tracing
            # (which also includes low-level extraction mechanics
            # entirely unrelated to this, and is vastly more
            # voluminous). See DETAIL's own definition above.
            logger.log(DETAIL, "Template errors in article '%s' (%s): title(%d) recursion(%d, %d, %d) "
                       "loop(%d) expr(%d)",
                       self.title, self.id, *errs)

    # ----------------------------------------------------------------------
    # Expand templates

    maxTemplateRecursionLevels = 30
    maxParameterRecursionLevels = 16

    # check for template beginning
    reOpen = re.compile(r'(?<!{){{(?!{)', re.DOTALL)

    def expandTemplates(self, wikitext):
        """
        :param wikitext: the text to be expanded.

        Templates are frequently nested. Occasionally, parsing mistakes may
        cause template insertion to enter an infinite loop, for instance when
        trying to instantiate Template:Country

        {{country_{{{1}}}|{{{2}}}|{{{2}}}|size={{{size|}}}|name={{{name|}}}}}

        which is repeatedly trying to insert template 'country_', which is
        again resolved to Template:Country. The straightforward solution of
        keeping track of templates that were already inserted for the current
        article would not work, because the same template may legally be used
        more than once, with different parameters in different parts of the
        article.  Therefore, we limit the number of iterations of nested
        template inclusion.

        """
        # Test template expansion at:
        # https://en.wikipedia.org/wiki/Special:ExpandTemplates

        res = ''
        if len(self.frame) >= self.maxTemplateRecursionLevels:
            self.recursion_exceeded_1_errs += 1
            return res

        # logger.debug('<expandTemplates ' + str(len(self.frame)))

        # See _mask_nowiki()'s own comment above for why this has to
        # happen here, on every call, rather than once on the
        # original article text.
        wikitext, nowiki_placeholders = _mask_nowiki(wikitext)

        cur = 0
        # look for matching {{...}}
        for s, e in findMatchingBraces(wikitext, 2):
            res += wikitext[cur:s] + self.expandTemplate(wikitext[s + 2:e - 2])
            cur = e
        # leftover
        res += wikitext[cur:]
        res = _unmask_nowiki(res, nowiki_placeholders)
        # logger.debug('   expandTemplates> %d %s', len(self.frame), res)
        return res

    def templateParams(self, parameters):
        """
        Build a dictionary with positional or name key to expanded parameters.
        :param parameters: the parts[1:] of a template, i.e. all except the title.
        """
        templateParams = {}

        if not parameters:
            return templateParams
        # guarded by isEnabledFor() for efficiency
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('<templateParams: %s', '|'.join(parameters))

        # Parameters can be either named or unnamed. In the latter case, their
        # name is defined by their ordinal position (1, 2, 3, ...).

        unnamedParameterCounter = 0

        # It's legal for unnamed parameters to be skipped, in which case they
        # will get default values (if available) during actual instantiation.
        # That is {{template_name|a||c}} means parameter 1 gets
        # the value 'a', parameter 2 value is not defined, and parameter 3 gets
        # the value 'c'.  This case is correctly handled by function 'split',
        # and does not require any special handling.
        for param in parameters:
            # Spaces before or after a parameter value are normally ignored,
            # UNLESS the parameter contains a link (to prevent possible gluing
            # the link to the following text after template substitution)

            # Parameter values may contain "=" symbols, hence the parameter
            # name extends up to the first such symbol.

            # It is legal for a parameter to be specified several times, in
            # which case the last assignment takes precedence. Example:
            # "{{t|a|b|c|2=B}}" is equivalent to "{{t|a|B|c}}".
            # Therefore, we don't check if the parameter has been assigned a
            # value before, because anyway the last assignment should override
            # any previous ones.
            # FIXME: Don't use DOTALL here since parameters may be tags with
            # attributes, e.g. <div class="templatequotecite">
            # Parameters may span several lines, like:
            # {{Reflist|colwidth=30em|refs=
            # &lt;ref name=&quot;Goode&quot;&gt;Title&lt;/ref&gt;

            # The '=' might occurr within an HTML attribute:
            #   "&lt;ref name=value"
            # but we stop at first.

            # The '=' might occurr within quotes:
            # ''''<span lang="pt-pt" xml:lang="pt-pt">cénicas</span>'''

            m = _TEMPLATE_PARAM_RE.match(param)
            if m:
                # This is a named parameter.  This case also handles parameter
                # assignments like "2=xxx", where the number of an unnamed
                # parameter ("2") is specified explicitly - this is handled
                # transparently.

                parameterName = m.group(1).strip()
                parameterValue = m.group(2)

                if ']]' not in parameterValue:  # if the value does not contain a link, trim whitespace
                    parameterValue = parameterValue.strip()
                templateParams[parameterName] = parameterValue
            else:
                # this is an unnamed parameter
                unnamedParameterCounter += 1

                if ']]' not in param:  # if the value does not contain a link, trim whitespace
                    param = param.strip()
                templateParams[str(unnamedParameterCounter)] = param
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('   templateParams> %s', '|'.join(templateParams.values()))
        return templateParams

    @staticmethod
    @lru_cache(maxsize=10000)
    def _parse_template(template):
        return Template.parse(template)

    def expandTemplate(self, body):
        """Expands template invocation.
        :param body: the parts of a template.

        :see http://meta.wikimedia.org/wiki/Help:Expansion for an explanation
        of the process.

        See in particular: Expansion of names and values
        http://meta.wikimedia.org/wiki/Help:Expansion#Expansion_of_names_and_values

        For most parser functions all names and values are expanded,
        regardless of what is relevant for the result. The branching functions
        (#if, #ifeq, #iferror, #ifexist, #ifexpr, #switch) are exceptions.

        All names in a template call are expanded, and the titles of the
        tplargs in the template body, after which it is determined which
        values must be expanded, and for which tplargs in the template body
        the first part (default).

        In the case of a tplarg, any parts beyond the first are never
        expanded.  The possible name and the value of the first part is
        expanded if the title does not match a name in the template call.

        :see code for braceSubstitution at
        https://doc.wikimedia.org/mediawiki-core/master/php/html/Parser_8php_source.html#3397:

        """

        # template        = "{{" parts "}}"

        # Templates and tplargs are decomposed in the same way, with pipes as
        # separator, even though eventually any parts in a tplarg after the first
        # (the parameter default) are ignored, and an equals sign in the first
        # part is treated as plain text.
        # Pipes inside inner templates and tplargs, or inside double rectangular
        # brackets within the template or tplargs are not taken into account in
        # this decomposition.
        # The first part is called title, the other parts are simply called parts.

        # If a part has one or more equals signs in it, the first equals sign
        # determines the division into name = value. Equals signs inside inner
        # templates and tplargs, or inside double rectangular brackets within the
        # part are not taken into account in this decomposition. Parts without
        # equals sign are indexed 1, 2, .., given as attribute in the <name> tag.

        if len(self.frame) >= self.maxTemplateRecursionLevels:
            self.recursion_exceeded_2_errs += 1
            # logger.debug('   INVOCATION> %d %s', len(self.frame), body)
            return ''

        logger.debug('INVOCATION %d %s', len(self.frame), body)

        parts = splitParts(body)
        # title is the portion before the first |
        logger.debug('TITLE %s', parts[0].strip())
        title = self.expandTemplates(parts[0].strip())

        # SUBST
        # Apply the template tag to parameters without
        # substituting into them, e.g.
        # {{subst:t|a{{{p|q}}}b}} gives the wikitext start-a{{{p|q}}}b-end
        # @see https://www.mediawiki.org/wiki/Manual:Substitution#Partial_substitution
        subst = False
        if _SUBST_WORDS_RE.match(title):
            title = _SUBST_WORDS_RE.sub('', title, 1)
            subst = True

        if title.lower() in self.magicWords.values:
            return self.magicWords[title.lower()]

        # Parser functions
        # The first argument is everything after the first colon.
        # It has been evaluated above.
        colon = title.find(':')
        if colon > 1:
            funct = title[:colon]
            parts[0] = title[colon + 1:].strip()  # side-effect (parts[0] not used later)
            if funct not in lazyParserFunctions:
                # Value functions receive fully expanded arguments.
                # parts[0] arrives expanded already, as part of title;
                # the rest are expanded here, before the call.
                parts = [parts[0]] + [self.expandTemplates(p) for p in parts[1:]]
            ret = callParserFunction(funct, parts, self.frame,
                                      page_title=self.title, page_id=self.id, extractor=self)
            return self.expandTemplates(ret)

        title = fullyQualifiedTemplateTitle(title, self)
        if not title:
            self.template_title_errs += 1
            return ''

        redirected = self.redirects.get(title)
        if redirected:
            title = redirected

        # get the template
        if title in self.templates:
            template = Extractor._parse_template(self.templates[title])
        else:
            # The page being included could not be identified
            return ''

        # logger.debug('TEMPLATE %s: %s', title, template)

        # tplarg          = "{{{" parts "}}}"
        # parts           = [ title *( "|" part ) ]
        # part            = ( part-name "=" part-value ) / ( part-value )
        # part-name       = wikitext-L3
        # part-value      = wikitext-L3
        # wikitext-L3     = literal / template / tplarg / link / comment /
        #                   line-eating-comment / unclosed-comment /
        #           	    xmlish-element / *wikitext-L3

        # A tplarg may contain other parameters as well as templates, e.g.:
        #   {{{text|{{{quote|{{{1|{{error|Error: No text given}}}}}}}}}}}
        # hence no simple RE like this would work:
        #   '{{{((?:(?!{{{).)*?)}}}'
        # We must use full CF parsing.

        # the parameter name itself might be computed, e.g.:
        #   {{{appointe{{#if:{{{appointer14|}}}|r|d}}14|}}}

        # Because of the multiple uses of double-brace and triple-brace
        # syntax, expressions can sometimes be ambiguous.
        # Precedence rules specifed here:
        # http://www.mediawiki.org/wiki/Preprocessor_ABNF#Ideal_precedence
        # resolve ambiguities like this:
        #   {{{{ }}}} -> { {{{ }}} }
        #   {{{{{ }}}}} -> {{ {{{ }}} }}
        #
        # :see: https://en.wikipedia.org/wiki/Help:Template#Handling_parameters

        params = parts[1:]

        if not subst:
            # Evaluate parameters, since they may contain templates, including
            # the symbol "=".
            # {{#ifexpr: {{{1}}} = 1 }}
            params = [self.expandTemplates(p) for p in params]

        # build a dict of name-values for the parameter values
        params = self.templateParams(params)

        # Guard against template self-inclusion loops. We compare (title,
        # params) rather than title alone: a template legitimately calling
        # itself with *different* (e.g. progressively shrinking) parameters
        # is a common, intentional MediaWiki idiom for iterating over a
        # variable-length argument list (templates have no native loop
        # construct) -- it makes real progress each step and terminates on
        # its own, well within maxTemplateRecursionLevels. That must NOT be
        # blocked. What we actually want to catch is the same title being
        # invoked again with the *same* parameters as an active ancestor --
        # that's genuine zero-progress recursion (e.g. a template whose
        # /doc examples re-invoke it with fixed, hardcoded parameters),
        # which can branch combinatorially and never terminates in practice.
        # Real MediaWiki detects the analogous case ("Template loop
        # detected: Template:X") and stops immediately; do the same here.
        if any(frameTitle == title and frameParams == params
               for frameTitle, frameParams in self.frame):
            self.template_loop_errs += 1
            loopKey = (self.id, title)
            if loopKey not in self.warned_loop_keys:
                self.warned_loop_keys.add(loopKey)
                logger.debug("Template loop detected: %s (article %s, id %s) -- "
                              "leaving unexpanded (further repeats in this article "
                              "are counted, see the per-article WARNING-level "
                              "summary, but not logged individually)",
                              title, self.title, self.id)
            return '{{' + body + '}}'

        # Perform parameter substitution
        # extend frame before subst, since there may be recursion in default
        # parameter value, e.g. {{OTRS|celebrative|date=April 2015}} in article
        # 21637542 in enwiki.
        self.frame.append((title, params))
        try:
            instantiated = template.subst(params, self)
            # logger.debug('instantiated %d %s', len(self.frame), instantiated)
            value = self.expandTemplates(instantiated)
        finally:
            self.frame.pop()
        # logger.debug('   INVOCATION> %s %d %s', title, len(self.frame), value)
        return value


# ----------------------------------------------------------------------
# parameter handling


def splitParts(paramsList):
    """
    :param paramsList: the parts of a template or tplarg.

    Split template parameters at the separator "|".
    separator "=".

    Template parameters often contain URLs, internal links, text or even
    template expressions, since we evaluate templates outside in.
    This is required for cases like:
      {{#if: {{{1}}} | {{lc:{{{1}}} | "parameter missing"}}
    Parameters are separated by "|" symbols. However, we
    cannot simply split the string on "|" symbols, since these
    also appear inside templates and internal links, e.g.

     {{if:|
      |{{#if:the president|
           |{{#if:|
               [[Category:Hatnote templates|A{{PAGENAME}}]]
            }}
       }}
     }}

    We split parts at the "|" symbols that are not inside any pair
    {{{...}}}, {{...}}, [[...]], {|...|}.
    """

    # Must consider '[' as normal in expansion of Template:EMedicine2:
    # #ifeq: ped|article|[http://emedicine.medscape.com/article/180-overview|[http://www.emedicine.com/ped/topic180.htm#{{#if: |section~}}
    # as part of:
    # {{#ifeq: ped|article|[http://emedicine.medscape.com/article/180-overview|[http://www.emedicine.com/ped/topic180.htm#{{#if: |section~}}}} ped/180{{#if: |~}}]

    # should handle both tpl arg like:
    #    4|{{{{{subst|}}}CURRENTYEAR}}
    # and tpl parameters like:
    #    ||[[Category:People|{{#if:A|A|{{PAGENAME}}}}]]

    sep = '|'
    parameters = []
    cur = 0
    for s, e in findMatchingBraces(paramsList):
        par = paramsList[cur:s].split(sep)
        if par:
            if parameters:
                # portion before | belongs to previous parameter
                parameters[-1] += par[0]
                if len(par) > 1:
                    # rest are new parameters
                    parameters.extend(par[1:])
            else:
                parameters = par
        elif not parameters:
            parameters = ['']  # create first param
        # add span to last previous parameter
        parameters[-1] += paramsList[s:e]
        cur = e
    # leftover
    par = paramsList[cur:].split(sep)
    if par:
        if parameters:
            # portion before | belongs to previous parameter
            parameters[-1] += par[0]
            if len(par) > 1:
                # rest are new parameters
                parameters.extend(par[1:])
        else:
            parameters = par

    # logger.debug('splitParts %s %s\nparams: %s', sep, paramsList, str(parameters))
    return parameters


# findMatchingBraces() is called extremely frequently -- recursively,
# once per expandTemplates()/subst()/splitParts() invocation, at every
# level of template nesting -- but only ever with ldelim in {0, 2, 3}:
# confirmed directly, every call site in this file (and every direct
# call in this project's own tests) uses one of these three literal
# values. Pre-compiling the (reOpen, reNext) pair for each here, once,
# at import time, avoids re-running re.compile() (itself non-trivial:
# '%'-formatting a pattern string, plus a cache lookup, on every single
# call even when the underlying compiled pattern ends up the same) --
# confirmed via profiling a real extraction run: re.compile() alone
# accounted for a measurable, entirely avoidable share of total time,
# called as many times as findMatchingBraces() itself was.
def _build_brace_patterns(ldelim):
    if ldelim:  # 2-3
        return (re.compile(r'[{]{%d,}' % ldelim),  # at least ldelim
                re.compile(r'[{]{2,}|}{2,}'))  # at least 2 open or close braces
    return (re.compile(r'{{2,}|\[{2,}'),
            re.compile(r'{{2,}|}{2,}|\[{2,}|]{2,}'))  # at least 2


_BRACE_PATTERNS = {ldelim: _build_brace_patterns(ldelim) for ldelim in (0, 2, 3)}


def findMatchingBraces(text, ldelim=0):
    """
    :param ldelim: number of braces to match. 0 means match [[]], {{}} and {{{}}}.
    """
    # Parsing is done with respect to pairs of double braces {{..}} delimiting
    # a template, and pairs of triple braces {{{..}}} delimiting a tplarg.
    # If double opening braces are followed by triple closing braces or
    # conversely, this is taken as delimiting a template, with one left-over
    # brace outside it, taken as plain text. For any pattern of braces this
    # defines a set of templates and tplargs such that any two are either
    # separate or nested (not overlapping).

    # Unmatched double rectangular closing brackets can be in a template or
    # tplarg, but unmatched double rectangular opening brackets cannot.
    # Unmatched double or triple closing braces inside a pair of
    # double rectangular brackets are treated as plain text.
    # Other formulation: in ambiguity between template or tplarg on one hand,
    # and a link on the other hand, the structure with the rightmost opening
    # takes precedence, even if this is the opening of a link without any
    # closing, so not producing an actual link.

    # In the case of more than three opening braces the last three are assumed
    # to belong to a tplarg, unless there is no matching triple of closing
    # braces, in which case the last two opening braces are are assumed to
    # belong to a template.

    # We must skip individual { like in:
    #   {{#ifeq: {{padleft:|1|}} | { | | &nbsp;}}
    # We must resolve ambiguities like this:
    #   {{{{ }}}} -> { {{{ }}} }
    #   {{{{{ }}}}} -> {{ {{{ }}} }}
    #   {{#if:{{{{{#if:{{{nominee|}}}|nominee|candidate}}|}}}|...}}

    # Handle:
    #   {{{{{|safesubst:}}}#Invoke:String|replace|{{{1|{{{{{|safesubst:}}}PAGENAME}}}}}|%s+%([^%(]-%)$||plain=false}}
    # as well as expressions with stray }:
    #   {{{link|{{ucfirst:{{{1}}}}}} interchange}}}

    # Falls back to building fresh (uncached) if ever called with a
    # value outside {0, 2, 3} -- slower, but still correct; every real
    # call site and every direct test call already stays within the
    # cached set, so this path is not expected to actually run.
    reOpen, reNext = _BRACE_PATTERNS.get(ldelim) or _build_brace_patterns(ldelim)

    cur = 0
    while True:
        m1 = reOpen.search(text, cur)
        if not m1:
            return
        lmatch = m1.end() - m1.start()
        m1start = m1.start()
        if m1.group()[0] == '{':
            if lmatch > 3:
                # More than 3 consecutive opening braces: per the
                # documented rule above, only the rightmost 3 belong to
                # this (innermost) match -- the excess, leftmost braces
                # belong to some outer level, and are deliberately left
                # unconsumed here (as plain text, from this function's
                # point of view) rather than folded into this match.
                m1start += lmatch - 3
                lmatch = 3
            stack = [lmatch]  # stack of opening braces lengths
        else:
            stack = [-lmatch]  # negative means [
        end = m1.end()
        while True:
            m2 = reNext.search(text, end)
            if not m2:
                return  # unbalanced
            end = m2.end()
            brac = m2.group()[0]
            lmatch = m2.end() - m2.start()

            if brac == '{':
                stack.append(lmatch)
            elif brac == '}':
                while stack:
                    openCount = stack.pop()  # opening span
                    if openCount == 0:  # illegal unmatched [[
                        continue
                    if lmatch >= openCount:
                        lmatch -= openCount
                        if lmatch <= 1:  # either close or stray }
                            break
                    else:
                        # put back unmatched
                        stack.append(openCount - lmatch)
                        break
                if not stack:
                    yield m1start, end - lmatch
                    cur = end
                    break
                elif len(stack) == 1 and 0 < stack[0] < ldelim:
                    # ambiguous {{{{{ }}} }}
                    yield m1start + stack[0], end
                    cur = end
                    break
            elif brac == '[':  # [[
                stack.append(-lmatch)
            else:  # ]]
                while stack and stack[-1] < 0:  # matching [[
                    openCount = -stack.pop()
                    if lmatch >= openCount:
                        lmatch -= openCount
                        if lmatch <= 1:  # either close or stray ]
                            break
                    else:
                        # put back unmatched (negative)
                        stack.append(lmatch - openCount)
                        break
                if not stack:
                    yield m1.start(), end - lmatch
                    cur = end
                    break
                # unmatched ]] are discarded
                cur = end


def findBalanced(text, openDelim, closeDelim, search_start=0):
    """
    Assuming that text contains a properly balanced expression using
    :param openDelim: as opening delimiters and
    :param closeDelim: as closing delimiters.
    :param search_start: character position to begin searching from
      (default 0, i.e. the whole text). Useful for scanning from
      partway through a large string without copying/slicing it first --
      regex .search(text, pos) already supports an arbitrary start
      position natively, without any copy.
    :return: an iterator producing pairs (start, end) of start and end
    positions in text containing a balanced expression.
    """
    openPat = '|'.join([re.escape(x) for x in openDelim])
    # patter for delimiters expected after each opening delimiter
    afterPat = {o: re.compile(openPat + '|' + c, re.DOTALL) for o, c in zip(openDelim, closeDelim)}
    stack = []
    start = 0
    cur = search_start
    # end = len(text)
    startSet = False
    startPat = re.compile(openPat)
    nextPat = startPat
    while True:
        next = nextPat.search(text, cur)
        if not next:
            return
        if not startSet:
            start = next.start()
            startSet = True
        delim = next.group(0)
        if delim in openDelim:
            stack.append(delim)
            nextPat = afterPat[delim]
        else:
            opening = stack.pop()
            # assert opening == openDelim[closeDelim.index(next.group(0))]
            if stack:
                nextPat = afterPat[stack[-1]]
            else:
                yield start, next.end()
                nextPat = startPat
                start = next.end()
                startSet = False
        cur = next.end()

# ----------------------------------------------------------------------
# parser functions utilities


def ucfirst(string):
    """:return: a string with just its first character uppercase
    We can't use title() since it coverts all words.
    """
    if string:
        if len(string) > 1:
            return string[0].upper() + string[1:]
        else:
            return string.upper()
    else:
        return ''


def lcfirst(string):
    """:return: a string with its first character lowercase"""
    if string:
        if len(string) > 1:
            return string[0].lower() + string[1:]
        else:
            return string.lower()
    else:
        return ''


# Pre-compiled once here rather than passed as a raw string to
# module-level re.match() on every fullyQualifiedTemplateTitle() call
# -- once per template invocation whose title contains a colon after
# its first character, same reasoning as _SUBST_WORDS_RE/
# _TEMPLATE_PARAM_RE above.
_TEMPLATE_TITLE_COLON_RE = re.compile(r'([^:]*)(:.*)')


def fullyQualifiedTemplateTitle(templateTitle, extractor):
    """
    Determine the namespace of the page being included through the template
    mechanism
    """
    if templateTitle.startswith(':'):
        # Leading colon by itself implies main namespace, so strip this colon
        return ucfirst(templateTitle[1:])
    else:
        m = _TEMPLATE_TITLE_COLON_RE.match(templateTitle)
        if m:
            # colon found but not in the first position - check if it
            # designates a known namespace
            prefix = normalizeNamespace(m.group(1))
            if prefix in extractor.knownNamespaces:
                return prefix + ucfirst(m.group(2))
    # The title of the page being included is NOT in the main namespace and
    # lacks any other explicit designation of the namespace - therefore, it
    # is resolved to the Template namespace (that's the default for the
    # template inclusion mechanism).

    # This is a defense against pages whose title only contains UTF-8 chars
    # that are reduced to an empty string. Right now I can think of one such
    # case - <C2><A0> which represents the non-breaking space.
    # In this particular case, this page is a redirect to [[Non-nreaking
    # space]], but having in the system a redirect page with an empty title
    # causes numerous problems, so we'll live happier without it.
    if templateTitle:
        return extractor.templatePrefix + ucfirst(templateTitle)
    else:
        return ''  # caller may log as error


def normalizeNamespace(ns):
    return ucfirst(ns)


# ----------------------------------------------------------------------
# Parser functions
# see http://www.mediawiki.org/wiki/Help:Extension:ParserFunctions
# https://github.com/Wikia/app/blob/dev/extensions/ParserFunctions/ParserFunctions_body.php


# #expr's operators, mapped to their Python equivalents -- this is the
# complete, explicit whitelist; nothing outside it is ever evaluated.
_SHARP_EXPR_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_SHARP_EXPR_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: lambda x: 0 if x else 1,
}
_SHARP_EXPR_COMPARISONS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# The exact fallback sharp_expr() returns on any failure -- named here
# (rather than repeating the literal) both to avoid duplicating it and
# because sharp_expr() also checks incoming expr text for this exact
# string, to detect a failed #expr's output being fed as literal input
# into an enclosing #expr call (see sharp_expr()'s own except block).
_SHARP_EXPR_ERROR_SPAN = '<span class="error"></span>'


def _sharp_expr_eval_node(node):
    """
    Recursively evaluates one node of a parsed #expr expression,
    computing the result directly in Python rather than ever calling
    eval()/exec()/compile() on the (untrusted, wikitext-derived)
    expression text. Every node type reachable here is on an explicit
    whitelist; anything else -- a function call, a name lookup, an
    attribute access, a string, anything at all outside plain
    numeric/boolean arithmetic -- raises ValueError and is treated as
    a malformed expression, the same outcome #expr's real syntax
    would produce for it anyway (it doesn't support any of those
    either: see https://www.mediawiki.org/wiki/Help:Extension:ParserFunctions,
    #expr operates on numbers and booleans only, never strings, and
    has no facility for function calls or name references at all).

    This must never be changed to route the expression text through
    eval()/exec()/compile() in any form: #expr's input comes from
    wikitext on openly-editable wikis, and any such path grants full
    access to Python's builtins (including __import__) unless
    explicit globals/locals are passed and carefully restricted --
    easy to get wrong, so the whitelist-only approach here is
    deliberate, not incidental.

    Previously, an eval() call had full access to
    Python's builtins, including __import__ -- e.g.
    "{{#expr: __import__('os').system('rm -rf ...') }}"
    would actually execute a shell command.
    """
    if isinstance(node, ast.Expression):
        return _sharp_expr_eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("only numeric constants are permitted in #expr")

    if isinstance(node, ast.BinOp):
        # "X round Y" gets pre-processed (see sharp_expr() below) into
        # "X |ROUND| Y", which -- since | left-associates in Python --
        # parses as (X | ROUND) | Y. Recognized specifically as this
        # exact shape, rather than treating BitOr as a general-purpose
        # operator (it isn't one in #expr's own grammar at all).
        if (isinstance(node.op, ast.BitOr)
                and isinstance(node.left, ast.BinOp)
                and isinstance(node.left.op, ast.BitOr)
                and isinstance(node.left.right, ast.Name)
                and node.left.right.id == 'ROUND'):
            value = _sharp_expr_eval_node(node.left.left)
            digits = _sharp_expr_eval_node(node.right)
            return round(value, int(digits))
        op_func = _SHARP_EXPR_BINOPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"operator not permitted in #expr: {type(node.op).__name__}")
        return op_func(_sharp_expr_eval_node(node.left), _sharp_expr_eval_node(node.right))

    if isinstance(node, ast.UnaryOp):
        op_func = _SHARP_EXPR_UNARYOPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"unary operator not permitted in #expr: {type(node.op).__name__}")
        return op_func(_sharp_expr_eval_node(node.operand))

    if isinstance(node, ast.Call):
        # "trunc EXPR" gets pre-processed (see sharp_expr() below) into
        # "TRUNC(EXPR)". Recognized specifically as this exact shape --
        # func is a bare Name 'TRUNC', exactly one positional argument,
        # no keywords, no starargs -- not general-purpose function-call
        # support, matching the narrow, specific-shape-only approach
        # used for ROUND above. Any other call shape (a different name,
        # wrong argument count, keyword arguments) falls through to the
        # same ValueError every other disallowed node type gets.
        if (isinstance(node.func, ast.Name) and node.func.id == 'TRUNC'
                and len(node.args) == 1 and not node.keywords):
            return int(_sharp_expr_eval_node(node.args[0]))
        raise ValueError("function calls are not permitted in #expr")

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError("chained comparisons not supported in #expr")
        op_func = _SHARP_EXPR_COMPARISONS.get(type(node.ops[0]))
        if op_func is None:
            raise ValueError(f"comparison not permitted in #expr: {type(node.ops[0]).__name__}")
        result = op_func(_sharp_expr_eval_node(node.left),
                         _sharp_expr_eval_node(node.comparators[0]))
        return 1 if result else 0

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = 1
            for value_node in node.values:
                result = _sharp_expr_eval_node(value_node)
                if not result:
                    return 0
            return result
        else:  # ast.Or
            for value_node in node.values:
                result = _sharp_expr_eval_node(value_node)
                if result:
                    return result
            return 0

    raise ValueError(f"disallowed #expr element: {type(node).__name__}")


# Converts real MediaWiki #expr's own "=" (equality) to Python's "==",
# but ONLY a standalone "=" -- not one that's already part of a real,
# multi-character comparison operator (<=, >=, ==, !=). A naive
# re.sub('=', '==', expr) doubles every "=" indiscriminately, which
# mangles all four of those into invalid Python syntax: "<=" becomes
# "<==", ">=" becomes ">==", "==" becomes "====", "!=" becomes "!==" --
# confirmed directly, all four failed to parse at all before this fix,
# while the two single-character comparisons (<, >) worked fine, which
# is what first made the "=" substitution the suspect. Real-world
# impact confirmed to be substantial, not theoretical: a single real
# article (pulling in citation/CS1 and Wikidata module machinery, both
# of which lean on #expr comparisons heavily for their own internal
# logic) hit this thousands of times in one page.
# (?<![<>=!]) -- the "=" isn't preceded by one of those (i.e. it's not
#   the second character of an existing two-character operator).
# (?!=) -- the "=" isn't followed by another "=" (i.e. it's not the
#   first character of an existing "==").
_EQUALS_TO_DOUBLE_EQUALS_RE = re.compile(r'(?<![<>=!])=(?!=)')

# #expr spells inequality both "<>" and "!="; Python only has the
# latter, having dropped "<>" in Python 3, so ast.parse() rejects the
# whole expression. Converted before the "=" substitution above, whose
# lookbehind already excludes the "!" this produces. jawiki's
# Template:Is-leap-year is one caller -- "{{{1}}} mod 100 <> 0" -- and
# through it every year article from 1 to 1582 lost the 平年/閏年 link
# its opening sentence is built around.
_NOT_EQUALS_RE = re.compile(r'<>')


def sharp_expr(expr, page_title=None, page_id=None, extractor=None):
    try:
        orig_expr = expr
        expr = _NOT_EQUALS_RE.sub('!=', expr)
        expr = _EQUALS_TO_DOUBLE_EQUALS_RE.sub('==', expr)
        expr = re.sub(r'\bmod\b', '%', expr)
        expr = re.sub(r'\bdiv\b', '/', expr)
        expr = re.sub(r'\bround\b', '|ROUND|', expr)
        # "trunc EXPR" is #expr's own prefix, unary truncate-toward-
        # zero operator -- real-world usage confirmed to always
        # already parenthesize its operand ("trunc (150 * 800 / 532)"),
        # which this relies on: only converts "trunc" to "TRUNC" when
        # immediately followed by "(", so the result is always valid
        # Python function-call syntax (the existing parens become the
        # call's own parens) rather than guessing where an
        # unparenthesized operand would end. _sharp_expr_eval_node()
        # below recognizes only this exact "TRUNC(...)" shape -- one
        # positional argument, no keywords, nothing else -- the same
        # narrow, specific-shape-only approach as ROUND above, not
        # general-purpose function-call support.
        expr = re.sub(r'\btrunc\b(?=\s*\()', 'TRUNC', expr)
        # Malformed #expr input -- number directly adjacent to a
        # Python keyword, e.g. "3 in 5" -> "3in5" -- makes ast.parse()
        # emit SyntaxWarning: invalid decimal literal as a side effect
        # of tokenizing, even though the parse still correctly fails
        # right after and gets caught below. Wikitext isn't Python
        # source and was never going to satisfy Python's tokenizer
        # rules; this warning has nothing to tell a reader here --
        # the article-identifying warning logged in the except branch
        # below is what's actually useful for finding and fixing the
        # real, on-wiki #expr call this came from.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(expr, mode='eval')
        return str(_sharp_expr_eval_node(tree))
    except Exception:
        # The same malformed #expr call is frequently invoked many
        # times over within a single article (e.g. once per row of a
        # table built from a broken shared template) -- one genuine
        # occurrence can otherwise produce hundreds of near-identical
        # log lines. Same dedup shape as expandTemplate()'s own loop
        # detection: count every occurrence (reflected in the single
        # per-article WARNING-level summary logged at the end of
        # extract(), which is what's actually useful for deciding
        # which pages are worth targeting), but only log the first
        # one per (article, expression) pair, and only at DEBUG --
        # detail for digging into a specific already-identified page,
        # not something that should show up at default verbosity.
        #
        # A second, distinct source of noise this same dedup can't
        # catch: nested #expr calls, e.g. {{#expr:{{#expr:X-1}}+1}},
        # where an inner failure's own _SHARP_EXPR_ERROR_SPAN output
        # gets substituted as literal text into the enclosing #expr's
        # input, which then also fails (a span tag isn't numeric).
        # Distinct cascades within the same article are each reported
        # only once.
        if _SHARP_EXPR_ERROR_SPAN in orig_expr:
            if extractor is not None:
                extractor.malformed_expr_errs += 1
                cascadeKey = (page_id, 'cascade')
                if cascadeKey in extractor.warned_expr_keys:
                    return _SHARP_EXPR_ERROR_SPAN
                extractor.warned_expr_keys.add(cascadeKey)
            if page_title is not None:
                logger.debug("Malformed #expr: %r (article %s, id %s) -- this looks like "
                              "a chain of nested #expr calls; further such chained failures "
                              "in this article are counted (see the per-article WARNING-level "
                              "summary) but not logged individually",
                              orig_expr, page_title, page_id)
            else:
                logger.debug("Malformed #expr: %r", orig_expr)
            return _SHARP_EXPR_ERROR_SPAN
        if extractor is not None:
            extractor.malformed_expr_errs += 1
            exprKey = (page_id, orig_expr)
            if exprKey in extractor.warned_expr_keys:
                return _SHARP_EXPR_ERROR_SPAN
            extractor.warned_expr_keys.add(exprKey)
        if page_title is not None:
            logger.debug("Malformed #expr: %r (article %s, id %s) -- further identical "
                          "occurrences in this article are counted (see the per-article "
                          "WARNING-level summary) but not logged individually",
                          orig_expr, page_title, page_id)
        else:
            logger.debug("Malformed #expr: %r", orig_expr)
        return _SHARP_EXPR_ERROR_SPAN


def sharp_ifexpr(test, valueIfTrue='', valueIfFalse='', *args, page_title=None, page_id=None, extractor=None):
    """
    {{#ifexpr: EXPRESSION | VALUE_IF_TRUE | VALUE_IF_FALSE}}

    Evaluates EXPRESSION via sharp_expr() itself -- not a separate
    evaluator -- so this gets sharp_expr()'s own safe AST-only
    evaluation, malformed-expression logging, and per-article dedup
    (including cascade suppression) for free, rather than duplicating
    any of it. Real MediaWiki semantics: a nonzero result selects
    valueIfTrue, zero selects valueIfFalse; a malformed expression
    (unlike #if/#ifeq, which never fail this way, since their own
    condition is never itself evaluated as an expression) reports the
    same as a bare, failing #expr would and returns that same error
    indicator rather than silently guessing a branch.
    """
    result = sharp_expr(test, page_title=page_title, page_id=page_id, extractor=extractor)
    if result == _SHARP_EXPR_ERROR_SPAN:
        return result
    try:
        numeric_result = float(result)
    except ValueError:
        # Shouldn't happen -- sharp_expr() only ever returns the error
        # span above or a str() of the int/float it computed -- but
        # fail safe rather than raise, consistent with every other
        # parser function here.
        return _SHARP_EXPR_ERROR_SPAN
    if numeric_result != 0:
        return valueIfTrue.strip()
    else:
        return valueIfFalse.strip()


def sharp_if(testValue, valueIfTrue, valueIfFalse=None, *args):
    # In theory, we should evaluate the first argument here,
    # but it was evaluated while evaluating part[0] in expandTemplate().
    if testValue.strip():
        # The {{#if:}} function is an if-then-else construct.
        # The applied condition is: "The condition string is non-empty".
        valueIfTrue = valueIfTrue.strip()
        if valueIfTrue:
            return valueIfTrue
    elif valueIfFalse:
        return valueIfFalse.strip()
    return ""


def _expandOperand(operand, expand):
    """Expand a comparison operand of #ifeq or #switch.

    :param expand: the Extractor's own expandTemplates, or None when
        the function is called directly rather than through
        callParserFunction() (unit tests, mainly) -- in which case the
        operand is used as given.

    The '{' test keeps operands that cannot contain a template or a
    tplarg -- nearly all of them, since case labels are usually plain
    words -- off the expansion path entirely.
    """
    if expand is not None and '{' in operand:
        return expand(operand)
    return operand


def sharp_ifeq(lvalue, rvalue, valueIfTrue, valueIfFalse=None, *args, expand=None):
    # Both operands take part in the comparison, so both are expanded.
    # lvalue arrives expanded, as parts[0] of the parser function call;
    # rvalue is expanded here. The two branches are not touched: at
    # most one is returned, and expandTemplate() expands whatever comes
    # back.
    rvalue = _expandOperand(rvalue, expand).strip()
    if rvalue:
        # lvalue is always defined
        if lvalue.strip() == rvalue:
            # The {{#ifeq:}} function is an if-then-else construct. The
            # applied condition is "is rvalue equal to lvalue". Note that this
            # does only string comparison while MediaWiki implementation also
            # supports numerical comparissons.

            if valueIfTrue:
                return valueIfTrue.strip()
        else:
            if valueIfFalse:
                return valueIfFalse.strip()
    return ""


def sharp_iferror(test, then='', Else=None, *args):
    if re.match(r'<(?:strong|span|p|div)\s(?:[^\s>]*\s+)*?class="(?:[^"\s>]*\s+)*?error(?:\s[^">]*)?"', test):
        return then
    elif Else is None:
        return test.strip()
    else:
        return Else.strip()


def sharp_switch(primary, *params, expand=None):
    # FIXME: we don't support numeric expressions in primary

    # {{#switch: comparison string
    #  | case1 = result1
    #  | case2
    #  | case4 = result2
    #  | 1 | case5 = result3
    #  | #default = result4
    # }}

    primary = primary.strip()
    found = False  # for fall through cases
    default = None
    rvalue = None
    lvalue = ''
    for param in params:
        # handle cases like:
        #  #default = [http://www.perseus.tufts.edu/hopper/text?doc=Perseus...]
        pair = param.split('=', 1)
        # The case label is a comparison operand, so it is expanded;
        # the result after '=' is not, since #switch returns at most
        # one result and expandTemplate() expands whatever comes back.
        # Labels are expanded one at a time as the scan reaches them,
        # so a match stops the scan and leaves the remaining labels
        # unexpanded.
        lvalue = _expandOperand(pair[0], expand).strip()
        rvalue = None
        if len(pair) > 1:
            # got "="
            rvalue = pair[1].strip()
            # check for any of multiple values pipe separated -- most
            # #switch cases are a single value with no "|" at all, so
            # skip building a list (split + strip on every element)
            # just to check membership in that common case; confirmed
            # via profiling a real extraction run and a direct,
            # isolated timing comparison that this matters (~2.5x
            # faster for the no-"|" case, and #switch calls with many
            # cases -- common in real, complex templates -- multiply
            # that per-case saving many times over in a single call).
            if found or (primary == lvalue if '|' not in lvalue
                         else primary in [v.strip() for v in lvalue.split('|')]):
                # Found a match, return now
                return rvalue
            elif lvalue == '#default':
                default = rvalue
            rvalue = None  # avoid defaulting to last case
        elif lvalue == primary:
            # If the value matches, set a flag and continue
            found = True
    # Default case
    # Check if the last item had no = sign, thus specifying the default case
    if rvalue is not None:
        return lvalue
    elif default is not None:
        return default
    return ''



# ----------------------------------------------------------------------
# {{#time: FORMAT | TIMESTAMP }}

# Format characters sharp_time() renders, all of them numeric. The
# ones left out -- month and day names (F, M, D, l), the composite
# r/c formats, the xg/xn-prefixed non-Gregorian calendars -- need the
# source wiki's own language data, which nothing here tracks: jawiki's
# "F" is 3月 where enwiki's is March. A format string containing any
# alphabetic character outside this table is declined (see
# sharp_time()), so a wrong month name is never produced.
_TIME_FORMATTERS = {
    'Y': lambda t: '%04d' % t.year,
    'y': lambda t: '%02d' % (t.year % 100),
    'L': lambda t: '1' if calendar.isleap(t.year) else '0',
    'n': lambda t: str(t.month),
    'm': lambda t: '%02d' % t.month,
    't': lambda t: str(calendar.monthrange(t.year, t.month)[1]),
    'j': lambda t: str(t.day),
    'd': lambda t: '%02d' % t.day,
    'z': lambda t: str(t.timetuple().tm_yday - 1),
    'N': lambda t: str(t.isoweekday()),
    'w': lambda t: str(t.isoweekday() % 7),
    'W': lambda t: '%02d' % t.isocalendar()[1],
    'G': lambda t: str(t.hour),
    'H': lambda t: '%02d' % t.hour,
    'g': lambda t: str((t.hour % 12) or 12),
    'h': lambda t: '%02d' % ((t.hour % 12) or 12),
    'i': lambda t: '%02d' % t.minute,
    's': lambda t: '%02d' % t.second,
    'U': lambda t: str(int(t.timestamp())),
}

# Timestamp forms sharp_time() accepts. Real MediaWiki hands the
# timestamp to PHP's strtotime, which takes a far wider and looser
# range of English date expressions than these; the forms here are the
# ones citation and date-validation templates actually pass. Anything
# else is reported as an invalid time.
_TIME_ISO_RE = re.compile(r"""
    ^\s*
    (?P<year>\d{1,4})
    (?:-(?P<month>\d{1,2})
       (?:-(?P<day>\d{1,2}))?
    )?
    (?:[ T]
       (?P<hour>\d{1,2}):(?P<minute>\d{2})
       (?::(?P<second>\d{2}))?
    )?
    Z?
    \s*$
""", re.VERBOSE)

# The 14-digit form MediaWiki stores revision timestamps in.
_TIME_MW_RE = re.compile(r'^\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*$')

# A single offset from the current time, e.g. "+24hours" -- what
# citation templates use to decide whether an access date is in the
# future. Only units timedelta represents exactly are here: a "+1
# month" offset has no fixed length, and guessing one would put a
# silently wrong date into a comparison.
_TIME_RELATIVE_RE = re.compile(
    r'^\s*([+-]?\d+)\s*(second|minute|hour|day|week)s?\s*$', re.IGNORECASE)

_TIME_RELATIVE_UNITS = {
    'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400, 'week': 604800,
}


def _parseTimestamp(timestamp):
    """Return a timezone-aware datetime for a #time timestamp, or None
    when it is not one of the accepted forms.

    An empty timestamp means now, as it does in MediaWiki. That and
    the relative form make the result depend on when extraction runs,
    which is also true of the real parser function; a run that needs
    to be reproducible byte for byte should keep that in mind.
    """
    timestamp = timestamp.strip()
    if not timestamp:
        return datetime.datetime.now(datetime.timezone.utc)

    relative = _TIME_RELATIVE_RE.match(timestamp)
    if relative:
        amount = int(relative.group(1))
        seconds = amount * _TIME_RELATIVE_UNITS[relative.group(2).lower()]
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=seconds))

    mediawiki = _TIME_MW_RE.match(timestamp)
    if mediawiki:
        parts = [int(p) for p in mediawiki.groups()]
        try:
            return datetime.datetime(*parts, tzinfo=datetime.timezone.utc)
        except ValueError:
            return None

    iso = _TIME_ISO_RE.match(timestamp)
    if not iso:
        return None
    fields = iso.groupdict()
    try:
        return datetime.datetime(
            int(fields['year']),
            int(fields['month'] or 1),
            int(fields['day'] or 1),
            int(fields['hour'] or 0),
            int(fields['minute'] or 0),
            int(fields['second'] or 0),
            tzinfo=datetime.timezone.utc,
        )
    except ValueError:
        # Well-formed but not a real date -- 2024-02-31, month 13.
        return None


def sharp_time(format_string, timestamp='', *args):
    """{{#time: FORMAT | TIMESTAMP }} -- render a date.

    Two results other than a formatted date are possible, and they
    mean different things. An unparseable timestamp gives the error
    span, which is what {{#iferror:}} tests for and what date-checking
    templates rely on to reject a malformed date. A format string
    asking for something outside _TIME_FORMATTERS gives the empty
    string, leaving the caller in the same position as before this
    function rendered anything at all rather than claiming the date
    itself was bad.

    The language and local-time arguments MediaWiki takes after the
    timestamp are ignored: output here is Gregorian and UTC.
    """
    parsed = _parseTimestamp(timestamp)
    if parsed is None:
        return _SHARP_EXPR_ERROR_SPAN

    out = []
    index = 0
    while index < len(format_string):
        char = format_string[index]
        if char == '\\':
            # Escapes the next character, which is then a literal.
            if index + 1 < len(format_string):
                out.append(format_string[index + 1])
                index += 2
                continue
            index += 1
            continue
        if char == '"':
            closing = format_string.find('"', index + 1)
            if closing == -1:
                # No closing quote: the quote is itself a literal.
                out.append(char)
                index += 1
                continue
            out.append(format_string[index + 1:closing])
            index = closing + 1
            continue
        if char in _TIME_FORMATTERS:
            out.append(_TIME_FORMATTERS[char](parsed))
            index += 1
            continue
        if char.isalpha():
            return ''
        out.append(char)
        index += 1
    return ''.join(out)


# Digit sets used by some wikis for "national digit" display, mapped
# back to plain ASCII for formatnum's reverse (|R) mode. Each mapping
# is a fixed, one-to-one character substitution with no locale
# ambiguity -- unlike thousands-separator conventions, which
# genuinely differ by locale in ways that can't be safely guessed
# from the text alone (e.g. "1.234" means 1234 in some locales, 1.234
# in others). Covers Arabic-Indic and Extended Arabic-Indic (used by
# Persian, Urdu, and other Arabic-script wikis) since those are the
# ones actually observed in practice; add more sets here if another
# wiki's real output needs them.
_FORMATNUM_LOCAL_DIGITS = str.maketrans(
    '٠١٢٣٤٥٦٧٨٩'   # Arabic-Indic
    '۰۱۲۳۴۵۶۷۸۹',  # Extended Arabic-Indic (Persian/Urdu)
    '01234567890123456789'
)


def sharp_formatnum(num, flag='', *args):
    """
    {{formatnum: NUM }} / {{formatnum: NUM | R }} -- note no '#'
    prefix, unlike most parser functions here; matches real
    MediaWiki's own syntax for this one.

    Real MediaWiki's formatnum is locale-dependent: which digit-
    grouping convention to use, and whether to substitute "national"
    digits (e.g. Urdu ۰-۹), both come from the source wiki's own
    language configuration -- which nothing in this codebase tracks
    (there's no concept of "which wiki this dump is from" anywhere).
    This is necessarily an approximation, not an exact match for
    every wiki's own output.

    Reverse mode (flag == 'R') is the direction actually exercised by
    real, on-wiki templates chaining into #expr arithmetic (e.g.
    {{formatnum:{{{1}}}|R}} to normalize an argument before doing
    math on it -- confirmed directly: this is exactly what left every
    #ifexpr comparison in a real, on-wiki Format price template
    blank before this existed, since the value being compared was
    always empty). This direction is safe to implement precisely:
    strip comma grouping and map known local digit sets back to
    ASCII, both fixed, context-free substitutions with no locale
    guessing involved.

    Forward mode (the default, no |R) inserts comma thousands
    separators only -- the English-Wikipedia convention, and the most
    common on Wikipedia overall -- and does NOT attempt national-
    digit output, since that would require knowing the source wiki's
    own language settings. Non-numeric input is returned unchanged in
    forward mode, matching real MediaWiki's own graceful degradation
    rather than raising.
    """
    if flag.strip() == 'R':
        return num.replace(',', '').translate(_FORMATNUM_LOCAL_DIGITS).strip()
    try:
        value = float(num)
    except ValueError:
        return num
    if value == int(value):
        return f'{int(value):,}'
    return f'{value:,}'


def _sharp_pad(string, width, padding='0', from_left=True):
    """
    Shared core for padleft/padright below -- the two real MediaWiki
    functions differ only in which side gets padded. Matches PHP's
    own str_pad() semantics, which real MediaWiki's implementation is
    built on: the padding string is repeated as many times as needed
    to cover the gap, then truncated to exactly that many characters
    (not repeated a whole number of times and left over-long) before
    being attached to the original string.

    Real, worked example: padleft("1", 4, "xy") needs 3 characters of
    padding (4 - len("1")). "xy" repeated is "xyxyxy...";  truncated
    to 3 characters gives "xyx"; prepended to "1" gives "xyx1" -- not
    "xyxy1" (which would be one whole extra repetition) and not "xy1"
    (which would be short by one character).

    :param width: target total length. Invalid or non-positive values
        (can't sensibly pad to zero or a negative width) leave the
        string unchanged rather than raising or truncating it --
        matches real MediaWiki's own graceful degradation elsewhere
        in this file (e.g. formatnum's forward mode on non-numeric
        input) rather than an error a template author would never see
        in their own rendered page.
    :param padding: the string to repeat. An explicitly empty padding
        string has nothing to repeat, so this also leaves the
        original string unchanged rather than looping forever or
        dividing by zero.
    """
    try:
        width = int(width)
    except (TypeError, ValueError):
        return string
    if not padding:
        return string
    needed = width - len(string)
    if needed <= 0:
        return string
    pad_repeated = (padding * (needed // len(padding) + 1))[:needed]
    if from_left:
        return pad_repeated + string
    else:
        return string + pad_repeated


def sharp_padleft(string, width, padding='0', *args):
    """{{padleft: STRING | WIDTH | PADDING }} -- pads on the left
    (prepends), matching real MediaWiki's own naming, which describes
    which side gets the new padding characters -- not which side of
    the original string they end up nearer to."""
    return _sharp_pad(string, width, padding, from_left=True)


def sharp_padright(string, width, padding='0', *args):
    """{{padright: STRING | WIDTH | PADDING }} -- pads on the right
    (appends). Same padding-string repeat-then-truncate semantics as
    padleft -- see _sharp_pad()'s own docstring."""
    return _sharp_pad(string, width, padding, from_left=False)


def sharp_len(string='', *args):
    """{{#len: STRING }} -- character count. String functions
    extension, hence the '#' prefix (unlike lc/uc/padleft/padright
    above, which are core magic words without one)."""
    return str(len(string))


def sharp_pos(string='', target='', offset='0', *args):
    """{{#pos: STRING | TARGET | OFFSET }} -- zero-based index of the
    first occurrence of TARGET in STRING, searching from OFFSET
    (default 0). Real MediaWiki semantics: returns an EMPTY STRING,
    not -1, when TARGET isn't found -- Python's str.find() returns
    -1, so that gets converted here rather than passed through
    directly. Deliberately asymmetric with #rpos below, which returns
    -1 on no match -- that's a real, documented quirk of the actual
    String functions extension, not something to "fix" into
    consistency here.
    """
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    result = string.find(target, offset)
    return '' if result == -1 else str(result)


def sharp_rpos(string='', target='', *args):
    """{{#rpos: STRING | TARGET }} -- zero-based index of the LAST
    occurrence of TARGET in STRING. Real MediaWiki semantics: returns
    -1 (as a string) when TARGET isn't found -- unlike #pos above,
    which returns empty string on no match. This asymmetry is real
    and documented, not a bug to reconcile between the two."""
    return str(string.rfind(target))


def sharp_sub(string='', start='0', length=None, *args):
    """{{#sub: STRING | START | LENGTH }} -- substring, matching
    PHP's substr() semantics (what real MediaWiki's #sub is built on)
    precisely rather than approximating with a plain Python slice:
      - START >= 0: begins at that position (0-indexed).
      - START < 0: begins that many characters from the end.
      - LENGTH omitted: returns everything from START to the end.
      - LENGTH >= 0: returns up to LENGTH characters.
      - LENGTH < 0: stops that many characters before the end of the
        *whole string* -- not relative to START. This is the part a
        naive s[start:start+length]-style translation gets wrong:
        substr("Hello world", 6, -2) is "wor" (stop 2 short of the
        whole string's own end), not an empty/nonsensical result from
        computing 6 + -2 = 4 (before start) and slicing s[6:4].
    """
    n = len(string)
    try:
        start = int(start)
    except (TypeError, ValueError):
        start = 0
    if start < 0:
        start = max(n + start, 0)
    else:
        start = min(start, n)
    if length is None or length == '':
        end = n
    else:
        try:
            length = int(length)
        except (TypeError, ValueError):
            length = 0
        if length >= 0:
            end = min(start + length, n)
        else:
            end = max(n + length, start)
    return string[start:end]


def sharp_count(string='', substring='', *args):
    """{{#count: STRING | SUBSTRING }} -- non-overlapping occurrence
    count, matching PHP's substr_count(). An empty SUBSTRING has no
    sensible count (Python's own str.count('') counts a match between
    every character, e.g. "abc".count('') == 4, which isn't a
    meaningful answer here), so that case returns '0' rather than
    passing through Python's own quirky definition.
    """
    if not substring:
        return '0'
    return str(string.count(substring))


def sharp_replace(string='', search='', replace='', limit=None, *args):
    """{{#replace: STRING | SEARCH | REPLACE | LIMIT }} -- replaces
    up to LIMIT occurrences of SEARCH with REPLACE (default: all of
    them). An empty SEARCH is left as a no-op returning STRING
    unchanged, rather than Python's own str.replace('', x) behavior
    of inserting x between every character -- PHP's str_replace()
    (what real MediaWiki's #replace is built on) treats an empty
    search as not matching anything, not as matching every gap.
    """
    if not search:
        return string
    if limit is None or limit == '':
        return string.replace(search, replace)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return string.replace(search, replace)
    return string.replace(search, replace, limit)


def sharp_explode(string='', delimiter='', position='0', limit=None, *args):
    """{{#explode: STRING | DELIMITER | POSITION | LIMIT }} -- splits
    STRING on DELIMITER and returns the segment at zero-based
    POSITION (real MediaWiki supports a negative POSITION too,
    counting from the end -- matches Python's own negative list
    indexing directly, so no special-casing needed for that part).
    LIMIT caps the number of segments produced, with the LAST segment
    absorbing everything beyond that count -- exactly Python's own
    str.split(delimiter, maxsplit) semantics, so LIMIT translates to
    maxsplit = LIMIT - 1. An empty DELIMITER has no valid split
    semantics (real PHP explode() rejects it outright), so that
    returns '' rather than raising or guessing a behavior. POSITION
    outside the resulting segment count also returns ''.
    """
    if not delimiter:
        return ''
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = 0
    if limit is None or limit == '':
        parts = string.split(delimiter)
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = None
        parts = string.split(delimiter, limit - 1) if limit and limit > 0 else string.split(delimiter)
    if -len(parts) <= position < len(parts):
        return parts[position]
    return ''



# Extension Scribuntu
# Only minimal support for Lua modules invoked via #invoke.
# FIXME: import real Lua modules (would require a Lua interpreter,
# which this project doesn't have).
#
# Must stay defined in this module, not a caller's -- sharp_invoke()
# below reads this as a global, and Python resolves that against the
# function's own defining module, not wherever it's called from.
modules = {
    'convert': {
        'convert': lambda x, u, *rest: x + ' ' + u,  # no conversion
    }
}


def sharp_invoke(module, function, frame):
    functions = modules.get(module)
    if functions:
        funct = functions.get(function)
        if funct:
            # Use the innermost (most recently entered) frame entry --
            # frame is a proper stack (see expandTemplate: appended
            # right before expanding a template's body, popped right
            # after), so frame[-1] is exactly the template invocation
            # that directly encloses this #invoke call, matching real
            # Scribunto's frame:getParent() semantics. Don't match by
            # guessing the calling template's title from the function
            # name instead -- that only works when they happen to
            # coincide (e.g. Template:Convert invoking "convert"), and
            # silently breaks for any differently-named alias template
            # invoking the same function (e.g. {{cvt|...}}).
            if frame:
                params = frame[-1][1]
                # extract positional args
                params = [params.get(str(i + 1)) for i in range(len(params))]
                return funct(*params)
            else:
                return funct()
    return ''


# Parser functions that expand their arguments lazily, rather than all
# up front. The branching functions choose one of their arguments and
# discard the others, so only the part that decides the branch is
# expanded eagerly (that is parts[0], expanded as part of the title in
# expandTemplate()); whichever branch is selected is expanded
# afterwards, by the expandTemplates() call applied to the return
# value. Expanding every branch up front would do the work of every
# arm of a large #switch to keep one, and would run expansions the
# page never asked for.
#
# '#invoke' is here for a different reason: sharp_invoke() takes the
# module and function names as written, along with the frame.
#
# Everything else in parserFunctions below is a value function --
# padleft, lc, #sub, formatnum and the rest -- which MediaWiki invokes
# with every argument already expanded.
lazyParserFunctions = {
    '#if',
    '#ifeq',
    '#iferror',
    '#ifexist',
    '#ifexpr',
    '#switch',
    '#invoke',
}

parserFunctions = {

    # '#expr' and '#ifexpr' are handled directly in
    # callParserFunction(), same as '#invoke' below them, not
    # dispatched through this dict -- both need page_title/page_id/
    # extractor threaded through for #expr's own failure logging
    # (#ifexpr evaluates its condition via sharp_expr() itself, so it
    # needs the same three), which no other entry here needs.

    '#if': sharp_if,

    '#ifeq': sharp_ifeq,

    '#iferror': sharp_iferror,

    '#ifexist': lambda *args: '',  # not supported

    '#rel2abs': lambda *args: '',  # not supported

    '#switch': sharp_switch,

    '# language': lambda *args: '',  # not supported

    '#time': sharp_time,

    # Local time, which would need the source wiki's own configured
    # timezone; sharp_time() works in UTC, which is what MediaWiki
    # stores timestamps in and what #time itself uses.
    '#timel': sharp_time,

    '#titleparts': lambda *args: '',  # not supported

    # String functions extension -- these are called as {{#len:...}},
    # {{#pos:...}}, etc. in real wikitext, with a '#' prefix. The core
    # case/url magic words further below (lc, uc, ucfirst, lcfirst,
    # padleft, padright, urlencode, urldecode) are called without one
    # -- {{lc:...}}, not {{#lc:...}} -- since they belong to a
    # different, older part of MediaWiki's own syntax, not this
    # extension. Both kinds are dispatched through this same dict
    # either way; the '#' is just literally part of the string
    # functions' own names as MediaWiki defines them.
    '#len': sharp_len,

    '#pos': sharp_pos,

    '#rpos': sharp_rpos,

    '#sub': sharp_sub,

    '#count': sharp_count,

    '#replace': sharp_replace,

    '#explode': sharp_explode,

    # This function is used in some pages to construct links
    # http://meta.wikimedia.org/wiki/Help:URL
    'urlencode': lambda string, *rest: urlencode(string),

    'urldecode': lambda string, *rest: urldecode(string),

    'lc': lambda string, *rest: string.lower() if string else '',

    'lcfirst': lambda string, *rest: lcfirst(string),

    'uc': lambda string, *rest: string.upper() if string else '',

    'ucfirst': lambda string, *rest: ucfirst(string),

    'int': lambda string, *rest: str(int(string)),

    'padleft': sharp_padleft,

    'padright': sharp_padright,

    'formatnum': sharp_formatnum,

}


def callParserFunction(functionName, args, frame, page_title=None, page_id=None, extractor=None):
    """
    Parser functions have similar syntax as templates, except that
    the first argument is everything after the first colon.
    :param functionName: nameof the parser function
    :param args: the arguments to the function
    :param page_title: :param page_id: the calling article's own
        title/id (not necessarily the template's -- a #expr call
        reached here may live inside a transcluded template, but the
        article is what a real investigation would start from anyway,
        and it's what's actually available here). Threaded through to
        #expr and #ifexpr specifically, which log them on failure to
        make a malformed on-wiki call findable; no other parser
        function currently needs them.
    :param extractor: the calling Extractor. Threaded through to
        #expr and #ifexpr for their own per-article malformed-#expr
        counting/dedup (see sharp_expr()'s own docstring), and its
        expandTemplates is what #ifeq and #switch expand their
        comparison operands with (see lazyParserFunctions). Without
        one, those operands are compared as given.
    :return: the result of the invocation, None in case of failure.

    http://meta.wikimedia.org/wiki/Help:ParserFunctions
    """

    # #ifeq and #switch expand their comparison operands themselves,
    # on demand; expandTemplates is bound here rather than taken as a
    # separate parameter, so there is one Extractor in play and no way
    # for a caller to pair one Extractor's state with another's
    # expansion.
    expand = extractor.expandTemplates if extractor is not None else None

    try:
        if functionName == '#invoke':
            # special handling of frame
            ret = sharp_invoke(args[0].strip(), args[1].strip(), frame)
            # logger.debug('parserFunction> %s %s', args[1], ret)
            return ret
        if functionName == '#expr':
            return sharp_expr(*args, page_title=page_title, page_id=page_id, extractor=extractor)
        if functionName == '#ifexpr':
            return sharp_ifexpr(*args, page_title=page_title, page_id=page_id, extractor=extractor)
        if functionName == '#ifeq':
            return sharp_ifeq(*args, expand=expand)
        if functionName == '#switch':
            return sharp_switch(*args, expand=expand)
        if functionName in parserFunctions:
            ret = parserFunctions[functionName](*args)
            # logger.debug('parserFunction> %s(%s) %s', functionName, args, ret)
            return ret
    except:
        return ""  # FIXME: fix errors

    return ""


# ----------------------------------------------------------------------
# Extract Template definition

# One trailing newline right after </noinclude> is collapsed, matching
# MediaWiki's own stripped/preserved whitespace rules:
# https://www.mediawiki.org/wiki/Manual:Newlines_and_spaces
# Trailing-side only (not before the opening tag too): stripping both
# sides would merge unrelated content that precedes/follows the
# <noinclude> block onto a single line, rather than just closing the
# gap the removed block leaves behind. Not extended to <includeonly>,
# whose content is always kept either way, so removing just its tags
# leaves no such gap to begin with.
reNoinclude = re.compile(r'<noinclude>(?:.*?)</noinclude>\n?', re.DOTALL)
reIncludeonly = re.compile(r'<includeonly>|</includeonly>', re.DOTALL)


def resolve_template_page(title, page):
    """
    Given a template page's raw text lines, determines whether it's a
    redirect or a genuine template definition, and if the latter,
    computes its final, stored text (comments stripped,
    noinclude/includeonly/onlyinclude resolved per
    https://en.wikipedia.org/wiki/Help:Template#Noinclude.2C_includeonly.2C_and_onlyinclude).

    Returns one of:
      - None: nothing to store (an empty page, or a template whose
        body is empty once noinclude/includeonly processing is done).
      - ('redirect', target_title)
      - ('template', final_text)

    Used by both define_template() (writes into a plain {title: text}
    dict) and template_blob's streaming builder (appends straight into
    a shared-memory blob without ever building that dict at all) --
    kept as the one place this resolution logic lives so both stay in
    sync automatically.
    """
    if not page:
        return None

    m = redirectRE.match(page[0])
    if m:
        return ('redirect', m.group(1))

    text = unescape(''.join(page))
    text = comment.sub('', text)
    text = reNoinclude.sub('', text)
    text = re.sub(r'<noinclude\s*>.*$', '', text, flags=re.DOTALL)
    text = re.sub(r'<noinclude/>\n?', '', text)

    onlyincludeAccumulator = ''
    for m in re.finditer(r'<onlyinclude>\n?(.*?)\n?</onlyinclude>', text, re.DOTALL):
        onlyincludeAccumulator += m.group(1)
    if onlyincludeAccumulator:
        text = onlyincludeAccumulator
    else:
        text = reIncludeonly.sub('', text)

    if not text:
        return None
    return ('template', text)


def define_template(title, page, templates, redirects):
    """
    Adds a template defined in the :param page: to :param templates:,
    or a redirect to :param redirects:.
    :param templates: the {title: text} dict to populate.
    :param redirects: the {title: target_title} dict to populate.
    Both required, not defaulted, since this function's entire job is
    writing into one or the other; a silent "if not given, use a
    throwaway empty dict" default would make a forgotten argument fail
    silently (the page is "defined" into a dict nobody keeps) rather
    than loudly, which is worse than just requiring it.
    """
    result = resolve_template_page(title, page)
    if result is None:
        return
    kind, value = result
    if kind == 'redirect':
        redirects[title] = value
        return
    text = value
    if title in templates and templates[title] != text:
        logger.warning('Redefining: %s', title)
    templates[title] = text
