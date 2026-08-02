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
import html
import json
import ast
import operator
from itertools import zip_longest
from urllib.parse import quote as urlencode
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

# ----------------------------------------------------------------------

# match tail after wikilink
tailRE = re.compile(r'\w+')
syntaxhighlight = re.compile('&lt;syntaxhighlight .*?&gt;(.*?)&lt;/syntaxhighlight&gt;', re.DOTALL)

## PARAMS ####################################################################

##
# Defined in <siteinfo>
# We include as default Template, when loading external template file.
knownNamespaces = set(['Template'])

##
# The #REDIRECT keyword, localized. MediaWiki's real redirect magic
# word has a per-wiki-language translation (e.g. Sindhi's own content
# language uses "چوريو" instead of "REDIRECT"), separate from the
# interface language -- matching only the English form meant a
# redirect page in a non-English wiki wasn't recognized as a redirect
# at all, and its entire (often stale, pre-redirect) body text got
# treated as the template's real content instead.
#
# Confirmed for Sindhi specifically: found "#چوريو [[Target]]" as the
# very first line of two separate, independent template pages in a
# real Sindhi Wikipedia dump (both structurally identical to a
# standard redirect: hash-prefixed keyword immediately followed by a
# wikilink, as the first thing on the page), and confirmed directly
# that treating it as a redirect (rather than as literal template
# body text) eliminates a real, reproduced content-leak bug. Not
# confirmed against MediaWiki's own localization source specifically
# (couldn't get a fetchable copy of it), but multiple independent
# structural signals plus the direct empirical fix both point the same
# way.
#
# Extensible: add further confirmed, per-language keywords here as
# they turn up on other wikis, rather than guessing translations
# preemptively for languages not yet actually encountered.
redirectKeywords = ['REDIRECT', 'چوريو']
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
    'inputbox'
]

