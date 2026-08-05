#!/usr/bin/env python3
"""
find_redirect_keywords.py

Discovers the localized "#REDIRECT" magic word(s) for one or more
MediaWiki wikis, so they can be added to extract.py's redirectKeywords
without needing to stumble onto each language's specific broken
template by hand first (see test_redirect_keywords.py for two real
cases found that way -- Sindhi's "چوريو" and Urdu's "رجوع_مکرر").

Two independent methods, meant to be used together:

  api      Query the wiki's own MediaWiki API directly
           (action=query&meta=siteinfo&siprop=magicwords). This is
           the authoritative, current configuration for that specific
           wiki, works for literally any language with a live
           MediaWiki API, and needs no dump on hand at all. This is
           the one to reach for first, and the one to run in bulk
           across every language you might ever touch.

  dump     Scan a raw XML dump you already have for pages whose
           <redirect> tag is set (MediaWiki's own authoritative
           classification, independent of extract.py's keyword list
           entirely), and report what the first token of their actual
           wikitext looks like. This needs no network access at all,
           and it's grounded in what the wiki's real content actually
           uses -- a good cross-check against the API answer, and a
           fallback for a wiki whose API isn't reachable.

In normal use: run `api` for every language code you might ever
process (cheap, one request per wiki, covers languages you don't have
a dump for yet), and spot-check a few with `dump` against real data
you already have on hand, since that's where the Urdu and Sindhi
cases were actually found.


Checking for cross-language false positives (--check-false-positives):

  redirectKeywords is one flat, global list shared across every wiki
  this codebase processes -- there's no per-language scoping anywhere.
  So a keyword added because it's genuinely "#REDIRECT" on wiki A
  becomes live for every OTHER wiki too, and could misfire there if
  the same word happens to mean something unrelated on wiki B. This
  is a real risk particularly among related languages sharing a
  script and vocabulary (e.g. Urdu, Sindhi, Saraiki, and Shahmukhi
  Punjabi all share the Perso-Arabic script and a lot of Persian/
  Arabic-derived words).

  `dump ... --check-false-positives KEYWORD [KEYWORD ...]` checks this
  directly: for each given keyword, it looks for non-redirect pages on
  THIS wiki whose first line nonetheless matches the redirect shape
  ("#keyword ... [[wikilink]]"). A match there means MediaWiki itself
  does NOT consider that page a redirect, even though our regex would
  -- exactly the false-positive signature.

  Run this the OPPOSITE direction from where a keyword was found: to
  check whether Urdu's "رجوع_مکرر" is safe to also apply to Sindhi
  content, run `dump` against an SD dump (not a UR one), checking
  for "رجوع_مکرر" there:

      python3 find_redirect_keywords.py dump sdwiki-*.xml.bz2 \
          --check-false-positives رجوع_مکرر

  A flagged match isn't automatically a real collision, though --
  it can also just be a genuine, correctly-formatted redirect that
  MediaWiki didn't tag with <redirect> for some unrelated reason (this
  happened on a real UR page, "سانچہ:۔", during testing:
  a real "رجوع_مکرر" redirect with no <redirect> tag, not a translation
  collision). The tool surfaces the mismatch and the example text; it
  can't tell the two cases apart on its own -- read the example before
  deciding whether a keyword is actually safe to add to the shared
  list.

Usage:
    python3 find_redirect_keywords.py api ps sd pnb skr ur
    python3 find_redirect_keywords.py dump /path/to/urwiki-*.xml.bz2

Output for `api` includes a ready-to-paste redirectKeywords fragment.
"""

import argparse
import bz2
import gzip
import json
import re
import sys
import urllib.request
from collections import Counter

API_URL = 'https://{lang}.wikipedia.org/w/api.php'
USER_AGENT = 'wikiextractor-redirect-keyword-check/1.0 (see extract.py redirectKeywords)'

PAGE_ID_RE = re.compile(r'<id>(\d+)</id>')
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
REDIRECT_TAG_RE = re.compile(r'<redirect\b')
TEXT_OPEN_RE = re.compile(r'<text[^>]*>')
TEXT_CLOSE_RE = re.compile(r'</text>')

# The actual keyword token: '#' followed by word characters (covers
# ASCII and non-Latin scripts alike -- \w is Unicode-aware in Python 3
# regexes by default), stopping at whitespace, '[', or another '#'.
FIRST_TOKEN_RE = re.compile(r'^\s*#([^\s\[#]+)')


def open_any(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'rt', encoding='utf-8', errors='replace')
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'rt', encoding='utf-8', errors='replace')


def fetch_redirect_aliases(lang):
    """Query one wiki's API for its configured #REDIRECT aliases.
    Returns a list of alias strings (the '#' prefix is part of MediaWiki's
    own convention here, stripped before returning), or raises on failure.
    """
    url = API_URL.format(lang=lang) + \
        '?action=query&meta=siteinfo&siprop=magicwords&format=json&formatversion=2'
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    for mw in data['query']['magicwords']:
        if mw['name'] == 'redirect':
            # MediaWiki's own aliases already include the leading '#'
            # (e.g. "#REDIRECT"); strip it for direct comparison against
            # extract.py's redirectKeywords, which doesn't include it.
            return [a.lstrip('#') for a in mw['aliases']]
    return []


def cmd_api(args):
    print(f"{'lang':<6} aliases", file=sys.stderr)
    all_new = {}
    for lang in args.langs:
        try:
            aliases = fetch_redirect_aliases(lang)
        except Exception as e:
            print(f"{lang:<6} ERROR: {e}")
            continue
        print(f"{lang:<6} {aliases}")
        all_new[lang] = aliases

    non_english = sorted({a for aliases in all_new.values() for a in aliases
                           if a.upper() != 'REDIRECT'})
    if non_english:
        print("\nNon-English aliases found across the languages checked --", file=sys.stderr)
        print("candidates to add to extract.py's redirectKeywords:", file=sys.stderr)
        print(json.dumps(non_english, ensure_ascii=False))


