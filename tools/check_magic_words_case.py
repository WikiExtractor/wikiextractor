#!/usr/bin/env python3
"""Audit a MediaWiki dump for magic-word variable spellings.

expandTemplate() resolves a variable name case sensitively: {{PAGENAME}}
is the variable, {{Pagename}} and {{pagename}} name a template. This
reports whether a dump contains anything that distinguishes the two, so
the rule can be checked at full-dump scale without extracting twice.

Two questions, one streaming pass:

  Off-case invocations. An invocation whose name matches a variable
  only when case is ignored -- {{Pagename}}, {{subpagename}}. These are
  the only places case-sensitive and case-insensitive matching can
  differ, so a count of zero means the rule changes nothing on this
  dump. Each one found is a site to inspect: case-insensitively it
  resolves as a variable, case sensitively it is a template lookup.

  Shadowed templates. Pages in the template namespace whose name
  matches a variable when case is ignored. Case-insensitive matching
  would swallow every invocation of these, so they are what the rule
  protects.

Both are counted separately for article and template pages, since an
invocation inside a template body counts the same as one in an article.

Usage:
    python3 check_magic_word_case.py DUMP [DUMP ...]

DUMP may be .xml, .xml.bz2 or .xml.gz, and is streamed rather than
read into memory.

Limitation: this is a static scan. A name assembled at expansion time,
as in {{ {{{1}}} }}, is invisible to it; only the extractor sees those.
"""

import argparse
import bz2
import collections
import gzip
import os
import re
import sys

try:
    from wikiextractor.extract import MagicWords
    VARIABLE_IDS = list(MagicWords.names)
except ImportError:
    sys.exit("ERROR: wikiextractor must be importable (put it on $PYTHONPATH).")

# Canonical spellings, which are what a page has to write for the
# extractor to treat the name as a variable.
CANONICAL = {name.upper() for name in VARIABLE_IDS}
# Keyed by uppercase so an invocation of any casing can be looked up.
BY_UPPER = {name.upper(): name for name in VARIABLE_IDS}

TITLE_RE = re.compile(r'<title>(.*?)</title>', re.DOTALL)
NS_RE = re.compile(r'<ns>(-?\d+)</ns>')
# A bare {{Name}} or {{Name|...}}. ':' is excluded so parser functions
# and namespaced transclusions are left out, '#' so are the branching
# functions -- neither is ever resolved as a variable.
INVOCATION_RE = re.compile(r'\{\{\s*([^|}{#:\n]{1,60}?)\s*[|}]')


def open_dump(path):
    if path.endswith('.bz2'):
        return bz2.open(path, 'rt', encoding='utf-8', errors='replace')
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')


def scan(path, examples_per_spelling=3):
    exact = collections.Counter()
    offcase = collections.Counter()
    offcase_where = collections.defaultdict(list)
    shadowed = []
    pages = 0
    title = ''
    ns = ''

    with open_dump(path) as handle:
        for line in handle:
            if '<' in line:
                m = TITLE_RE.search(line)
                if m:
                    title = m.group(1)
                m = NS_RE.search(line)
                if m:
                    ns = m.group(1)
                if '</page>' in line:
                    pages += 1
                    # A template page whose own name collides with a
                    # variable is what case-insensitive matching would
                    # have swallowed.
                    if ns == '10' and ':' in title:
                        bare = title.split(':', 1)[1]
                        if bare.upper() in CANONICAL:
                            shadowed.append(title)
            if '{{' not in line:
                continue
            for m in INVOCATION_RE.finditer(line):
                name = m.group(1).strip()
                upper = name.upper()
                if upper not in CANONICAL:
                    continue
                if name == upper:
                    exact[name] += 1
                else:
                    offcase[name] += 1
                    where = offcase_where[name]
                    if len(where) < examples_per_spelling and title not in where:
                        where.append(title)

    return {'path': path, 'pages': pages, 'exact': exact, 'offcase': offcase,
            'offcase_where': offcase_where, 'shadowed': shadowed}


def report(result, show):
    exact, offcase = result['exact'], result['offcase']
    print('=' * 72)
    print('%s  (%d pages)' % (os.path.basename(result['path']), result['pages']))
    print('  canonical uppercase : %6d invocations, %d distinct'
          % (sum(exact.values()), len(exact)))
    print('  off-case            : %6d invocations, %d distinct'
          % (sum(offcase.values()), len(offcase)))

    if offcase:
        print('\n  OFF-CASE SPELLINGS -- case-sensitive matching treats each of')
        print('  these as a template rather than as a variable:')
        for name, n in offcase.most_common(show):
            print('    %-24s x%-7d canonical: %s' % (name, n, name.upper()))
            for where in result['offcase_where'][name]:
                print('        on: %s' % where)
        if len(offcase) > show:
            print('    ... and %d more distinct spellings' % (len(offcase) - show))
    else:
        print('\n  No off-case invocations: case-sensitive and case-insensitive')
        print('  matching resolve identically on this dump.')

    if result['shadowed']:
        print('\n  TEMPLATE PAGES NAMED AFTER A VARIABLE -- case-insensitive')
        print('  matching would swallow every invocation of these:')
        for t in result['shadowed'][:show]:
            print('    %s' % t)
        if len(result['shadowed']) > show:
            print('    ... and %d more' % (len(result['shadowed']) - show))
    else:
        print('\n  No template page collides with a variable name.')

    if exact:
        print('\n  Variables actually used (canonical spelling):')
        for name, n in exact.most_common(show):
            print('    %-24s x%d' % (name, n))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dumps', nargs='+', help='dump file(s): .xml, .xml.bz2 or .xml.gz')
    ap.add_argument('--show', type=int, default=25,
                    help='how many entries to list per section (default %(default)s)')
    args = ap.parse_args(argv)

    totals = {'exact': collections.Counter(), 'offcase': collections.Counter(),
              'shadowed': 0, 'pages': 0}
    for path in args.dumps:
        result = scan(path)
        report(result, args.show)
        totals['exact'] += result['exact']
        totals['offcase'] += result['offcase']
        totals['shadowed'] += len(result['shadowed'])
        totals['pages'] += result['pages']

    if len(args.dumps) > 1:
        print('=' * 72)
        print('ALL DUMPS: %d pages, %d canonical, %d off-case, %d shadowed templates'
              % (totals['pages'], sum(totals['exact'].values()),
                 sum(totals['offcase'].values()), totals['shadowed']))

    # Non-zero exit when something needs a human to look at it.
    return 1 if totals['offcase'] else 0


if __name__ == '__main__':
    sys.exit(main())