##
# Recognize only these namespaces
# w: Internal links to the Wikipedia
# wiktionary: Wiki dictionary
# wikt: shortcut for Wiktionary
#
acceptedNamespaces = ['w', 'wiktionary', 'wikt']


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
    text = replaceExternalLinks(text)

    # replace internal links
    text = replaceInternalLinks(text)

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
    # whitespace in the source (a real, confirmed case on Saraiki
    # Wikipedia: "اُٹھا<br>رب" with no spaces at all around the tag,
    # which would otherwise become "اُٹھارب" -- two words fused into
    # one). Substitute these with a space, but only where there's
    # actually something to merge with on both sides -- a line-break
    # tag sitting at the very start/end of a line (immediately next to
    # a newline, or at the start/end of the text) needs no additional
    # separator, since there's nothing on the empty side to merge
    # with; adding one there just creates an invisible leading or
    # trailing space that doesn't affect meaning but does clutter
    # every diff against such a line.
    #
    # This MUST run before any of the span-collecting steps below:
    # substituteLineBreakTag() changes text's length (a longer tag
    # collapses to a single space), so any span collected beforehand
    # (comments, self-closing tags, ignored tags) would hold stale
    # positions once dropSpans() later runs against the shifted text
    # -- a real, confirmed bug found on a real Urdu Wikipedia article
    # ("محمد علی جناح"/Muhammad Ali Jinnah, id 1086): two of six HTML
    # comments after a br/hr substitution earlier in the article
    # survived untouched, because dropSpans() ended up removing the
    # wrong span of characters entirely, at positions that no longer
    # corresponded to where those comments actually were.
    for pattern in lineBreak_tag_patterns:
        text = substituteLineBreakTag(pattern, text)

    # Same must-run-before-span-collection reasoning as the br/hr
    # substitution just above applies here too: this also mutates
    # text's length, so it has to happen before anything else records
    # a position into this same text.
    for pattern in block_separator_tag_patterns:
        text = substituteLineBreakTag(pattern, text, separator='\n')

    spans = []
    # Drop HTML comments
    for m in comment.finditer(text):
        spans.append((m.start(), m.end()))

    # Drop self-closing tags
    for pattern in selfClosing_tag_patterns:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    # Drop ignored tags
    for left, right in ignored_tag_patterns:
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
        # "geo/18aug2018-1"), which the old, blanket '/'-excluding
        # character class broke on -- the opening tag simply failed
        # to match at all, surviving as literal text, while its
        # closing tag (left unpaired) got correctly stripped by the
        # orphaned-close-tag handling below, producing a mismatched
        # "closing tag vanished, opening tag remains" result. Only a
        # '/' immediately before the final '>' should be excluded here
        # -- that's a genuine self-closing tag (e.g. <ref name="x" />,
        # already handled separately by selfClosing_tag_patterns
        # above), not a wrapping open that discardElements should
        # pair up.
        # (?=(...))\1 emulates an atomic/possessive match for the
        # quoted alternatives (see lineBreak_tag_patterns above for
        # the full reasoning) -- needed here specifically because
        # without it, the (?<!/) exclusion below can force a
        # backtrack that falls back to treating quote characters as
        # plain [^>] matches, finding a WRONG match that ends at a
        # quoted value's own inner '>' instead of failing to match
        # (correctly) on a genuine self-closing tag like
        # <ref style="a > b" />.
        text = dropNested(text, r'''<\s*%s\b(?:(?=("[^"]*"|'[^']*'|[^>]))\1)*(?<!/)>''' % tag,
                           close_pattern)
        # dropNested only ever removes a close tag as part of a
        # matched (open, close) pair -- an unpaired one
        # (its own opening tag consumed or malformed elsewhere, e.g. by
        # a failed nested template expansion earlier on the same page)
        # is left completely untouched by its pairing logic, rather
        # than throwing off matching for the rest of the document.
        # So anything still matching close_pattern at this point is
        # genuinely orphaned within this text -- same "strip the stray
        # tag rather than guess at pairing" approach as the noinclude
        # handling below.
        text = re.sub(close_pattern, '', text, flags=re.IGNORECASE)

    # Any <noinclude>/</noinclude> still remaining at this point is
    # genuinely unmatched within this page's own text -- a properly
    # paired instance would already have been removed, tags and
    # content together, by the loop above. noinclude is a
    # template-specific construct; its most likely source in a
    # REGULAR article (not a template page) is misplaced markup a
    # human editor accidentally copy-pasted directly from a template,
    # confirmed on a real PNB Wikipedia article ("اربیم"/Erbium,
    # id 113) where the closing tag appears BEFORE its "opening"
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
    text = re.sub(r' (,:\.\)\]»)', r'\1', text)
    text = re.sub(r'(\[\(«) ', r'\1', text)
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


def compact(text, mark_headers=False):
    """Deal with headers, lists, empty sections, residuals of tables.
    :param text: convert to HTML
    """

    page = []  # list of paragraph
    headers = {}  # Headers for unfilled sections
    emptySection = False  # empty sections are discarded
    listLevel = ''  # nesting of lists

    for line in text.split('\n'):

        if not line:
            if len(listLevel):    # implies Extractor.HtmlFormatting
                for c in reversed(listLevel):
                    page.append(listClose[c])
                    listLevel = ''
            continue

        # Handle section titles
        m = section.match(line)
        if m:
            title = m.group(2)
            lev = len(m.group(1))
            if Extractor.HtmlFormatting:
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
            if Extractor.HtmlFormatting:
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
        elif len(listLevel):    # implies Extractor.HtmlFormatting
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
            if Extractor.keepSections:
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


def replaceExternalLinks(text):
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
            label = makeExternalImage(label)

        # Use the encoded URL
        # This means that users can paste URLs directly into the text
        # Funny characters like ö aren't valid in URLs anyway
        # This was changed in August 2004
        s += makeExternalLink(url, label)  # + trail

    return s + text[cur:]


def makeExternalLink(url, anchor):
    """Function applied to wikiLinks"""
    if Extractor.keepLinks:
        return '<a href="%s">%s</a>' % (urlencode(url), anchor)
    else:
        return anchor


