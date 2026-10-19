#!/usr/bin/env python3
"""
audit_empty_docs.py

Finds every document in an extraction whose body is empty, and decides
for each one whether the source page actually had extractable content.

An article that extracts to nothing leaves nothing behind to notice, so
a run can lose articles silently. But most empty documents are correct:
a stub whose entire body is an infobox and a navbox has no prose to
extract, and tables are dropped by design. This tool separates the two.

For each empty document it locates the page in the dump, expands its
templates with the real pipeline, and then measures how much prose the
page has outside of tables. The answer is one of:

  NO_PROSE            Nothing outside the tables but categories, HTML
                      tags and link markup. The empty document is
                      correct and needs no attention.
  PROSE_LOST          Letters outside the tables, in quantity, and the
                      document is still empty. Something consumed text
                      it should have kept. These are the ones to look
                      at.
  DROPNESTED_DISAGREES
                      A flag, reported alongside the verdict rather
                      than instead of it. See below.
  EXTRACTION_ERROR    The page raised while being extracted.
  NOT_FOUND_IN_DUMP   The document's page id never appeared in the
                      dump -- wrong dump, or an id mismatch.

Every PROSE_LOST page also gets a cause, naming the markup its prose
sits in, since most of them turn out to be deliberate behavior rather
than loss:

  LIST_ONLY             Lists and indents, which compact() drops unless
                        --html is on. An index or a list article has
                        nothing else in it.
  HEADINGS_ONLY         Section titles whose sections are empty.
  UNRECOGNIZED_REDIRECT A leading redirect line that was not recognized
                        as one, so the page was extracted as an article.
  DISCARDED_ELEMENT     Text inside a gallery, ref, imagemap or another
                        element clean() removes wholesale.
  MARKUP_ONLY           Prose by the letter count, but nothing in any
                        bucket -- worth a look at the preview.
  UNEXPLAINED           Ordinary lines, in quantity, that compact()
                        keeps and this page does not have. These are
                        the ones to read, and they sort to the top of
                        the report.

Two table strippers run on the expanded text: the extractor's own
dropNested(), and an independent scanner in this file. Measuring the
prose with dropNested() alone would hide exactly the failure this tool
exists to find, since a bug there makes the page look prose-free to any
check built on top of it. When the two disagree by more than a few
characters the page is flagged DROPNESTED_DISAGREES and both numbers
are reported, which localizes the loss to table handling without
assuming either stripper is the correct one.

"Prose" is counted as letters, so it works the same on Shahmukhi,
Devanagari and Latin text. Markup that produces no output -- namespace
links such as [[Category:...]] or [[File:...]], HTML tags, comments,
and unexpanded template calls -- is removed before counting, so a page
consisting only of those counts as zero.

Usage:
    python3 audit_empty_docs.py \
        --extracted /path/to/output_dir_or_file \
        --dump      /path/to/xxwiki-latest-pages-articles.xml.bz2 \
        [--templates /path/to/templates_file] \
        [--prose-threshold 40] \
        [--limit 200] \
        [--csv report.csv] \
        [--show 10]

--extracted accepts a single file or a directory (searched
recursively); files may be plain text, .bz2, or .gz, and either the
classic <doc id="..." url="..." title="...">...</doc> format or
WikiExtractor's --json line format.

Templates come from the dump itself unless --templates names a file or
--no-templates is given. Without templates, a page whose prose arrives
through a template call looks empty, so the NO_PROSE verdict
understates content; the tool says so in its summary when it runs that
way.
"""

