#!/usr/bin/env python3
"""
extract_page_range.py

Slice pages out of a (possibly bz2-compressed) MediaWiki XML dump,
producing a smaller, independently valid dump -- for bisecting which
page(s) cause wikiextractor to stall, or for pulling out one specific
page by its <id> to test or share.

Four modes:

  --count        Just stream through and report the total number of <page>
                 elements (fast, doesn't hold anything in memory).

  --find-id      Report the 0-based ordinal position of the page with a
                 given <id> (needed if you want to slice a --start/--end
                 range around it), and exit.

  --start/--end  Stream through once, and write out a new dump containing
                 only pages [start, end) (0-indexed, half-open range),
                 wrapped in the same <mediawiki>/<siteinfo> header and
                 </mediawiki> footer as the original, so wikiextractor can
                 process it as a standalone file.

  --extract-id   Find the page(s) with the given <id>(s) and write them out
                 as a standalone dump, in a single streaming pass (stops as
                 soon as all requested ids have been found, rather than
                 needing a separate --find-id pass first, and rather than
                 always reading to the end of the file). The simplest way
                 to pull out one or more specific pages. Accepts a single
                 id or a comma-separated list (e.g. --extract-id 49,59);
                 pages are written in the order they appear in the source
                 dump, not the order given on the command line.

Usage:
    python extract_page_range.py --input urwiki-20260701-pages-articles-multistream.xml.bz2 --count

    python extract_page_range.py --input urwiki-20260701-pages-articles-multistream.xml.bz2 --find-id 2376854

    python extract_page_range.py --input urwiki-20260701-pages-articles-multistream.xml.bz2 \
        --start 0 --end 150000 --output half1.xml.bz2

    python extract_page_range.py --input urwiki-20260701-pages-articles-multistream.xml.bz2 \
        --start 150000 --end 300000 --output half2.xml.bz2

    python extract_page_range.py --input urwiki-20260701-pages-articles-multistream.xml.bz2 \
        --extract-id 2376854 --output single_page.xml.bz2

    python extract_page_range.py --input pnwiki-20260701-pages-articles.xml.bz2 \
        --extract-id 49,59 --output two_pages.xml.bz2

Binary search workflow (for bisecting a stall):
    1. --count to get total page count N.
    2. Split [0, N) in half, extract both halves, run wikiextractor on each
       (normal multiprocessing settings -- reproduce the stall).
    3. Whichever half stalls, split THAT range in half again. Repeat.
    4. Once the range is down to a few thousand pages, wikiextractor will
       finish (or stall) fast even with --processes 1, so you can switch to
       that for the final pinpointing and confirm the single offending
       article by watching the last debug line printed.
    5. Once you know the specific page id, --extract-id pulls it out
       directly for isolated testing, without repeating the bisection.

Notes:
    - Streams the input; never holds the whole dump in memory.
    - Detects <page> / </page> on their own (whitespace-trimmed) line, which
      is how Wikimedia dumps are always formatted. Page content is XML-escaped
      by the exporter, so a literal "<page>" line can't appear inside article
      text.
    - Output is written as .bz2 if the --output path ends in .bz2, otherwise
      as plain XML text.
"""

import argparse
import bz2
import re
import sys


def parse_id_list(s):
    """
    argparse type for --extract-id: accepts either a single id ("49")
    or a comma-separated list ("49,59"). Whitespace around each entry
    is tolerated. Returns a list of ints, in the order given (though
    do_extract_ids() writes pages in source-dump order regardless,
    not this order).
    """
    ids = []
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid id {part!r} in --extract-id {s!r} -- expected an "
                f"integer, or a comma-separated list of integers")
    if not ids:
        raise argparse.ArgumentTypeError(f"--extract-id {s!r} contained no ids")
    return ids


