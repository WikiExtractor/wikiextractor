#!/usr/bin/env python3
"""
extract_templates_by_id.py

Given a page dump and the id(s) of specific page(s) in it, find every
template those pages actually reference -- using extract.py's own,
real template-expansion machinery to discover them, not a regex
heuristic -- then pull all of them out of a separate, large templates
file.

Why the real machinery instead of a heuristic: extract.py's own
expandTemplate() already correctly handles everything a hand-rolled
"{{" scanner would get wrong or have to special-case --
    - Magic words (e.g. {{PAGENAME}}) are recognized and never treated
      as a template lookup at all.
    - Parser functions ({{#if:...}}, {{#invoke:...}}, etc.) are
      dispatched via callParserFunction() before any template lookup
      happens, so #invoke's own module name is never mistaken for a
      page name.
    - Redirects (title -> another title) are resolved via the real
      redirects dict, populated by define_template() itself detecting
      "#REDIRECT [[...]]" pages.
    - Nested calls, and titles that only become visible after some
      OTHER template has actually been substituted, are handled
      because this hooks the exact point where expandTemplate() looks
      a title up, not just a static scan of the original text.

How it works: templates and templateCache (module-level dicts in
extract.py) are temporarily replaced with instrumented versions that
record every title expandTemplate() checks for, while still behaving
like normal dicts. Each pass:
  1. Runs the real clean_text() on the page's wikitext with whatever
     templates have been loaded so far (none, on the first pass) --
     every title expandTemplate() tries to look up and can't yet
     satisfy gets recorded.
  2. Searches the templates file for those specific titles, and loads
     any found via the real define_template() (so redirects are
     handled correctly, same as define_template always does).
  3. If anything new was loaded, repeats -- since expanding into a
     newly-loaded template's own body can reveal further, nested
     lookups that weren't visible before. Stops once a pass loads
     nothing new.

Usage:
    python3 extract_templates_by_id.py --templates templates.xml.bz2 \
        --input tables.bz2 --ids 49,59 --output infoboxes.xml

Notes:
    - Requires wikiextractor's extract.py to be importable (e.g. on
      $PYTHONPATH already).
    - Streams the templates file on every pass rather than holding it
      in memory; --max-passes guards against an unexpectedly deep or
      circular chain.
"""

import argparse
import bz2
import re
import sys


def open_maybe_bz2_read(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')


def parse_id_list(s):
    ids = []
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid id {part!r} in --ids {s!r} -- expected an integer, "
                f"or a comma-separated list of integers")
    if not ids:
        raise argparse.ArgumentTypeError(f"--ids {s!r} contained no ids")
    return ids


def stream_pages(input_path):
    """Yields ('page', text) once per <page>...</page> block, verbatim."""
    with open_maybe_bz2_read(input_path) as f:
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
                    yield ('page', ''.join(page_lines))
                    page_lines = []
                continue


def get_pages_wikitext_by_id(input_path, wanted_ids):
    """Returns {id: wikitext} for whichever of wanted_ids are found."""
    still_needed = set(wanted_ids)
    result = {}
    for kind, text in stream_pages(input_path):
        if not still_needed:
            break
        id_match = re.search(r'<id>(\d+)</id>', text)
        if not id_match:
            continue
        page_id = int(id_match.group(1))
        if page_id in still_needed:
            text_match = re.search(r'<text[^>]*>(.*?)</text>', text, re.DOTALL)
            if text_match:
                result[page_id] = text_match.group(1)
            still_needed.discard(page_id)
    if still_needed:
        missing = ', '.join(str(i) for i in sorted(still_needed))
        print(f"WARNING: {len(still_needed)} page id(s) not found in --input: {missing}",
              file=sys.stderr)
    return result


def determine_template_prefix(templates_path):
    """
    Same approach load_templates() itself uses: reconstruct the
    template namespace prefix (e.g. "Template:", or "سانچہ:" on PNB)
    from the first template title actually in the file, rather than
    assuming "Template:" specifically.
    """
    for kind, text in stream_pages(templates_path):
        m = re.search(r'<title>(.*?)</title>', text, re.DOTALL)
        if m:
            title = m.group(1)
            colon = title.find(':')
            if colon > 1:
                return title[:colon + 1]
    return 'Template:'  # fallback if the file is empty or has no ':' in any title


