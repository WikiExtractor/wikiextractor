#!/usr/bin/env python3
"""
classify_missing_pages.py

SEE ALSO: confirm_redirect_keyword_causation.py -- a different, heavier
tool for a different question. This script (classify_missing_pages.py)
answers "did this page disappear entirely, and if so, is that because
it's a genuine MediaWiki redirect?" by checking the raw dump directly --
fast, and doesn't care what code change caused the disappearance.
confirm_redirect_keyword_causation.py instead answers "did THIS specific
content change happen because a newly-recognized redirect keyword was
actually invoked during this article's own extraction?" by re-running
the real extraction machinery -- slower, but causally confirms content
*changes* too, not just pages that vanished outright. If a page's id is
simply gone from the new output, this script is the right one; if a
page's *text* changed, or you need to confirm a specific keyword/template
mechanism rather than just "is the dump's own <redirect> tag set", use
confirm_redirect_keyword_causation.py instead.

Compares the output of two WikiExtractor runs (e.g. "before" and "after" a
redirect-handling fix), finds page ids present in the "before" output but
missing from the "after" output, and checks each missing id against the
original XML dump to classify *why* it disappeared:

  - REDIRECT_WITH_CONTENT: the page has a <redirect> tag in the dump AND
    has more than a trivial amount of text after the redirect line. This is
    the expected/acceptable case (content that was never visible on live
    Wikipedia to begin with) -- matches the SKR/UR pattern already checked
    by hand.
  - REDIRECT_TRIVIAL: has a <redirect> tag but little/no trailing content.
    Also expected -- nothing of substance was dropped.
  - NOT_A_REDIRECT: missing from the "after" output, but the dump shows no
    <redirect> tag at all. This is NOT the expected pattern and deserves a
    closer look -- it means something other than pure redirect-filtering
    caused the page to disappear.
  - NOT_FOUND_IN_DUMP: the id never turned up while scanning the dump
    (wrong dump file, id mismatch, or scan gave up early -- see --no-early-exit).

Usage:
    python3 classify_missing_pages.py \
        --before /path/to/before_output_dir_or_file \
        --after  /path/to/after_output_dir_or_file \
        --dump   /path/to/xxwiki-latest-pages-meta-current.xml.bz2 \
        [--trailing-threshold 20] \
        [--csv report.csv] \
        [--no-early-exit]

--before and --after each accept either a single file or a directory
(searched recursively); files may be plain text, .bz2, or .gz, and either
the classic <doc id="..." url="..." title="...">...</doc> format or
WikiExtractor's --json line format.
"""

import argparse
import bz2
import gzip
import json
import os
import re
import sys
import csv as csv_module

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

PAGE_ID_RE = re.compile(r'<id>(\d+)</id>')
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
REDIRECT_TAG_RE = re.compile(r'<redirect\b')
TEXT_OPEN_RE = re.compile(r'<text[^>]*>')
TEXT_CLOSE_RE = re.compile(r'</text>')


class _ProgressReader:
    """Wraps a raw binary file object, calling pbar.update() with the
    number of bytes actually read off of it. Handed to bz2.open()/
    gzip.open() as their `filename` argument (both accept a file object
    in place of a path) so the progress bar reflects bytes consumed
    from disk -- i.e. how far through the (compressed) dump file we
    are -- regardless of the decompressed line-by-line iteration on
    top. Anything other than read()/close() is delegated straight
    through to the raw object, since that's all bz2/gzip need plus
    whatever incidental attribute access shows up."""
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


def open_any(path, mode='rt'):
    """Open a plain, .bz2, or .gz file transparently, as UTF-8 text."""
    if path.endswith('.bz2'):
        return bz2.open(path, mode, encoding='utf-8', errors='replace')
    if path.endswith('.gz'):
        return gzip.open(path, mode, encoding='utf-8', errors='replace')
    return open(path, mode, encoding='utf-8', errors='replace')


