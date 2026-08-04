#!/usr/bin/env python3
"""
find_missing_redirects.py

Compares the output of two WikiExtractor runs (e.g. "before" and "after" a
redirect-handling fix), finds page ids present in the "old" output but
missing from the "new" output, and checks each missing id against the
original XML dump to classify *why* it disappeared:

  - REDIRECT_WITH_CONTENT: the page has a <redirect> tag in the dump AND
    has more than a trivial amount of text after the redirect line. This is
    the expected/acceptable case (content that was never visible on live
    Wikipedia to begin with) -- matches the SKR/UR pattern already checked
    by hand.
  - REDIRECT_TRIVIAL: has a <redirect> tag but little/no trailing content.
    Also expected -- nothing of substance was dropped.
  - NOT_A_REDIRECT: missing from the new output, but the dump shows no
    <redirect> tag at all. This is NOT the expected pattern and deserves a
    closer look -- it means something other than pure redirect-filtering
    caused the page to disappear.
  - NOT_FOUND_IN_DUMP: the id never turned up while scanning the dump
    (wrong dump file, id mismatch, or scan gave up early -- see --no-early-exit).

Usage:
    python3 find_missing_redirects.py \
        --old  /path/to/old_output_dir_or_file \
        --new  /path/to/new_output_dir_or_file \
        --dump /path/to/xxwiki-latest-pages-meta-current.xml.bz2 \
        [--trailing-threshold 20] \
        [--csv report.csv] \
        [--no-early-exit]

--old and --new each accept either a single file or a directory (searched
recursively); files may be plain text, .bz2, or .gz, and either the
classic <doc id="..." url="..." title="...">...</doc> format or WikiExtractor's
--json line format.
"""

import argparse
import bz2
import gzip
import json
import os
import re
import sys
import csv as csv_module

DOC_OPEN_RE = re.compile(r'<doc\s+id="(\d+)"[^>]*?title="([^"]*)"[^>]*>')

PAGE_ID_RE = re.compile(r'<id>(\d+)</id>')
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
REDIRECT_TAG_RE = re.compile(r'<redirect\b')
TEXT_OPEN_RE = re.compile(r'<text[^>]*>')
TEXT_CLOSE_RE = re.compile(r'</text>')


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
    print(f"[{verbose_label}] scanned {file_count} file(s), found {len(ids)} doc ids",
          file=sys.stderr)
    return ids


def scan_dump_for_ids(dump_path, target_ids, early_exit=True):
    """
    Stream through the original XML dump looking for <page> blocks whose
    page id is in target_ids. Returns {id: info_dict} where info_dict has
    keys: title, has_redirect, text_len, trailing_len, trailing_preview.

    Only buffers one page's worth of lines at a time, and (by default) stops
    once every target id has been found, so this is safe to run against a
    full-size dump even though we only care about a handful of ids.
    """
    remaining = set(target_ids)
    results = {}

    in_page = False
    page_lines = []
    page_id = None
    seen_first_id = False

    with open_any(dump_path) as f:
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
                    results[page_id] = _parse_page_block(''.join(page_lines))
                    remaining.discard(page_id)
                page_lines = []
                if early_exit and not remaining:
                    break

    return results, remaining


def _parse_page_block(block):
    title_m = TITLE_RE.search(block)
    title = title_m.group(1) if title_m else ''
    has_redirect = bool(REDIRECT_TAG_RE.search(block))

    text_open_m = TEXT_OPEN_RE.search(block)
    text = ''
    if text_open_m:
        text_close_m = TEXT_CLOSE_RE.search(block, text_open_m.end())
        end = text_close_m.start() if text_close_m else len(block)
        text = block[text_open_m.end():end]

    stripped = text.strip()
    lines = stripped.split('\n') if stripped else []
    trailing = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''

    return {
        'title': title,
        'has_redirect': has_redirect,
        'text_len': len(stripped),
        'trailing_len': len(trailing),
        'trailing_preview': trailing[:80].replace('\n', ' '),
    }


def classify(info, trailing_threshold):
    if info is None:
        return 'NOT_FOUND_IN_DUMP'
    if info['has_redirect']:
        if info['trailing_len'] > trailing_threshold:
            return 'REDIRECT_WITH_CONTENT'
        return 'REDIRECT_TRIVIAL'
    return 'NOT_A_REDIRECT'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--old', required=True, help='old extractor output (file or dir)')
    ap.add_argument('--new', required=True, help='new extractor output (file or dir)')
    ap.add_argument('--dump', required=True, help='original XML dump (.xml, .bz2, or .gz)')
    ap.add_argument('--trailing-threshold', type=int, default=20,
                     help='chars of trailing content above which a redirect '
                          'is REDIRECT_WITH_CONTENT rather than REDIRECT_TRIVIAL '
                          '(default: 20)')
    ap.add_argument('--csv', help='optional path to write a CSV report')
    ap.add_argument('--no-early-exit', action='store_true',
                     help='scan the whole dump even after all missing ids are found '
                          '(useful for sanity-checking id coverage)')
    args = ap.parse_args()

    old_ids = collect_doc_ids(args.old, 'old')
    new_ids = collect_doc_ids(args.new, 'new')

    missing = sorted(set(old_ids) - set(new_ids), key=int)
    print(f"\n{len(missing)} page id(s) in old output but missing from new output\n",
          file=sys.stderr)

    if not missing:
        print("No missing pages -- nothing to classify.")
        return

    dump_info, not_found = scan_dump_for_ids(args.dump, missing,
                                              early_exit=not args.no_early_exit)

    rows = []
    counts = {}
    for pid in missing:
        info = dump_info.get(pid)
        label = classify(info, args.trailing_threshold)
        counts[label] = counts.get(label, 0) + 1
        rows.append({
            'id': pid,
            'title': old_ids.get(pid, info['title'] if info else ''),
            'classification': label,
            'has_redirect': info['has_redirect'] if info else '',
            'text_len': info['text_len'] if info else '',
            'trailing_len': info['trailing_len'] if info else '',
            'trailing_preview': info['trailing_preview'] if info else '',
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
            print(f"                          trailing={r['trailing_len']} chars: {r['trailing_preview']!r}")
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