def makeExternalImage(url, alt=''):
    if Extractor.keepLinks:
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
    everything after it. An earlier version of this function bounded
    detection to a single paragraph specifically to avoid long-distance
    accidental pairing with an unrelated stray bracket elsewhere in the
    same article, but that turned out to cause a worse, confirmed
    problem in practice: a real File: link (English Wikipedia,
    "Asterix") has a <ref>...</ref> citation, nested inside its image
    caption, that itself contains a genuine blank line before its
    closing tag -- entirely legitimate wikitext, just untidy
    formatting. Paragraph-bounding incorrectly treated the File: link's
    opening as unclosed (since its true close was one blank line away),
    causing the whole link to survive as literal text instead of being
    cleanly dropped, exactly the correct behavior it has without this
    fix at all. Whole-text detection resolves this correctly, since the
    true closing "]]" is found and paired normally, no differently
    than findBalanced() would have paired it on its own.
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


def replaceInternalLinks(text):
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
        res += text[cur:s] + makeInternalLink(title, label) + trail
        cur = end
    return (res + text[cur:]).replace(LINK_OPEN_PLACEHOLDER, '[[')


def makeInternalLink(title, label):
    colon = title.find(':')
    if colon > 0 and title[:colon] not in acceptedNamespaces:
        return ''
    if colon == 0:
        # drop also :File:
        colon2 = title.find(':', colon + 1)
        if colon2 > 1 and title[colon + 1:colon2] not in acceptedNamespaces:
            return ''
    if Extractor.keepLinks:
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
# no implied break at all, confirmed against real HTML tokenizer/CSS
# default-display behavior). Stripped the same tag-syntax-removed,
# content-kept way, but via a newline substitution (see
# substituteLineBreakTag()) rather than plain deletion -- otherwise
# two adjacent blocks with no whitespace between them in the source
# fuse into one run-on string, the same class of bug as the earlier
# br/hr word-merging fix, just for a different set of tags. A newline
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
    'plaintext', 'poem', 's', 'section', 'span', 'strike', 'strong',
    'sub', 'sup', 'tt', 'u', 'var'
)

placeholder_tags = {'math': 'formula', 'code': 'codice'}


def normalizeTitle(title):
    """Normalize title"""
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
        if ns in knownNamespaces:
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

# Match ignored tags
ignored_tag_patterns = []


def ignoreTag(tag):
    left = re.compile(r'<%s\b.*?>' % tag, re.IGNORECASE | re.DOTALL)  # both <ref> and <reference>
    right = re.compile(r'</\s*%s\s*>' % tag, re.IGNORECASE)  # space allowed, such as </span >
    ignored_tag_patterns.append((left, right))


def resetIgnoredTags():
    global ignored_tag_patterns
    ignored_tag_patterns = []


for tag in ignoredTags:
    ignoreTag(tag)