def open_maybe_bz2_read(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')


def open_maybe_bz2_write(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'wt', encoding='utf-8')
    return open(path, 'w', encoding='utf-8')


def stream_pages(input_path):
    """
    Yields (header_text, page_text_generator) -- actually simpler: this is a
    generator-based single pass. We yield control via a small state machine
    so the caller can decide, page by page, whether to keep or skip it,
    without ever holding more than one page's text in memory at a time.

    Yields tuples: ('header', text) once, then ('page', text) per page,
    then ('footer', text) once.
    """
    with open_maybe_bz2_read(input_path) as f:
        header_lines = []
        in_page = False
        page_lines = []
        got_header = False

        for line in f:
            stripped = line.strip()

            if not in_page and stripped == '<page>':
                if not got_header:
                    yield ('header', ''.join(header_lines))
                    got_header = True
                in_page = True
                page_lines = [line]
                continue

            if in_page:
                page_lines.append(line)
                if stripped == '</page>':
                    in_page = False
                    yield ('page', ''.join(page_lines))
                    page_lines = []
                continue

            if stripped == '</mediawiki>':
                yield ('footer', line)
                return

            header_lines.append(line)


def do_count(input_path):
    n = 0
    for kind, _ in stream_pages(input_path):
        if kind == 'page':
            n += 1
    print(f"Total pages: {n}")


def do_find_id(input_path, page_id):
    """Report the 0-based ordinal position of a page, given its <id>."""
    idx = 0
    for kind, text in stream_pages(input_path):
        if kind == 'page':
            # crude but fine: the first <id>...</id> in a page block is the
            # page id (revision/contributor ids come after it in the block).
            m = re.search(r'<id>(\d+)</id>', text)
            if m and m.group(1) == str(page_id):
                print(f"Page id {page_id} found at ordinal position {idx}")
                return idx
            idx += 1
    print(f"Page id {page_id} not found (scanned {idx} pages)")
    return None


def do_extract(input_path, output_path, start, end):
    header = None
    footer = '</mediawiki>\n'
    idx = 0
    kept = 0

    out = open_maybe_bz2_write(output_path)
    try:
        for kind, text in stream_pages(input_path):
            if kind == 'header':
                header = text
                out.write(header)
            elif kind == 'page':
                if start <= idx < end:
                    out.write(text)
                    kept += 1
                idx += 1
                if idx >= end:
                    # We've collected everything we need; still need footer.
                    break
            elif kind == 'footer':
                footer = text
        out.write(footer)
    finally:
        out.close()

    print(f"Wrote {kept} pages (requested range [{start}, {end})) to {output_path}")
    if kept == 0:
        print("WARNING: 0 pages written -- check your --start/--end against the total "
              "page count (use --count first).", file=sys.stderr)


def do_extract_ids(input_path, output_path, page_ids):
    """
    Find the page(s) with the given <id>(s) and write them out as a
    standalone dump. Single streaming pass: stops as soon as every
    requested id has been found, rather than requiring a separate
    --find-id pass beforehand, and rather than always reading to the
    end of the file regardless of how early the matches turn up.

    Pages are written in the order they're encountered in the source
    dump, not the order given in page_ids -- simpler, and matches how
    --start/--end already behaves.
    """
    header = None
    footer = '</mediawiki>\n'
    still_needed = set(page_ids)
    found_ids = []

    out = open_maybe_bz2_write(output_path)
    try:
        for kind, text in stream_pages(input_path):
            if kind == 'header':
                header = text
                out.write(header)
            elif kind == 'page':
                m = re.search(r'<id>(\d+)</id>', text)
                if m and int(m.group(1)) in still_needed:
                    out.write(text)
                    found_ids.append(int(m.group(1)))
                    still_needed.discard(int(m.group(1)))
                    if not still_needed:
                        break
            elif kind == 'footer':
                footer = text
        out.write(footer)
    finally:
        out.close()

    print(f"Wrote {len(found_ids)} page(s) ({', '.join(str(i) for i in found_ids)}) "
          f"to {output_path}")
    if still_needed:
        missing = ', '.join(str(i) for i in sorted(still_needed))
        print(f"WARNING: {len(still_needed)} id(s) not found: {missing}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, help='Path to the source dump (.xml or .xml.bz2)')
    ap.add_argument('--count', action='store_true', help='Just count total <page> elements and exit')
    ap.add_argument('--find-id', type=int, help='Report the ordinal position of the page with this <id> and exit')
    ap.add_argument('--extract-id', type=parse_id_list,
                     help='Find and write out the page(s) with this <id> -- a single '
                          'id, or a comma-separated list, e.g. 49,59 (requires --output)')
    ap.add_argument('--start', type=int, help='Start page index (0-based, inclusive)')
    ap.add_argument('--end', type=int, help='End page index (exclusive)')
    ap.add_argument('--output', help='Output path (.xml or .xml.bz2)')
    args = ap.parse_args()

    if args.count:
        do_count(args.input)
        return

    if args.find_id is not None:
        do_find_id(args.input, args.find_id)
        return

    if args.extract_id is not None:
        if not args.output:
            ap.error('--extract-id requires --output')
        do_extract_ids(args.input, args.output, args.extract_id)
        return

    if args.start is None or args.end is None or not args.output:
        ap.error('--start, --end, and --output are required unless --count is given')

    do_extract(args.input, args.output, args.start, args.end)


if __name__ == '__main__':
    main()
