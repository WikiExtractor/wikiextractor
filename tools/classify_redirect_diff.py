#!/usr/bin/env python3
"""
classify_redirect_diff.py

Given full <doc>-formatted extraction outputs from before/after some
change to extract.py (originally built for the localized-redirect-
keyword fix, but the approach generalizes to any change expected to
resolve specific, identifiable artifacts), the templates file, and
the ORIGINAL source dump those documents were extracted from,
classifies each changed document by actually confirming what caused
the change -- not by pattern-matching the diff text alone.

Why this version looks different from an earlier one: that version
diffed the whole before/after files as one pair of giant line lists,
handed directly to difflib.SequenceMatcher.get_opcodes(). Confirmed
directly that this can be dramatically slower than it looks like it
should be -- get_opcodes() has no way to show progress mid-call, and
on a real, large dump this can run for many minutes where command-
line `diff` finishes in seconds, since SequenceMatcher's matching
algorithm doesn't have the same practical performance
characteristics GNU diff's does at this scale.

Two real fixes, not just a progress bar papering over the slowness:
  1. Split both files by <doc id="...">...</doc> boundaries first,
     and fast-path any document whose text is byte-identical between
     before and after (a plain string comparison, not a diff at all)
     -- skipping the vast majority of documents entirely, since only
     a small fraction are typically affected by any given fix. Only
     documents that actually differ get a real (and now much smaller,
     per-document rather than whole-file) diff computed at all.
  2. For each document that DOES differ (or appears/disappears
     entirely), look up its actual source wikitext in --source and
     re-run the real, instrumented extract.py machinery on it
     directly -- checking whether a redirect using one of the
     newly-recognized keywords was actually invoked during THIS
     document's own extraction, rather than only checking "does some
     template somewhere in --templates have this shape" (which an
     earlier version of this script did, and which can't tell two
     differently-named templates using the same keyword apart, or
     confirm a given template was actually reached by this specific
     article at all).

Usage:
    python3 classify_redirect_diff.py --before before.txt --after after.txt \
        --templates templates.xml --source dump.xml.bz2

Notes:
    - Requires wikiextractor's extract.py to be importable (e.g. on
      $PYTHONPATH already).
    - --templates should be in the same <page>/<title>/<text> shape
      load_templates()'s own --output_file produces.
    - --source is the original page dump those documents were
      extracted from (.xml or .xml.bz2) -- needed to actually re-run
      extraction for causal confirmation, not just to read text from.
"""

import argparse
import bz2
import difflib
import re
import sys

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        """Minimal fallback supporting both usage patterns this script
        needs: wrapping an iterable directly, and being used as a
        context manager with .update() calls for byte-based progress.
        """
        def __init__(self, iterable=None, total=None, desc=None, unit=None, unit_scale=None):
            self.iterable = iterable

        def __iter__(self):
            return iter(self.iterable) if self.iterable is not None else iter([])

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def update(self, n=1):
            pass