# Match selfClosing HTML tags
selfClosing_tag_patterns = [
    # nobr is treated the same permissive way as br/hr for matching
    # purposes (optional trailing slash), since a bare, unclosed
    # <nobr> is the same kind of stray/orphaned tag -- but unlike
    # br/hr, "no line break" doesn't call for inserting a space where
    # the tag was, so it stays in the pure-deletion group below rather
    # than moving to lineBreak_tag_patterns.
    #
    # ref/references/nowiki/templatestyles are NOT treated this way:
    # for ref specifically, the self-closing form has a distinct, real
    # meaning (e.g. <ref name="x" /> reuses an earlier-defined
    # reference) from the non-self-closing form (<ref
    # name="x">actual citation text</ref>, a genuine paired tag with
    # real content) -- making the slash optional would misidentify the
    # OPENING of a real paired tag as if it were self-closing.
    # templatestyles is always used in self-closing form in real
    # MediaWiki usage (it loads CSS for a template's rendering, never
    # wraps real content), so the strict pattern doesn't lose anything
    # for it either.
    # (?=(...))\1 emulates an atomic/possessive match for the quoted
    # alternatives (see lineBreak_tag_patterns below for the full
    # reasoning) -- without it, a literal '>' inside a quoted
    # attribute value (e.g. <ref style="a > b" />) would prevent this
    # from matching at all, since [^>]* alone stops at that inner '>'
    # and can never find the real, required trailing '/' after it.
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
lineBreak_tag_patterns = [
    # (?=(...))\1 emulates an atomic/possessive match for the quoted
    # alternatives, portably (works pre-3.11 too, unlike native atomic
    # groups): once a quoted string is matched, the engine can never
    # backtrack into re-interpreting its own quote characters as
    # individual [^>] matches -- confirmed directly this matters, not
    # just theoretical: without it, a literal '>' inside a quoted
    # attribute value (e.g. <br style="a > b" />, legal HTML -- a
    # literal '>' inside a quoted value doesn't end the tag, confirmed
    # against a real HTML tokenizer) truncates the match early, at
    # that inner '>', leaving the tag's own real ending stranded as
    # literal text afterward.
    re.compile(r'''<\s*%s\b(?:(?=("[^"]*"|'[^']*'|[^>]))\1)*>''' % tag,
               re.DOTALL | re.IGNORECASE)
    for tag in lineBreakTags
]

# blockSeparatorTags (see the comment there) are substituted with a
# newline rather than a space, via the same substituteLineBreakTag()
# mechanism -- each tag contributes its own opening AND closing
# pattern separately here, since (unlike br/hr, which are single,
# self-closing tags) these have two distinct halves, each appearing at
# a different position and needing its own independent substitution.
# Same shapes as ignoreTag()'s own left/right patterns, for
# consistency: opening requires the tag name immediately after '<',
# matching real HTML tokenizer behavior (a bare '< p>' is not treated
# as a tag at all by real parsers, so this shouldn't either); closing
# tolerates whitespace on either side of the name.
block_separator_tag_patterns = [
    pattern
    for tag in blockSeparatorTags
    for pattern in (
        re.compile(r'<%s\b.*?>' % tag, re.IGNORECASE | re.DOTALL),
        re.compile(r'</\s*%s\s*>' % tag, re.IGNORECASE),
    )
]


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

    @classmethod
    def parse(cls, body):
        tpl = Template()
        # we must handle nesting, s.a.
        # {{{1|{{PAGENAME}}}
        # {{{italics|{{{italic|}}}
        # {{#if:{{{{{#if:{{{nominee|}}}|nominee|candidate}}|}}}|
        #
        start = 0
        for s,e in findMatchingBraces(body, 3):
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
        self.name = Template.parse(parts[0])
        if len(parts) > 1:
            # This parameter has a default value
            self.default = Template.parse(parts[1])
        else:
            self.default = None

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


class Extractor():
    """
    An extraction task on a article.
    """
    ##
    # Whether to preserve links in output
    keepLinks = False

    ##
    # Whether to preserve section titles
    keepSections = True

    ##
    # Whether to output text with HTML formatting elements in <doc> files.
    HtmlFormatting = False

    ##
    # Whether to produce json instead of the default <doc> output format.
    to_json = False
    # Whether to produce text instead of the default <doc> output format.
    to_text = False

    ##
    # Whether or not to discard empty (title only) documents
    discard_empty = False

    ##
    # Obtained from TemplateNamespace
    templatePrefix = ''

    def __init__(self, id, revid, urlbase, title, page):
        """
        :param page: a list of lines.
        """
        self.id = id
        self.revid = revid
        self.url = get_url(urlbase, id)
        self.title = title
        self.page = page
        self.magicWords = MagicWords()
        self.frame = []
        self.recursion_exceeded_1_errs = 0  # template recursion within expandTemplates()
        self.recursion_exceeded_2_errs = 0  # template recursion within expandTemplate()
        self.recursion_exceeded_3_errs = 0  # parameter recursion
        self.template_title_errs = 0
        self.template_loop_errs = 0  # same (title, params) reappearing in its own expansion chain
        self.warned_loop_keys = set()  # (id, title) pairs already warned about, to avoid log spam

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

        text = compact(text, mark_headers=mark_headers)
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
            header = '<doc id="%s" url="%s" title="%s">\n' % (self.id, self.url, self.title)
            # Separate header from text with a newline.
            header += self.title + '\n\n'
            footer = "\n</doc>\n"
            out.write(header)
            out.write('\n'.join(text))
            out.write('\n')
            out.write(footer)

        errs = (self.template_title_errs,
                self.recursion_exceeded_1_errs,
                self.recursion_exceeded_2_errs,
                self.recursion_exceeded_3_errs,
                self.template_loop_errs)
        if any(errs):
            logger.warning("Template errors in article '%s' (%s): title(%d) recursion(%d, %d, %d) loop(%d)",
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

        cur = 0
        # look for matching {{...}}
        for s, e in findMatchingBraces(wikitext, 2):
            res += wikitext[cur:s] + self.expandTemplate(wikitext[s + 2:e - 2])
            cur = e
        # leftover
        res += wikitext[cur:]
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

            m = re.match(" *([^=']*?) *=(.*)", param, re.DOTALL)
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
        logger.debug('   templateParams> %s', '|'.join(templateParams.values()))
        return templateParams

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
        if re.match(substWords, title, re.IGNORECASE):
            title = re.sub(substWords, '', title, 1, re.IGNORECASE)
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
            # arguments after first are not evaluated
            ret = callParserFunction(funct, parts, self.frame)
            return self.expandTemplates(ret)

        title = fullyQualifiedTemplateTitle(title)
        if not title:
            self.template_title_errs += 1
            return ''

        redirected = redirects.get(title)
        if redirected:
            title = redirected

        # get the template
        if title in templateCache:
            template = templateCache[title]
        elif title in templates:
            template = Template.parse(templates[title])
            # add it to cache
            templateCache[title] = template
            del templates[title]
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
                logger.warning("Template loop detected: %s (article %s, id %s) -- "
                                 "leaving unexpanded (further repeats in this "
                                 "article are counted but not logged)",
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

    if ldelim:  # 2-3
        reOpen = re.compile(r'[{]{%d,}' % ldelim)  # at least ldelim
        reNext = re.compile(r'[{]{2,}|}{2,}')  # at least 2 open or close bracces
    else:
        reOpen = re.compile(r'{{2,}|\[{2,}')
        reNext = re.compile(r'{{2,}|}{2,}|\[{2,}|]{2,}')  # at least 2

    cur = 0
    while True:
        m1 = reOpen.search(text, cur)
        if not m1:
            return
        lmatch = m1.end() - m1.start()
        if m1.group()[0] == '{':
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
                    yield m1.start(), end - lmatch
                    cur = end
                    break
                elif len(stack) == 1 and 0 < stack[0] < ldelim:
                    # ambiguous {{{{{ }}} }}
                    yield m1.start() + stack[0], end
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


def fullyQualifiedTemplateTitle(templateTitle):
    """
    Determine the namespace of the page being included through the template
    mechanism
    """
    if templateTitle.startswith(':'):
        # Leading colon by itself implies main namespace, so strip this colon
        return ucfirst(templateTitle[1:])
    else:
        m = re.match(r'([^:]*)(:.*)', templateTitle)
        if m:
            # colon found but not in the first position - check if it
            # designates a known namespace
            prefix = normalizeNamespace(m.group(1))
            if prefix in knownNamespaces:
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
        return Extractor.templatePrefix + ucfirst(templateTitle)
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


def sharp_expr(expr):
    try:
        expr = re.sub('=', '==', expr)
        expr = re.sub('mod', '%', expr)
        expr = re.sub(r'\bdiv\b', '/', expr)
        expr = re.sub(r'\bround\b', '|ROUND|', expr)
        tree = ast.parse(expr, mode='eval')
        return str(_sharp_expr_eval_node(tree))
    except Exception:
        return '<span class="error"></span>'


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


def sharp_ifeq(lvalue, rvalue, valueIfTrue, valueIfFalse=None, *args):
    rvalue = rvalue.strip()
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


def sharp_switch(primary, *params):
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
        lvalue = pair[0].strip()
        rvalue = None
        if len(pair) > 1:
            # got "="
            rvalue = pair[1].strip()
            # check for any of multiple values pipe separated
            if found or primary in [v.strip() for v in lvalue.split('|')]:
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


parserFunctions = {

    '#expr': sharp_expr,

    '#if': sharp_if,

    '#ifeq': sharp_ifeq,

    '#iferror': sharp_iferror,

    '#ifexpr': lambda *args: '',  # not supported

    '#ifexist': lambda *args: '',  # not supported

    '#rel2abs': lambda *args: '',  # not supported

    '#switch': sharp_switch,

    '# language': lambda *args: '',  # not supported

    '#time': lambda *args: '',  # not supported

    '#timel': lambda *args: '',  # not supported

    '#titleparts': lambda *args: '',  # not supported

    # This function is used in some pages to construct links
    # http://meta.wikimedia.org/wiki/Help:URL
    'urlencode': lambda string, *rest: urlencode(string),

    'lc': lambda string, *rest: string.lower() if string else '',

    'lcfirst': lambda string, *rest: lcfirst(string),

    'uc': lambda string, *rest: string.upper() if string else '',

    'ucfirst': lambda string, *rest: ucfirst(string),

    'int': lambda string, *rest: str(int(string)),

    'padleft': lambda char, width, string: string.ljust(char, int(pad)), # CHECK_ME

}


def callParserFunction(functionName, args, frame):
    """
    Parser functions have similar syntax as templates, except that
    the first argument is everything after the first colon.
    :param functionName: nameof the parser function
    :param args: the arguments to the function
    :return: the result of the invocation, None in case of failure.

    http://meta.wikimedia.org/wiki/Help:ParserFunctions
    """

    try:
        if functionName == '#invoke':
            # special handling of frame
            ret = sharp_invoke(args[0].strip(), args[1].strip(), frame)
            # logger.debug('parserFunction> %s %s', args[1], ret)
            return ret
        if functionName in parserFunctions:
            ret = parserFunctions[functionName](*args)
            # logger.debug('parserFunction> %s(%s) %s', functionName, args, ret)
            return ret
    except:
        return ""  # FIXME: fix errors

    return ""


# ----------------------------------------------------------------------
# Extract Template definition

reNoinclude = re.compile(r'<noinclude>(?:.*?)</noinclude>', re.DOTALL)
reIncludeonly = re.compile(r'<includeonly>|</includeonly>', re.DOTALL)

# These are built before spawning processes, hence they are shared.
templates = {}
redirects = {}
# cache of parser templates
# FIXME: sharing this with a Manager slows down.
templateCache = {}


def define_template(title, page):
    """
    Adds a template defined in the :param page:.
    @see https://en.wikipedia.org/wiki/Help:Template#Noinclude.2C_includeonly.2C_and_onlyinclude
    """
    global templates
    global redirects

    # title = normalizeTitle(title)

    # An empty page (zero lines) is a genuine, valid case for a
    # template whose current revision has no content at all (a
    # self-closing <text bytes="0" .../> in the source) -- not a
    # redirect, and not any real content either. Confirmed this is
    # reachable now that collect_pages()/load_templates() correctly
    # recognize that self-closing form instead of silently merging the
    # next page's content into this one (which previously masked this
    # case entirely, since page was never actually empty by the time
    # it reached here).
    if not page:
        return

    # check for redirects
    m = redirectRE.match(page[0])
    if m:
        redirects[title] = m.group(1)  # normalizeTitle(m.group(1))
        return

    text = unescape(''.join(page))

    # We're storing template text for future inclusion, therefore,
    # remove all <noinclude> text and keep all <includeonly> text
    # (but eliminate <includeonly> tags per se).
    # However, if <onlyinclude> ... </onlyinclude> parts are present,
    # then only keep them and discard the rest of the template body.
    # This is because using <onlyinclude> on a text fragment is
    # equivalent to enclosing it in <includeonly> tags **AND**
    # enclosing all the rest of the template body in <noinclude> tags.

    # remove comments
    text = comment.sub('', text)

    # eliminate <noinclude> fragments
    text = reNoinclude.sub('', text)
    # eliminate unterminated <noinclude> elements
    text = re.sub(r'<noinclude\s*>.*$', '', text, flags=re.DOTALL)
    text = re.sub(r'<noinclude/>', '', text)

    onlyincludeAccumulator = ''
    for m in re.finditer('<onlyinclude>(.*?)</onlyinclude>', text, re.DOTALL):
        onlyincludeAccumulator += m.group(1)
    if onlyincludeAccumulator:
        text = onlyincludeAccumulator
    else:
        text = reIncludeonly.sub('', text)

    if text:
        if title in templates and templates[title] != text:
            logger.warning('Redefining: %s', title)
        templates[title] = text