def iter_files(path):
    """Yield file paths under `path` (or just `path` itself if it's a file)."""
    if os.path.isfile(path):
        yield path
        return
    for root, _dirs, files in sorted(os.walk(path)):
        for name in sorted(files):
            yield os.path.join(root, name)


def collect_doc_ids(path, verbose_label):
    """
    Scan an extractor-output tree and return {id: title}.
    Supports both classic <doc ...> format and --json line format.
    """
    ids = {}
    file_count = 0
    for fpath in iter_files(path):
        file_count += 1
        try:
            with open_any(fpath) as f:
                for line in f:
                    line_stripped = line.lstrip()
                    if line_stripped.startswith('{'):
                        # --json format: one JSON object per line
                        try:
                            obj = json.loads(line)
                        except (ValueError, json.JSONDecodeError):
                            continue
                        if 'id' in obj:
                            ids[str(obj['id'])] = obj.get('title', '')
                    elif '<doc' in line:
                        m = DOC_OPEN_RE.search(line)
                        if m:
                            ids[m.group(1)] = m.group(2)
        except (OSError, UnicodeDecodeError) as e:
            print(f"  warning: could not read {fpath}: {e}", file=sys.stderr)
    if file_count == 0:
        raise SystemExit(
            f"error: --{verbose_label} path {path!r} matched no files to scan "
            f"(nonexistent path, empty directory, or a typo?) -- refusing to "
            f"silently treat this as \"0 pages\"")
    print(f"[{verbose_label}] scanned {file_count} file(s), found {len(ids)} doc ids",
          file=sys.stderr)
    return ids


def scan_dump_for_ids(dump_path, target_ids, early_exit=True, ex_module=None, templates=None,
                       redirects=None, template_prefix=''):
    """
    Stream through the original XML dump looking for <page> blocks whose
    page id is in target_ids. Returns {id: info_dict} where info_dict has
    keys: title, has_redirect, text_len, raw_trailing_len,
    raw_trailing_preview, extracted_len, extracted_preview,
    extraction_error.

    Only buffers one page's worth of lines at a time, and (by default) stops
    once every target id has been found, so this is safe to run against a
    full-size dump even though we only care about a handful of ids.

    If ex_module (wikiextractor.extract) is given, each matched redirect
    page's raw wikitext is also run through the real clean_text() pipeline
    -- see _parse_page_block() -- so trailing_len reflects what would
    actually have shown up in the extracted corpus rather than raw dump
    byte count.
    """
    remaining = set(target_ids)
    total_targets = len(target_ids)
    results = {}

    in_page = False
    page_lines = []
    page_id = None
    seen_first_id = False

    dump_size = os.path.getsize(dump_path)
    with tqdm(total=dump_size, desc="scanning dump", unit="B", unit_scale=True,
              unit_divisor=1024) as pbar:
        pbar.set_postfix(found=f"0/{total_targets}")
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
                        results[page_id] = _parse_page_block(
                            ''.join(page_lines), ex_module, templates, redirects, template_prefix)
                        remaining.discard(page_id)
                        pbar.set_postfix(found=f"{len(results)}/{total_targets}")
                    page_lines = []
                    if early_exit and not remaining:
                        break

    return results, remaining