def open_maybe_bz2_read(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')


def parse_docs(path):
    """Returns {doc_id: full_doc_text} by splitting on
    <doc id="...">...</doc> boundaries, streaming line-by-line rather
    than holding the whole file as one string plus one big regex scan.
    """
    doc_id_re = re.compile(r'<doc id="(\d+)"')
    result = {}
    current_id = None
    current_lines = None

    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if current_id is None:
                m = doc_id_re.search(line)
                if m:
                    current_id = m.group(1)
                    current_lines = [line]
                continue
            current_lines.append(line)
            if line.strip() == '</doc>':
                result[current_id] = ''.join(current_lines)
                current_id = None
                current_lines = None
    return result


def get_source_wikitext_by_id(source_path, wanted_ids):
    """Returns {id: wikitext} for whichever of wanted_ids are found in
    the original source dump.

    Handles both real forms a <text> element can take: the normal
    <text ...>content</text> pair, and the self-closing
    <text bytes="0" ... /> form MediaWiki's own exporter writes for a
    revision with zero bytes of content -- confirmed directly this
    second form exists in real data and was previously silently
    unmatched entirely (no closing </text> to find at all), causing
    the id to be discarded from the search without ever being added
    to the result, indistinguishable from "not present in the dump"
    even though the page genuinely is there, just with empty text.
    """
    still_needed = set(wanted_ids)
    result = {}
    with open_maybe_bz2_read(source_path) as f:
        in_page = False
        page_lines = []
        for line in f:
            stripped = line.strip()
            if not in_page and stripped == '<page>':
                in_page = True
                page_lines = [line]
                continue
            if in_page:
                page_lines.append(line)
                if stripped == '</page>':
                    in_page = False
                    if still_needed:
                        text = ''.join(page_lines)
                        id_m = re.search(r'<id>(\d+)</id>', text)
                        if id_m and id_m.group(1) in still_needed:
                            if re.search(r'<text\b[^>]*/>', text):
                                result[id_m.group(1)] = ''
                            else:
                                text_m = re.search(r'<text[^>]*>(.*?)</text>', text, re.DOTALL)
                                if text_m:
                                    result[id_m.group(1)] = text_m.group(1)
                            still_needed.discard(id_m.group(1))
                    page_lines = []
                    if not still_needed:
                        break
    return result


class RecordingDict(dict):
    """Behaves exactly like a normal dict, except every key checked
    via `in` OR `.get()` gets passed to on_lookup first -- used in
    place of extract.py's real templates/templateCache/redirects
    globals so every title expandTemplate() tries to resolve gets
    recorded, without altering the real, already-loaded data (this
    wraps a COPY of it; the caller restores the original objects
    afterward). Both hooks matter: templates/templateCache are
    checked via `in`, but redirects specifically is checked via
    `.get()` -- and critically, redirects.get(title) is what sees the
    ORIGINAL, requested title; templates/templateCache only ever see
    whatever title redirects.get() resolved it to, once the
    resolution has already replaced it. Missing the .get() hook here
    would silently miss every redirect actually taken, seeing only
    its target."""
    def __init__(self, source_dict, on_lookup):
        super().__init__(source_dict)
        self._on_lookup = on_lookup

    def __contains__(self, key):
        self._on_lookup(key)
        return super().__contains__(key)

    def get(self, key, default=None):
        self._on_lookup(key)
        return super().get(key, default)


def find_redirect_templates_invoked(ex, page_id, wikitext, non_english_redirect_titles):
    """
    Runs the real clean_text() on wikitext, instrumented to record
    every template title looked up, then returns the subset of those
    that are known (from --templates) to be redirects using one of
    the newly-recognized, non-English keywords -- genuine, per-article
    confirmation, not a file-wide pattern match.
    """
    looked_up = set()
    original_templates = ex.templates
    original_cache = ex.templateCache
    original_redirects = ex.redirects
    try:
        ex.templates = RecordingDict(ex.templates, looked_up.add)
        ex.templateCache = RecordingDict(ex.templateCache, looked_up.add)
        ex.redirects = RecordingDict(ex.redirects, looked_up.add)
        extractor = ex.Extractor(page_id, page_id, f"https://example.org/wiki?curid={page_id}",
                                  f"Page{page_id}", [wikitext])
        try:
            extractor.clean_text(wikitext, expand_templates=True)
        except Exception:
            pass  # a page that can't finish expanding still reveals what it looked up first
    finally:
        ex.templates = original_templates
        ex.templateCache = original_cache
        ex.redirects = original_redirects
    return looked_up & non_english_redirect_titles


def find_non_english_redirect_titles(templates_path, redirect_keywords):
    """
    Returns the set of template titles in templates_path whose own
    raw text starts with one of redirect_keywords -- these are the
    templates whose CORRECT resolution as a redirect is new behavior
    (English "#REDIRECT" was already handled before any such fix).

    Streams line-by-line, properly extracting each <text>...</text>
    element's actual content before pattern-matching against it --
    checking a raw line still carrying its <text> opening tag (e.g.
    "<text>#چوريو [[Target]]") would silently fail to match at all,
    since the pattern only checks from the true start of the content.
    """
    pattern = re.compile(r'#(?:%s)\b' % '|'.join(re.escape(k) for k in redirect_keywords),
                          re.IGNORECASE)
    result = set()
    current_title = None
    current_text_lines = None
    in_text = False

    with open(templates_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            stripped = line.strip()
            title_m = re.match(r'<title>(.*)</title>', stripped)
            if title_m:
                current_title = title_m.group(1)
                continue
            if stripped.startswith('<text'):
                in_text = True
                current_text_lines = []
                after_tag = re.sub(r'^\s*<text[^>]*>', '', line, count=1)
                if '</text>' in after_tag:
                    current_text_lines.append(after_tag.split('</text>', 1)[0])
                    in_text = False
                    _check_and_record(current_title, current_text_lines, pattern, result)
                else:
                    current_text_lines.append(after_tag)
                continue
            if in_text:
                if '</text>' in line:
                    current_text_lines.append(line.split('</text>', 1)[0])
                    in_text = False
                    _check_and_record(current_title, current_text_lines, pattern, result)
                else:
                    current_text_lines.append(line)
    return result


def _check_and_record(title, text_lines, pattern, result_set):
    if title is not None and pattern.match(''.join(text_lines).strip()):
        result_set.add(title)


def refine_word_diff(old_text, new_text):
    """Word-level diff within a single (already small, per-document)
    text pair, so a short, localized change is reported as itself
    rather than as if the whole document were rewritten."""
    old_words = old_text.split(' ')
    new_words = new_text.split(' ')
    sm = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        removed.extend(old_words[i1:i2])
        added.extend(new_words[j1:j2])
    return ' '.join(removed), ' '.join(added)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--before', required=True, help='<doc>-formatted extraction output before the change')
    ap.add_argument('--after', required=True, help='<doc>-formatted extraction output after the change')
    ap.add_argument('--templates', required=True, help='Templates file (<page>/<title>/<text> shape)')
    ap.add_argument('--source', required=True,
                     help='Original page dump (.xml or .xml.bz2) the documents were extracted from')
    args = ap.parse_args()

    import wikiextractor.extract as ex

    keywords_to_check = [k for k in ex.redirectKeywords if k.upper() != 'REDIRECT']
    if not keywords_to_check:
        print("No non-English redirect keywords configured -- nothing to check.", file=sys.stderr)
        sys.exit(1)

    print("Parsing before/after into per-document text...", file=sys.stderr)
    before_docs = parse_docs(args.before)
    after_docs = parse_docs(args.after)
    print(f"{len(before_docs)} document(s) before, {len(after_docs)} after.", file=sys.stderr)

    all_ids = set(before_docs) | set(after_docs)
    changed_ids = [doc_id for doc_id in all_ids
                   if before_docs.get(doc_id) != after_docs.get(doc_id)]
    print(f"{len(all_ids) - len(changed_ids)} document(s) identical, skipped via a direct "
          f"string comparison. {len(changed_ids)} document(s) actually changed (including any "
          f"that appeared or disappeared entirely) -- diffing and confirming only those.",
          file=sys.stderr)

    if not changed_ids:
        print("No changes at all.")
        return

    print("Loading templates and identifying which use a non-English redirect keyword...",
          file=sys.stderr)
    non_english_redirect_titles = find_non_english_redirect_titles(args.templates, keywords_to_check)
    print(f"{len(non_english_redirect_titles)} such template(s) found in --templates.",
          file=sys.stderr)

    print("Loading templates into the real extract.py machinery...", file=sys.stderr)
    with open(args.templates, encoding='utf-8', errors='replace') as f:
        import wikiextractor.WikiExtractor as we
        we.load_templates(f)

    source_texts = get_source_wikitext_by_id(args.source, changed_ids)
    missing_from_source = [doc_id for doc_id in changed_ids if doc_id not in source_texts]
    print(f"{len(source_texts)} of {len(changed_ids)} changed document id(s) found in --source.",
          file=sys.stderr)
    if missing_from_source:
        print(f"WARNING: {len(missing_from_source)} id(s) NOT found in --source: "
              f"{', '.join(missing_from_source)}. If you've directly confirmed these ids ARE "
              f"present in your actual dump (e.g. via extract_page_range.py), double-check "
              f"--source is pointed at that exact file, and not some other, smaller extract "
              f"(this is an easy mix-up across a long working session with several similar "
              f"files in play).", file=sys.stderr)

    counts = {'confirmed': 0, 'unconfirmed': 0}
    for doc_id in tqdm(changed_ids, desc="Classifying changed documents", unit="doc"):
        before_text = before_docs.get(doc_id, '')
        after_text = after_docs.get(doc_id, '')

        if doc_id not in before_docs:
            status = 'appeared (present only in --after)'
        elif doc_id not in after_docs:
            status = 'disappeared (present only in --before)'
        else:
            removed, added = refine_word_diff(before_text, after_text)
            status = f'changed: -{removed[:200]!r} +{added[:200]!r}'

        if doc_id in source_texts:
            invoked = find_redirect_templates_invoked(
                ex, doc_id, source_texts[doc_id], non_english_redirect_titles)
        else:
            invoked = None

        if invoked:
            counts['confirmed'] += 1
            print(f"[OK] Doc {doc_id}: {status}")
            print(f"     confirmed -- this article's own extraction actually invoked: "
                  f"{', '.join(sorted(invoked))}")
        else:
            counts['unconfirmed'] += 1
            marker = '!!' if invoked is not None else '??'
            reason = ("no non-English-keyword redirect was invoked during this article's "
                      "own extraction -- not explained by this mechanism" if invoked is not None
                      else "doc id not found in --source -- cannot confirm at all")
            print(f"[{marker}] Doc {doc_id}: {status}")
            print(f"     {reason}")
        print()

    total = sum(counts.values())
    print(f"Summary: {total} changed document(s) -- "
          f"{counts['confirmed']} confirmed (this article's own extraction actually invoked a "
          f"non-English-keyword redirect), "
          f"{counts['unconfirmed']} unconfirmed (not explained by this mechanism, or couldn't "
          f"be checked -- worth a manual look)")


if __name__ == '__main__':
    main()