import argparse
import bz2
import csv as csv_module
import gzip
import json
import os
import re
import sys
import unicodedata

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        """Minimal fallback with just enough surface for how this script
        uses it: a context manager with .update() for byte-based
        progress and .set_postfix() for the running found-count."""
        def __init__(self, total=None, desc=None, unit=None, unit_scale=None,
                     unit_divisor=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def update(self, n=1):
            pass

        def set_postfix(self, **kwargs):
            pass


DOC_OPEN_RE = re.compile(r'<doc\s+id="(\d+)"[^>]*?title="([^"]*)"[^>]*>')
DOC_CLOSE_RE = re.compile(r'</doc>')

PAGE_ID_RE = re.compile(r'<id>(\d+)</id>')
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
TEXT_OPEN_RE = re.compile(r'<text[^>]*>')
TEXT_CLOSE_RE = re.compile(r'</text>')
REDIRECT_TAG_RE = re.compile(r'<redirect\b')

COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
HTML_TAG_RE = re.compile(r'<[^>]{0,200}>')
# A link whose target carries a namespace prefix -- Category, File,
# Image, their localized spellings, and interwiki links. These produce
# no body text, so they must not count as prose. An ordinary [[link]]
# has no colon in its target and is left alone.
NS_LINK_RE = re.compile(r'\[\[[^\[\]|]{1,80}:[^\[\]]{0,400}\]\]')

TABLE_OPEN = '{|'
TABLE_CLOSE = '|}'

# Elements whose body is removed wholesale by clean(). Built from the
# extractor's own list so the two can't drift apart. Text inside one of
# these was never going to be output, so it must not be counted as
# prose the page lost.
DISCARD_RE = None

HEADING_RE = re.compile(r'^\s*=={1,5}.*==\s*$')
# A redirect line is '#' followed straight away by a word -- '#REDIRECT',
# '#مڑجوڑ'. A list item puts a space there, or nothing at all.
REDIRECT_LINE_RE = re.compile(r'^#\s*[^\s#*:;][^\s]*')


class _ProgressReader:
    """Wraps a raw binary file object, calling pbar.update() with the
    number of bytes actually read off of it. Handed to bz2.open()/
    gzip.open() as their `filename` argument (both accept a file object
    in place of a path) so the progress bar reflects bytes consumed
    from disk regardless of the decompressed iteration on top."""

    def __init__(self, raw, pbar):
        self._raw = raw
        self._pbar = pbar

    def read(self, *args, **kwargs):
        data = self._raw.read(*args, **kwargs)
        self._pbar.update(len(data))
        return data

    def close(self):
        self._raw.close()

    def __getattr__(self, name):
        return getattr(self._raw, name)


def open_any(path, mode='rt'):
    """Open a plain, .bz2, or .gz file transparently, as UTF-8 text."""
    if path.endswith('.bz2'):
        return bz2.open(path, mode, encoding='utf-8', errors='replace')
    if path.endswith('.gz'):
        return gzip.open(path, mode, encoding='utf-8', errors='replace')
    return open(path, mode, encoding='utf-8', errors='replace')


def open_dump_with_progress(path, pbar):
    """Like open_any(), but reads go through _ProgressReader first so
    `pbar` advances by bytes actually consumed from the on-disk
    (possibly compressed) file."""
    raw = open(path, 'rb')
    wrapped = _ProgressReader(raw, pbar)
    if path.endswith('.bz2'):
        return bz2.open(wrapped, 'rt', encoding='utf-8', errors='replace')
    if path.endswith('.gz'):
        return gzip.open(wrapped, 'rt', encoding='utf-8', errors='replace')
    import io
    return io.TextIOWrapper(wrapped, encoding='utf-8', errors='replace')


def iter_files(path):
    """Yield file paths under `path` (or just `path` itself if it's a file)."""
    if os.path.isfile(path):
        yield path
        return
    for root, _dirs, files in sorted(os.walk(path)):
        for name in sorted(files):
            yield os.path.join(root, name)


def letters(text):
    """Count characters that are letters in any script, which is what
    distinguishes real body text from markup left behind by a strip."""
    return sum(1 for c in text if unicodedata.category(c).startswith('L'))


def build_discard_re(ex_module):
    """One alternation over the extractor's discardElements, so a
    page's galleries, refs and imagemaps can be set aside in a single
    pass."""
    tags = '|'.join(re.escape(t) for t in ex_module.discardElements)
    return re.compile(r'<\s*(%s)\b[^>]*>.*?<\s*/\s*\1\s*>' % tags,
                      re.IGNORECASE | re.DOTALL)


def split_discarded(text):
    """Return (text without discarded elements, the removed parts)."""
    if DISCARD_RE is None:
        return text, ''
    taken = []

    def take(m):
        taken.append(m.group(0))
        return ' '
    return DISCARD_RE.sub(take, text), ''.join(taken)


class MissRecorder:
    """A read-only view over the templates mapping that counts the
    lookups that miss.

    expandTemplate() answers a call for a template it does not have
    with the empty string, which is indistinguishable in the output
    from a template that legitimately renders to nothing. Wrapping the
    mapping catches the miss at the one place the decision is made --
    after redirect resolution and title normalization -- without
    restating any of that logic here.
    """

    def __init__(self, base):
        self._base = base
        self.misses = {}

    def start_page(self):
        self.misses = {}

    def __contains__(self, title):
        if title in self._base:
            return True
        self.misses[title] = self.misses.get(title, 0) + 1
        return False

    def __getitem__(self, title):
        return self._base[title]

    def get(self, title, default=None):
        return self._base.get(title, default)

    def __len__(self):
        return len(self._base)


def strip_nonprose(text, drop_templates=True):
    """Remove the markup that produces no body text: comments, HTML
    tags, namespace links, and template calls that never expanded.

    drop_templates is False when the caller has already separated the
    template calls out and is measuring one side of that split.
    """
    text = COMMENT_RE.sub(' ', text)
    text, _discarded = split_discarded(text)
    if drop_templates:
        text, _taken = split_template_calls(text)
    text = NS_LINK_RE.sub(' ', text)
    return HTML_TAG_RE.sub(' ', text)


def strip_tables(text):
    """Remove {| ... |} regions, nesting included, and return what is
    left over.

    This is deliberately a second implementation rather than a call to
    the extractor's dropNested(): the point of running both is to catch
    the case where the extractor's table handling is what emptied the
    page, which a check built on dropNested() could not see.

    A group that never closes is removed from its opening delimiter to
    the end of the text, which is the most content this can justifiably
    remove and so keeps the tool from reporting prose that a reasonable
    extractor would have dropped anyway. Groups already closed before
    it are unaffected.
    """
    out = []
    pos = 0
    depth = 0
    start = 0
    while pos < len(text):
        nxt_open = text.find(TABLE_OPEN, pos)
        nxt_close = text.find(TABLE_CLOSE, pos)
        if nxt_open == -1 and nxt_close == -1:
            break
        if nxt_close == -1 or (nxt_open != -1 and nxt_open < nxt_close):
            if depth == 0:
                out.append(text[start:nxt_open])
                start = nxt_open
            depth += 1
            pos = nxt_open + len(TABLE_OPEN)
        else:
            if depth > 0:
                depth -= 1
                if depth == 0:
                    start = nxt_close + len(TABLE_CLOSE)
            pos = nxt_close + len(TABLE_CLOSE)
    if depth == 0:
        out.append(text[start:])
    # depth > 0: an unclosed group runs to the end, so nothing more is kept.
    return ''.join(out)


def split_template_calls(text):
    """Return (text with balanced {{...}} regions removed, the removed
    regions joined).

    A template call still present after expansion never produced any
    output, so its letters are not content the page lost. Counting them
    as prose is how a page of unexpanded infobox markup comes to look
    like an article that went missing.
    """
    kept = []
    taken = []
    pos = 0
    depth = 0
    start = 0
    while pos < len(text):
        o = text.find('{{', pos)
        c = text.find('}}', pos)
        if o == -1 and c == -1:
            break
        if c == -1 or (o != -1 and o < c):
            if depth == 0:
                kept.append(text[start:o])
                start = o
            depth += 1
            pos = o + 2
        else:
            if depth > 0:
                depth -= 1
                if depth == 0:
                    taken.append(text[start:c + 2])
                    start = c + 2
            pos = c + 2
    if depth == 0:
        kept.append(text[start:])
    else:
        taken.append(text[start:])
    return ''.join(kept), ''.join(taken)


def attribute_prose(text):
    """Split the prose outside a page's tables into buckets by the
    markup that holds it, so an empty document can be explained rather
    than only flagged.

    Returns letters per bucket: 'template' for calls that never
    expanded, 'redirect' for a leading redirect line, 'heading' for
    section titles, 'list' for list and indent items, and 'other' for
    everything left -- which is the bucket worth a person's time, since
    compact() keeps ordinary lines and drops the rest.
    """
    text = COMMENT_RE.sub(' ', text)
    text, discarded = split_discarded(text)
    body, templates = split_template_calls(text)
    buckets = {'template': letters(strip_nonprose(templates,
                                                  drop_templates=False)),
               'discarded': letters(HTML_TAG_RE.sub(' ', discarded)),
               'redirect': 0, 'heading': 0, 'list': 0, 'other': 0}
    first = True
    for line in body.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        n = letters(strip_nonprose(stripped))
        if not n:
            continue
        if first and REDIRECT_LINE_RE.match(stripped):
            buckets['redirect'] += n
        elif HEADING_RE.match(stripped):
            buckets['heading'] += n
        elif stripped[0] in '*#;:':
            buckets['list'] += n
        else:
            buckets['other'] += n
        first = False
    return buckets


def collect_empty_docs(path, verbose_label='extracted'):
    """Scan an extractor-output tree and return [(id, title)] for every
    document whose body is empty -- that is, holds nothing beyond the
    title line compact() writes at the top.

    Supports both the classic <doc ...> format and --json line format.
    """
    empties = []
    total = 0
    file_count = 0
    for fpath in iter_files(path):
        file_count += 1
        try:
            with open_any(fpath) as f:
                doc_id = title = None
                body = []
                for line in f:
                    if doc_id is None:
                        stripped = line.lstrip()
                        if stripped.startswith('{'):
                            try:
                                obj = json.loads(line)
                            except (ValueError, json.JSONDecodeError):
                                continue
                            if 'id' not in obj:
                                continue
                            total += 1
                            jtitle = obj.get('title', '')
                            jtext = (obj.get('text') or '').strip()
                            if jtext in ('', jtitle.strip()):
                                empties.append((str(obj['id']), jtitle))
                            continue
                        m = DOC_OPEN_RE.search(line)
                        if m:
                            doc_id, title = m.group(1), m.group(2)
                            body = []
                        continue
                    if DOC_CLOSE_RE.search(line):
                        total += 1
                        text = ''.join(body).strip()
                        if text in ('', title.strip()):
                            empties.append((doc_id, title))
                        doc_id = title = None
                        continue
                    body.append(line)
        except (OSError, UnicodeDecodeError) as e:
            print('  warning: could not read %s: %s' % (fpath, e), file=sys.stderr)
    if file_count == 0:
        raise SystemExit(
            'error: --%s path %r matched no files to scan (nonexistent path, '
            'empty directory, or a typo?) -- refusing to silently treat this '
            'as "0 documents"' % (verbose_label, path))
    print('[%s] scanned %d file(s): %d documents, %d empty'
          % (verbose_label, file_count, total, len(empties)), file=sys.stderr)
    return empties, total


def load_dump_templates(dump_path, templates_path=None):
    """Return (templates, redirects, prefix) for the expansion pass.

    Templates come from a dedicated file when one is given, otherwise
    from a full pass over the dump, which is what a real extraction
    does.
    """
    import wikiextractor.WikiExtractor as we

    source = templates_path or dump_path
    templates, redirects = {}, {}
    size = os.path.getsize(source)
    with tqdm(total=size, desc='loading templates', unit='B',
              unit_scale=True, unit_divisor=1024) as pbar:
        with open_dump_with_progress(source, pbar) as f:
            count, prefix = we.load_templates(f, templates=templates,
                                              redirects=redirects)
    print('loaded %d templates (prefix %r)' % (count, prefix), file=sys.stderr)
    return templates, redirects, prefix


def measure(page_id, title, text, ex_module, templates, redirects, prefix,
            prose_threshold):
    """Expand one page and report how much prose survives outside its
    tables, by both strippers."""
    result = {
        'id': page_id,
        'title': title,
        'source_len': len(text),
        'expanded_len': 0,
        'prose_ref': 0,
        'prose_dropnested': 0,
        'table_opens': 0,
        'table_closes': 0,
        'final_len': None,
        'prose_other': 0,
        'prose_list': 0,
        'prose_heading': 0,
        'prose_template': 0,
        'prose_redirect': 0,
        'prose_discarded': 0,
        'missing_templates': 0,
        'missing_names': '',
        'preview': '',
        'error': '',
    }
    try:
        if isinstance(templates, MissRecorder):
            templates.start_page()
        extractor = ex_module.Extractor(
            page_id, page_id, 'https://example.org/wiki?curid=',
            title, text.split('\n'), templates=templates, redirects=redirects,
            templatePrefix=prefix)
        expanded = extractor.expandTemplates(text)
        result['expanded_len'] = len(expanded)
        result['table_opens'] = expanded.count(TABLE_OPEN)
        result['table_closes'] = expanded.count(TABLE_CLOSE)

        outside = strip_tables(expanded)
        ref = strip_nonprose(outside)
        result['prose_ref'] = letters(ref)

        dn = strip_nonprose(ex_module.dropNested(expanded, r'{\|', r'\|}'))
        result['prose_dropnested'] = letters(dn)

        for bucket, n in attribute_prose(outside).items():
            result['prose_' + bucket] = n

        final = extractor.clean_text(text)
        result['final_len'] = sum(len(t) for t in final)

        if isinstance(templates, MissRecorder):
            misses = templates.misses
            result['missing_templates'] = sum(misses.values())
            result['missing_names'] = ' | '.join(
                name for name, _n in sorted(misses.items(),
                                            key=lambda kv: -kv[1])[:5])

        preview = ' '.join(ref.split())
        result['preview'] = preview[:120]
    except Exception as e:  # a bad page shouldn't abort the audit
        result['error'] = '%s: %s' % (type(e).__name__, e)
    return result


def classify(info, prose_threshold):
    """Verdict plus any flags, as (verdict, [flag, ...])."""
    if info is None:
        return 'NOT_FOUND_IN_DUMP', []
    if info['error']:
        return 'EXTRACTION_ERROR', []
    flags = []
    # The two strippers differ a little by construction, since they
    # resolve an unclosed group's extent differently, so the flag is
    # for a disagreement large enough to mean one of them lost a
    # section rather than a tail: a tenth of the larger measurement,
    # floored so that a tiny page can't trip it on a few characters.
    biggest = max(info['prose_ref'], info['prose_dropnested'])
    if abs(info['prose_ref'] - info['prose_dropnested']) > max(
            prose_threshold, biggest // 10):
        flags.append('DROPNESTED_DISAGREES')
    if info['table_opens'] != info['table_closes']:
        flags.append('UNBALANCED_TABLES')
    if info['prose_template'] >= prose_threshold:
        flags.append('UNEXPANDED_TEMPLATE')
    if info['missing_templates']:
        flags.append('MISSING_TEMPLATE')
    if info['final_len']:
        # The document was empty in the output being audited but this
        # tree extracts it to something, which is what a fix looks like.
        flags.append('NOW_NONEMPTY')
    if max(info['prose_ref'], info['prose_dropnested']) >= prose_threshold:
        return 'PROSE_LOST', flags
    return 'NO_PROSE', flags


def cause_of(info, prose_threshold):
    """Name what holds a lost page's prose. UNEXPLAINED comes first
    whenever ordinary lines account for enough of it, because those are
    the pages compact() should have kept and did not; the remaining
    names each point at a known, deliberate behavior."""
    if info is None or info['error']:
        return ''
    if info['prose_other'] >= prose_threshold:
        return 'UNEXPLAINED'
    buckets = {k: info['prose_' + k]
               for k in ('list', 'heading', 'redirect', 'discarded')}
    best = max(buckets, key=lambda k: buckets[k])
    if not buckets[best]:
        return 'MARKUP_ONLY'
    return {'list': 'LIST_ONLY', 'heading': 'HEADINGS_ONLY',
            'redirect': 'UNRECOGNIZED_REDIRECT',
            'discarded': 'DISCARDED_ELEMENT'}[best]


def scan_dump(dump_path, wanted, ex_module, templates, redirects, prefix,
              prose_threshold):
    """Stream the dump, measuring each page whose id is wanted."""
    remaining = set(wanted)
    titles = dict(wanted.items()) if isinstance(wanted, dict) else {}
    results = {}

    in_page = False
    page_lines = []
    page_id = None
    seen_first_id = False

    size = os.path.getsize(dump_path)
    with tqdm(total=size, desc='scanning dump', unit='B', unit_scale=True,
              unit_divisor=1024) as pbar:
        pbar.set_postfix(found='0/%d' % len(remaining))
        with open_dump_with_progress(dump_path, pbar) as f:
            for line in f:
                if not in_page:
                    if '<page>' in line:
                        in_page = True
                        page_lines = [line]
                        page_id = None
                        seen_first_id = False
                    continue
                page_lines.append(line)
                if not seen_first_id:
                    m = PAGE_ID_RE.search(line)
                    if m:
                        page_id = m.group(1)
                        seen_first_id = True
                if '</page>' in line:
                    in_page = False
                    if page_id in remaining:
                        block = ''.join(page_lines)
                        title_m = TITLE_RE.search(block)
                        title = titles.get(page_id) or (
                            title_m.group(1) if title_m else '')
                        text = ''
                        open_m = TEXT_OPEN_RE.search(block)
                        if open_m:
                            close_m = TEXT_CLOSE_RE.search(block, open_m.end())
                            end = close_m.start() if close_m else len(block)
                            import html as html_mod
                            text = html_mod.unescape(block[open_m.end():end])
                        results[page_id] = measure(
                            page_id, title, text, ex_module, templates,
                            redirects, prefix, prose_threshold)
                        results[page_id]['is_redirect'] = bool(
                            REDIRECT_TAG_RE.search(block))
                        remaining.discard(page_id)
                        pbar.set_postfix(
                            found='%d/%d' % (len(results),
                                             len(results) + len(remaining)))
                    page_lines = []
                    if not remaining:
                        break
    return results, remaining


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--extracted', required=True,
                    help='extractor output to audit (file or dir)')
    ap.add_argument('--dump', required=True,
                    help='original XML dump (.xml, .bz2, or .gz)')
    ap.add_argument('--templates',
                    help='templates file to expand with, in place of a full '
                         'pass over the dump')
    ap.add_argument('--no-templates', action='store_true',
                    help='skip template expansion entirely. Much faster, but '
                         'a page whose prose arrives through a template call '
                         'then looks empty, so NO_PROSE understates content')
    ap.add_argument('--prose-threshold', type=int, default=40,
                    help='letters outside the tables above which a page counts '
                         'as having prose (default: 40)')
    ap.add_argument('--limit', type=int,
                    help='audit only the first N empty documents')
    ap.add_argument('--show', type=int, default=20,
                    help='how many PROSE_LOST pages to print in full '
                         '(default: 20)')
    ap.add_argument('--csv', help='optional path to write a CSV report')
    args = ap.parse_args()

    try:
        import wikiextractor.extract as ex_module
    except ImportError:
        # Running the script by path puts tools/ on sys.path rather than
        # the repository root, so fall back to the tree this file lives
        # in before giving up. An explicit PYTHONPATH still wins, which
        # is how a different tree gets audited.
        sys.path.append(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        try:
            import wikiextractor.extract as ex_module
        except ImportError as e:
            raise SystemExit(
                'error: wikiextractor is not importable (%s). Run this from '
                'the repository root, or put it on PYTHONPATH -- this tool '
                'measures with the real expansion pipeline and has nothing '
                'to fall back on.' % e)
    print('measuring with %s' % os.path.dirname(ex_module.__file__),
          file=sys.stderr)
    global DISCARD_RE
    DISCARD_RE = build_discard_re(ex_module)

    empties, total_docs = collect_empty_docs(args.extracted)
    if not empties:
        print('No empty documents -- nothing to audit.')
        return
    if args.limit:
        empties = empties[:args.limit]
        print('auditing the first %d' % len(empties), file=sys.stderr)

    if args.no_templates:
        templates, redirects, prefix = {}, {}, ''
    else:
        templates, redirects, prefix = load_dump_templates(
            args.dump, args.templates)

    # Wrapped so that a call to a template the dump does not define --
    # which expandTemplate() answers with the empty string, leaving no
    # trace in the output -- is counted rather than inferred.
    templates = MissRecorder(templates)

    wanted = dict(empties)
    results, not_found = scan_dump(args.dump, wanted, ex_module, templates,
                                   redirects, prefix, args.prose_threshold)

    rows = []
    counts = {}
    flag_counts = {}
    cause_counts = {}
    for page_id, title in empties:
        info = results.get(page_id)
        verdict, flags = classify(info, args.prose_threshold)
        cause = cause_of(info, args.prose_threshold) if verdict == 'PROSE_LOST' else ''
        counts[verdict] = counts.get(verdict, 0) + 1
        for fl in flags:
            flag_counts[fl] = flag_counts.get(fl, 0) + 1
        if cause:
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
        row = {'id': page_id, 'title': title, 'verdict': verdict,
               'cause': cause, 'flags': ' '.join(flags)}
        row.update({k: ('' if info is None else info.get(k, ''))
                    for k in ('source_len', 'expanded_len', 'prose_ref',
                              'prose_dropnested', 'prose_other', 'prose_list',
                              'prose_heading', 'prose_template',
                              'prose_redirect', 'prose_discarded',
                              'missing_templates', 'missing_names',
                              'table_opens',
                              'table_closes', 'final_len', 'is_redirect',
                              'preview', 'error')})
        rows.append(row)

    interesting = [r for r in rows
                   if r['verdict'] in ('PROSE_LOST', 'EXTRACTION_ERROR',
                                       'NOT_FOUND_IN_DUMP') or r['flags']]
    # Unexplained pages first, then by how much ordinary text they hold:
    # the printed head of the report should be the reading order.
    interesting.sort(key=lambda r: (r['cause'] != 'UNEXPLAINED',
                                    -(r['prose_other'] or 0),
                                    -(r['prose_ref'] or 0)))

    print('=' * 100)
    if not interesting:
        print('Every empty document has no prose outside its tables.')
    for r in interesting[:args.show]:
        print('[%-17s] %-21s id=%-9s %s'
              % (r['verdict'], r['cause'], r['id'], r['title']))
        if r['error']:
            print('                     %s' % r['error'])
            continue
        print('                     source %d -> expanded %d; prose outside '
              'tables: %d (reference) / %d (dropNested); {| x%d |} x%d'
              % (r['source_len'], r['expanded_len'], r['prose_ref'],
                 r['prose_dropnested'], r['table_opens'], r['table_closes']))
        print('                     prose by markup: other %d, list %d, '
              'heading %d, discarded element %d, unexpanded template %d, '
              'redirect line %d'
              % (r['prose_other'], r['prose_list'], r['prose_heading'],
                 r['prose_discarded'], r['prose_template'],
                 r['prose_redirect']))
        if r['missing_templates']:
            print('                     %d call(s) to templates not in the '
                  'dump: %s' % (r['missing_templates'], r['missing_names']))
        if r['flags']:
            print('                     flags: %s' % r['flags'])
        if r['preview']:
            print('                     %s' % r['preview'])
    if len(interesting) > args.show:
        print('... and %d more (see --csv for the full list)'
              % (len(interesting) - args.show))
    print('=' * 100)
    print('%d empty of %d documents' % (len(empties), total_docs))
    print('Verdicts: %s' % ', '.join('%s=%d' % kv for kv in sorted(counts.items())))
    if cause_counts:
        print('Causes:   %s' % ', '.join(
            '%s=%d' % kv for kv in sorted(cause_counts.items(),
                                          key=lambda kv: -kv[1])))
    if flag_counts:
        print('Flags:    %s' % ', '.join('%s=%d' % kv
                                         for kv in sorted(flag_counts.items())))
    missing_pages = sum(1 for r in rows if r['missing_templates'])
    if missing_pages:
        per_name = {}
        for r in rows:
            for name in (r['missing_names'] or '').split(' | '):
                if name:
                    per_name[name] = per_name.get(name, 0) + 1
        top = sorted(per_name.items(), key=lambda kv: -kv[1])[:10]
        print('Templates not in the dump: %d of these pages call at least one'
              % missing_pages)
        for name, n in top:
            print('    %-50s %d page(s)' % (name[:50], n))
    if not_found:
        print('%d id(s) never appeared in the dump' % len(not_found))
    if args.no_templates:
        print('NOTE: run without template expansion, so NO_PROSE understates '
              'any page whose prose arrives through a template.')

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv_module.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print('\nWrote CSV report to %s' % args.csv, file=sys.stderr)


if __name__ == '__main__':
    main()