def _parse_page_block(block, ex_module=None, templates=None, redirects=None, template_prefix=''):
    title_m = TITLE_RE.search(block)
    title = title_m.group(1) if title_m else ''
    has_redirect = bool(REDIRECT_TAG_RE.search(block))
    id_m = PAGE_ID_RE.search(block)
    page_id = id_m.group(1) if id_m else ''

    text_open_m = TEXT_OPEN_RE.search(block)
    text = ''
    if text_open_m:
        text_close_m = TEXT_CLOSE_RE.search(block, text_open_m.end())
        end = text_close_m.start() if text_close_m else len(block)
        text = block[text_open_m.end():end]

    stripped = text.strip()
    lines = stripped.split('\n') if stripped else []
    trailing = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''

    result = {
        'title': title,
        'has_redirect': has_redirect,
        'text_len': len(stripped),
        'raw_trailing_len': len(trailing),
        'raw_trailing_preview': trailing[:80].replace('\n', ' '),
        # Populated only when running with real extraction verification
        # (the default; see --no-extraction-check). None means "not run",
        # as distinct from 0, which means "ran, and it's genuinely empty".
        'extracted_len': None,
        'extracted_preview': '',
        'extraction_error': '',
    }

    # Only worth actually running the extractor if there's a <redirect>
    # tag (otherwise this isn't the case we're trying to measure at all)
    # and some raw trailing text exists in the first place (an empty
    # trailing section trivially extracts to nothing).
    if ex_module is not None and has_redirect and trailing:
        try:
            extractor = ex_module.Extractor(
                page_id, page_id, f"https://example.org/wiki?curid={page_id}",
                title, [stripped], templates=templates, redirects=redirects,
                templatePrefix=template_prefix)
            # Run on the full page text, redirect line included, to
            # exactly mirror what the real extraction pipeline does in
            # Extractor.extract() -- not just the trailing portion in
            # isolation, in case the redirect line itself affects how
            # the rest of the text gets parsed.
            extracted = extractor.clean_text(stripped)
            extracted_text = '\n'.join(extracted).strip()
            result['extracted_len'] = len(extracted_text)
            result['extracted_preview'] = extracted_text[:80].replace('\n', ' ')
        except Exception as e:
            # A page that fails to extract cleanly still tells us
            # something (worth a manual look) -- don't let one bad page
            # abort the whole run.
            result['extraction_error'] = f"{type(e).__name__}: {e}"

    return result