class RecordingDict(dict):
    """
    Behaves exactly like a normal dict, except every key checked via
    `in` gets passed to on_lookup first. Used in place of extract.py's
    real `templates`/`templateCache` globals so every title
    expandTemplate() tries to resolve gets recorded, regardless of
    whether the lookup succeeds.
    """
    def __init__(self, *args, on_lookup, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_lookup = on_lookup

    def __contains__(self, key):
        self._on_lookup(key)
        return super().__contains__(key)


def discover_and_extract(wikiextractor_extract_module, page_texts, templates_path, max_passes):
    ex = wikiextractor_extract_module
    ex.Extractor.templatePrefix = determine_template_prefix(templates_path)

    looked_up = set()
    ex.templates = RecordingDict(on_lookup=looked_up.add)
    ex.templateCache = RecordingDict(on_lookup=looked_up.add)
    ex.redirects.clear()

    loaded_pages = {}  # title -> original <page> text, for writing to --output

    for pass_num in range(1, max_passes + 1):
        looked_up.clear()
        for page_id, wikitext in page_texts.items():
            extractor = ex.Extractor(page_id, str(page_id),
                                      f"https://example.org/wiki?curid={page_id}",
                                      f"Page{page_id}", [wikitext])
            try:
                extractor.clean_text(wikitext, expand_templates=True)
            except Exception:
                # a page that genuinely can't finish expanding (e.g. hits
                # recursion limits) still reveals whatever it looked up
                # before failing -- looked_up is unaffected by the exception
                pass

        still_unsatisfied = {t for t in looked_up
                              if t not in ex.templates and t not in ex.templateCache}
        if not still_unsatisfied:
            break

        print(f"Pass {pass_num}: {len(still_unsatisfied)} title(s) not yet resolved, searching...")
        found_this_pass, remaining = find_titles_in_templates_file(
            templates_path, still_unsatisfied)

        if not found_this_pass:
            # nothing new to load -- no point re-running clean_text again
            break

        for title, page_text in found_this_pass.items():
            loaded_pages[title] = page_text
            text_match = re.search(r'<text[^>]*>(.*?)</text>', page_text, re.DOTALL)
            page_lines = [text_match.group(1)] if text_match else ['']
            ex.define_template(title, page_lines)
    else:
        print(f"WARNING: stopped after {max_passes} passes -- possible deep "
              f"nesting or a circular reference", file=sys.stderr)

    never_found = {t for t in looked_up
                    if t not in ex.templates and t not in ex.templateCache
                    and t not in loaded_pages}
    return loaded_pages, never_found


def find_titles_in_templates_file(templates_path, wanted_titles):
    """One full pass over templates_path, exact title match only (titles
    reaching this point have already been through extract.py's own
    fullyQualifiedTemplateTitle(), so they're already fully qualified --
    no bare-name/prefix flexibility needed here, unlike
    extract_templates_by_title.py's own matching)."""
    still_needed = set(wanted_titles)
    found = {}
    for kind, text in stream_pages(templates_path):
        if not still_needed:
            break
        m = re.search(r'<title>(.*?)</title>', text, re.DOTALL)
        if not m:
            continue
        actual_title = m.group(1)
        if actual_title in still_needed:
            found[actual_title] = text
            still_needed.discard(actual_title)
    return found, still_needed


def open_maybe_bz2_write(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'wt', encoding='utf-8')
    return open(path, 'w', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--templates', required=True, help='Path to the large templates file (.xml or .xml.bz2)')
    ap.add_argument('--input', required=True, help='Path to the page dump containing the page(s) in question')
    ap.add_argument('--ids', required=True, type=parse_id_list,
                     help='Page id(s) to find referenced templates for -- a single id, '
                          'or a comma-separated list, e.g. 49,59')
    ap.add_argument('--output', required=True, help='Output path for the extracted templates (.xml or .xml.bz2)')
    ap.add_argument('--max-passes', type=int, default=20,
                     help='Safety cap on how many transitive-dependency passes to run (default: 20)')
    args = ap.parse_args()

    import wikiextractor.extract as ex

    pages = get_pages_wikitext_by_id(args.input, args.ids)
    if not pages:
        print("No requested pages were found -- nothing to look up.", file=sys.stderr)
        sys.exit(1)

    loaded_pages, never_found = discover_and_extract(ex, pages, args.templates, args.max_passes)

    out = open_maybe_bz2_write(args.output)
    try:
        for title, page_text in loaded_pages.items():
            out.write(page_text)
    finally:
        out.close()

    print(f"\nWrote {len(loaded_pages)} template(s) to {args.output}:")
    for title in loaded_pages:
        print(f"  {title}")
    if never_found:
        missing = ', '.join(sorted(never_found))
        print(f"\nWARNING: {len(never_found)} referenced title(s) never found "
              f"in --templates: {missing}", file=sys.stderr)


if __name__ == '__main__':
    main()