def cmd_dump(args):
    counts = Counter()          # keyed by case-NORMALIZED keyword
    case_variants = {}          # normalized -> Counter of the actual raw casings seen
    examples = {}                # normalized -> (title, preview) of the first example seen
    total_redirects = 0
    false_positive_counts = Counter()
    false_positive_examples = {}
    check_keywords = set(args.check_false_positives or [])

    with open_any(args.dump) as f:
        in_page = False
        page_lines = []
        for line in f:
            if not in_page:
                if '<page>' in line:
                    in_page = True
                    page_lines = [line]
                continue
            page_lines.append(line)
            if '</page>' in line:
                in_page = False
                block = ''.join(page_lines)
                is_redirect = bool(REDIRECT_TAG_RE.search(block))
                title_m = TITLE_RE.search(block)
                title = title_m.group(1) if title_m else '?'
                text_open_m = TEXT_OPEN_RE.search(block)
                text = ''
                if text_open_m:
                    text_close_m = TEXT_CLOSE_RE.search(block, text_open_m.end())
                    end = text_close_m.start() if text_close_m else len(block)
                    text = block[text_open_m.end():end].strip()

                if is_redirect:
                    total_redirects += 1
                    m = FIRST_TOKEN_RE.match(text)
                    if m:
                        keyword = m.group(1)
                        # Group case-insensitively: redirectRE itself is
                        # compiled with re.IGNORECASE, so 'REDIRECT',
                        # 'redirect', and 'Redirect' are all the exact
                        # same already-handled keyword to the real
                        # matching code, not three different ones worth
                        # separately flagging for investigation.
                        norm = keyword.upper()
                        counts[norm] += 1
                        case_variants.setdefault(norm, Counter())[keyword] += 1
                        examples.setdefault(norm, (title, text[:60]))

                # False-positive check: does this page's first line look
                # exactly like a redirect to one of the candidate
                # keywords -- '#keyword' followed by a wikilink on the
                # same line -- even though MediaWiki's own <redirect>
                # tag says it ISN'T one? That mismatch is direct evidence
                # the keyword means something else in this wiki's
                # language, at least in this specific page. Matched
                # case-insensitively, same as the real redirectRE.
                if check_keywords and not is_redirect:
                    for kw in check_keywords:
                        kw_re = re.compile(r'^\s*#' + re.escape(kw) + r'\b.*?\[\[',
                                            re.IGNORECASE)
                        if kw_re.match(text):
                            false_positive_counts[kw] += 1
                            false_positive_examples.setdefault(kw, (title, text[:80]))

                page_lines = []

    print(f"{total_redirects} <redirect>-tagged page(s) scanned\n", file=sys.stderr)
    if counts:
        print("Keyword     Count   Example")
        for norm, n in counts.most_common():
            title, preview = examples[norm]
            print(f"{norm:<12}{n:<8}{title!r}: {preview!r}")
            variants = case_variants[norm]
            if len(variants) > 1:
                variant_str = ', '.join(f'{v} x{c}' for v, c in variants.most_common())
                print(f"             (case variants: {variant_str})")

        top = counts.most_common(1)[0][0]
        print(f"\nMost likely genuine keyword: {top!r} ({counts[top]} of {total_redirects} "
              f"redirects use it, case-insensitive)", file=sys.stderr)
        others = [k for k in counts if k != top]
        if others:
            print(f"Other DISTINCT keyword(s) seen (case variants already merged above) -- "
                  f"worth a manual look before trusting, could be vandalism, an old "
                  f"deprecated alias, or a second valid alias: {others}",
                  file=sys.stderr)
    elif not check_keywords:
        print("No redirect keyword tokens found -- unexpected for a real dump; "
              "double-check --dump points at the right file.", file=sys.stderr)

    if check_keywords:
        print(f"\n=== False-positive check for {sorted(check_keywords)} on this wiki ===",
              file=sys.stderr)
        if not false_positive_counts:
            print("None found: no non-redirect page's first line matched "
                  "'#keyword ... [[...]]' for any of the checked keywords. No evidence "
                  "of a collision on this dump -- not a guarantee for wikis you haven't "
                  "checked, but a real, verified data point for this one.", file=sys.stderr)
        else:
            for kw, n in false_positive_counts.most_common():
                title, preview = false_positive_examples[kw]
                print(f"  {kw!r}: {n} non-redirect page(s) matched the redirect shape -- "
                      f"e.g. {title!r}: {preview!r}", file=sys.stderr)
            print("These look like false positives: MediaWiki itself does NOT consider "
                  "them redirects, but they'd be misclassified as one if this keyword "
                  "were enabled while processing this wiki. Worth reading the example(s) "
                  "before adding this keyword to a shared, global list.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)

    api_p = sub.add_parser('api', help="query each wiki's own API directly")
    api_p.add_argument('langs', nargs='+',
                        help='Wikipedia language codes, e.g. ps sd pnb skr ur')
    api_p.set_defaults(func=cmd_api)

    dump_p = sub.add_parser('dump', help='scan a dump you already have on hand')
    dump_p.add_argument('dump', help='path to a .xml/.xml.bz2/.xml.gz dump')
    dump_p.add_argument('--check-false-positives', nargs='+', metavar='KEYWORD',
                         help="check whether any of these candidate keywords (e.g. from "
                              "another language) would misfire as a redirect on THIS "
                              "wiki's non-redirect pages -- the direct way to test the "
                              "cross-language collision risk instead of guessing at it")
    dump_p.set_defaults(func=cmd_dump)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