def classify(info, trailing_threshold):
    if info is None:
        return 'NOT_FOUND_IN_DUMP'
    if not info['has_redirect']:
        return 'NOT_A_REDIRECT'
    # Prefer the real extracted length when we have it -- it's what
    # actually would or wouldn't have shown up in the corpus. Fall back
    # to the raw dump byte count only when extraction verification
    # wasn't run at all (--no-extraction-check, or ex_module unavailable).
    length = info['extracted_len']
    if length is None:
        length = info['raw_trailing_len']
    if length > trailing_threshold:
        return 'REDIRECT_WITH_CONTENT'
    return 'REDIRECT_TRIVIAL'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--before', required=True, help='extractor output before the change (file or dir)')
    ap.add_argument('--after', required=True, help='extractor output after the change (file or dir)')
    ap.add_argument('--dump', required=True, help='original XML dump (.xml, .bz2, or .gz)')
    ap.add_argument('--templates', help='optional templates file (<page>/<title>/<text> shape, as '
                          'produced by load_templates()/extract_page_range.py --templates) to load '
                          'before running the extraction check, so templates referenced in a '
                          'redirect page\'s trailing content resolve to their real content instead '
                          'of nothing. Without this, template calls in trailing content resolve to '
                          'nothing, same as an undefined template -- fine for the common case '
                          '(maintenance/categorization templates, which are supposed to produce '
                          'nothing), but can UNDERstate real content for a redirect whose trailing '
                          'text pulls in a real content template (e.g. an infobox).')
    ap.add_argument('--trailing-threshold', type=int, default=20,
                     help='chars of trailing content above which a redirect '
                          'is REDIRECT_WITH_CONTENT rather than REDIRECT_TRIVIAL. '
                          'Applies to the real extracted length when the extraction '
                          'check ran (the default), or to raw dump byte count '
                          'otherwise (--no-extraction-check) (default: 20)')
    ap.add_argument('--no-extraction-check', action='store_true',
                     help='skip running the real clean_text() extraction pipeline on '
                          'each candidate redirect page, and classify from raw dump '
                          'byte count alone (faster, no wikiextractor dependency, but '
                          'raw wikitext bytes routinely overstate real content -- e.g. '
                          'maintenance templates and category tags count as bytes here '
                          'but produce no visible output at all)')
    ap.add_argument('--csv', help='optional path to write a CSV report')
    ap.add_argument('--no-early-exit', action='store_true',
                     help='scan the whole dump even after all missing ids are found '
                          '(useful for sanity-checking id coverage)')
    args = ap.parse_args()

    ex_module = None
    templates = None
    redirects = None
    template_prefix = ''
    if not args.no_extraction_check:
        try:
            import wikiextractor.extract as ex_module
            if args.templates:
                import wikiextractor.WikiExtractor as we
                print(f"Loading templates from {args.templates}...", file=sys.stderr)
                templates = {}
                redirects = {}
                with we.decode_open(args.templates) as f:
                    count, template_prefix = we.load_templates(f, templates=templates, redirects=redirects)
                print(f"Loaded {count} templates.", file=sys.stderr)
        except ImportError as e:
            print(f"warning: --no-extraction-check was not given, but wikiextractor isn't "
                  f"importable ({e}) -- falling back to raw dump byte counts instead of real "
                  f"extraction. Put wikiextractor on PYTHONPATH to get real extracted-content "
                  f"lengths, or pass --no-extraction-check to silence this warning.",
                  file=sys.stderr)
            ex_module = None

    before_ids = collect_doc_ids(args.before, 'before')
    after_ids = collect_doc_ids(args.after, 'after')

    missing = sorted(set(before_ids) - set(after_ids), key=int)
    print(f"\n{len(missing)} page id(s) in 'before' output but missing from 'after' output\n",
          file=sys.stderr)

    if not missing:
        print("No missing pages -- nothing to classify.")
        return

    dump_info, not_found = scan_dump_for_ids(args.dump, missing,
                                              early_exit=not args.no_early_exit,
                                              ex_module=ex_module, templates=templates,
                                              redirects=redirects, template_prefix=template_prefix)

    rows = []
    counts = {}
    for pid in missing:
        info = dump_info.get(pid)
        label = classify(info, args.trailing_threshold)
        counts[label] = counts.get(label, 0) + 1
        rows.append({
            'id': pid,
            'title': before_ids.get(pid, info['title'] if info else ''),
            'classification': label,
            'has_redirect': info['has_redirect'] if info else '',
            'text_len': info['text_len'] if info else '',
            'raw_trailing_len': info['raw_trailing_len'] if info else '',
            'raw_trailing_preview': info['raw_trailing_preview'] if info else '',
            'extracted_len': info['extracted_len'] if info else '',
            'extracted_preview': info['extracted_preview'] if info else '',
            'extraction_error': info['extraction_error'] if info else '',
        })

    # Print a human-readable summary, flagged cases first.
    priority = {'NOT_A_REDIRECT': 0, 'NOT_FOUND_IN_DUMP': 1,
                'REDIRECT_TRIVIAL': 2, 'REDIRECT_WITH_CONTENT': 3}
    rows.sort(key=lambda r: priority.get(r['classification'], 9))

    print("=" * 100)
    for r in rows:
        flag = " <-- CHECK THIS" if r['classification'] in ('NOT_A_REDIRECT', 'NOT_FOUND_IN_DUMP') else ""
        print(f"[{r['classification']:<22}] id={r['id']:<10} title={r['title']!r}{flag}")
        if r['classification'] == 'REDIRECT_WITH_CONTENT':
            if r['extraction_error']:
                print(f"                          extraction FAILED ({r['extraction_error']}) "
                      f"-- classified from raw dump bytes: {r['raw_trailing_len']} chars")
            elif r['extracted_len'] is not None:
                print(f"                          extracted={r['extracted_len']} chars: "
                      f"{r['extracted_preview']!r}  (raw dump bytes: {r['raw_trailing_len']})")
            else:
                print(f"                          raw dump bytes: {r['raw_trailing_len']} chars: "
                      f"{r['raw_trailing_preview']!r}  (no extraction check ran)")
    print("=" * 100)
    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv_module.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote CSV report to {args.csv}", file=sys.stderr)


if __name__ == '__main__':
    main()
