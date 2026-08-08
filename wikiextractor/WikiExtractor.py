#!/usr/bin/env python
# -*- coding: utf-8 -*-

# =============================================================================
#  Version: 3.0 (January 24, 2023)
#  Author: Giuseppe Attardi (attardi@di.unipi.it), University of Pisa
#
#  Contributors:
#   Antonio Fuschetto (fuschett@aol.com)
#   Leonardo Souza (lsouza@amtera.com.br)
#   Juan Manuel Caicedo (juan@cavorite.com)
#   Humberto Pereira (begini@gmail.com)
#   Siegfried-A. Gevatter (siegfried@gevatter.com)
#   Pedro Assis (pedroh2306@gmail.com)
#   Wim Muskee (wimmuskee@gmail.com)
#   Radics Geza (radicsge@gmail.com)
#   Nick Ulven (nulven@github)
#
# =============================================================================
#  Copyright (c) 2009-2023. Giuseppe Attardi (attardi@di.unipi.it).
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

"""Wikipedia Extractor:
Extracts and cleans text from a Wikipedia database dump and stores output in a
number of files of similar size in a given directory.
Each file will contain several documents in the format:

    <doc id="" url="" title="">
        ...
        </doc>

If the program is invoked with the --json flag, then each file will                                            
contain several documents formatted as json ojects, one per line, with                                         
the following structure

    {"id": "", "revid": "", "url": "", "title": "", "text": "..."}

If the program is invoked with --text, then the output will not include
the <doc> tags and will have just the content instead.  This is mutually
exclusinve with --json

The program performs template expansion by preprocesssng the whole dump and
collecting template definitions.
"""

import argparse
import bz2
import contextlib
import logging
import os.path
import platform
import re  # TODO use regex when it will be standard
import sys
import threading
import time
from io import StringIO
from multiprocessing import get_context, cpu_count
from timeit import default_timer

from .extract import Extractor, ignoreTag, define_template, \
    resolve_template_page, _DEFAULT_IGNORED_TAG_PATTERNS
from . import template_blob

# ===========================================================================

# Program version
__version__ = '3.0.8'

# Separate from extract.py's own 'wikiextractor.extract' logger (which
# covers the extraction mechanics themselves -- template substitution,
# link processing, etc., under --debug): this one covers map/reduce
# coordination -- per-page timing, queue dispatch, reducer progress,
# and worker/reducer liveness -- under --debug_map_reduce. Named and
# configured independently so either can be enabled without the other.
mapreduce_logger = logging.getLogger('wikiextractor.mapreduce')


def configure_mapreduce_logging(enabled):
    """
    Sets mapreduce_logger's level and gives it its own handler/format
    (including a timestamp, %(asctime)s -- the root logger's own
    format has none) with propagate=False, so its messages go out
    through this handler only, not duplicated via the root logger's.

    Must be called at the start of extract_process() and
    reduce_process() specifically, not just once in the parent: both
    are real multiprocessing.Process instances, and under the "spawn"
    start method (unlike "fork"), a child re-imports the module fresh
    and does NOT inherit a level set on the parent's already-running
    logger after import. Calling this at the top of each of those
    functions -- rather than passing a boolean into every individual
    logging call -- is what lets call sites just be unconditional
    mapreduce_logger.debug(...) calls throughout the rest of this file.
    """
    mapreduce_logger.setLevel(logging.DEBUG if enabled else logging.WARNING)
    if not mapreduce_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(levelname)s: %(asctime)s %(message)s'))
        mapreduce_logger.addHandler(handler)
    mapreduce_logger.propagate = False


def configure_root_logging(log_level):
    """
    Sets the root logger's own level and format -- same reasoning and
    same required call sites as configure_mapreduce_logging() above
    (start of extract_process() and reduce_process(), not just once in main()).

    On a spawn, main()'s own logging.basicConfig()/logger.setLevel() calls
    never ran in that process at all, so this call sets up the logging
    for the children processes.
    """
    logging.basicConfig(format='%(levelname)s: %(message)s')
    logging.getLogger().setLevel(log_level)

##
# The namespace used for template definitions
# It is the name associated with namespace key=10 in the siteinfo header.
templateNamespace = ''

##
# The namespace used for module definitions
# It is the name associated with namespace key=828 in the siteinfo header.
moduleNamespace = ''

# ----------------------------------------------------------------------
# Expand using WikiMedia API
# import json

# def expandTemplates(text):
#     """Expand templates invoking MediaWiki API"""
#     text = urlib.urlencodew(text)
#     base = urlbase[:urlbase.rfind('/')]
#     url = base + "/w/api.php?action=expandtemplates&format=json&text=" + text
#     exp = json.loads(urllib.urlopen(url))
#     return exp['expandtemplates']['*']

# ------------------------------------------------------------------------------
# Output


class NextFile():

    """
    Synchronous generation of next available file name.
    """

    filesPerDir = 100

    def __init__(self, path_name):
        self.path_name = path_name
        self.dir_index = -1
        self.file_index = -1

    def next(self):
        self.file_index = (self.file_index + 1) % NextFile.filesPerDir
        if self.file_index == 0:
            self.dir_index += 1
        dirname = self._dirname()
        if not os.path.isdir(dirname):
            os.makedirs(dirname)
        return self._filepath()

    def _dirname(self):
        char1 = self.dir_index % 26
        char2 = int(self.dir_index / 26) % 26
        return os.path.join(self.path_name, '%c%c' % (ord('A') + char2, ord('A') + char1))

    def _filepath(self):
        return '%s/wiki_%02d' % (self._dirname(), self.file_index)


class OutputSplitter():

    """
    File-like object, that splits output to multiple files of a given max size.
    """

    def __init__(self, nextFile, max_file_size=0, compress=True):
        """
        :param nextFile: a NextFile object from which to obtain filenames
            to use.
        :param max_file_size: the maximum size of each file.
        :para compress: whether to write data with bzip compression.
        """
        self.nextFile = nextFile
        self.compress = compress
        self.max_file_size = max_file_size
        self.file = self.open(self.nextFile.next())

    def reserve(self, size):
        if self.size + size > self.max_file_size:
            self.close()
            self.file = self.open(self.nextFile.next())

    def write(self, data):
        data_bytes = data.encode('utf-8')
        self.reserve(len(data_bytes))
        self.size += self.file.write(data_bytes)

    def close(self):
        self.file.close()

    def open(self, filename):
        self.size = 0
        if self.compress:
            return bz2.BZ2File(filename + '.bz2', 'w')
        else:
            return open(filename, 'wb')


# ----------------------------------------------------------------------
# READER

tagRE = re.compile(r'(.*?)<(/?\w+)[^>]*>(?:([^<]*)(<.*?>)?)?')
#                    1     2               3      4


def load_templates(file, output_file=None, encoding='utf-8', templates=None, redirects=None,
                    blob_builder=None, redirects_blob_builder=None, template_prefix=''):
    """
    Load templates from :param file:.
    :param output_file: file where to save templates and modules.
    :param templates: the {title: text} dict to populate -- defaults
        to a fresh, empty dict when not given (matching Extractor's
        own default), NOT any shared global; there is no longer a
        module-level `templates` for this function to reach into.
        A caller that wants access to the populated dict afterward
        (process_dump(), the --article path, tests) must pass its
        own dict here explicitly and keep using that same reference
        -- this function mutates it in place, same as
        define_template() itself always has, just one level up.
        Ignored when blob_builder is given.
    :param redirects: the {title: target_title} dict to populate --
        same defaulting and mutate-in-place behavior as templates
        above. Ignored when redirects_blob_builder is given.
    :param blob_builder: a template_blob.StreamingTemplateBlobBuilder
        to stream parsed templates directly into instead of
        populating `templates` -- for a real, full-scale dump, this
        avoids ever holding a full {title: text} dict in memory at
        all (see StreamingTemplateBlobBuilder's own docstring).
    :param redirects_blob_builder: the equivalent streaming builder
        for redirects, populated alongside blob_builder in the same
        pass. redirects is typically far smaller than templates, but
        it's read the same way (an ordinary .get() on every template
        expansion) and so is exposed to the identical fork/COW
        privatization growth confirmed for templates itself -- see
        template_blob.py's own module docstring.
    :param template_prefix: the already-known Template-namespace
        prefix (e.g. 'Template:'), if the caller already determined
        it from the real dump's own siteinfo -- process_dump()'s own,
        separate siteinfo scan does this before ever calling here.
        When not given (empty), falls back to self-bootstrapping it
        from the first colon-containing title encountered, same as
        previously, just via a local variable instead of the (now
        removed) Extractor.templatePrefix class attribute -- this
        function never constructs any Extractor itself, it only needs
        the prefix for its own page-filtering logic during loading.
    :return: (number of templates loaded, the template_prefix that was
        actually used -- either what was passed in, or whatever got
        self-bootstrapped). The caller threads this into each real
        Extractor(...) it constructs -- particularly the --article
        path, which has no separate siteinfo scan of its own and
        relies entirely on this return value.
    """
    global templateNamespace
    global moduleNamespace, modulePrefix
    modulePrefix = moduleNamespace + ':'
    if blob_builder is None and templates is None:
        templates = {}
    if redirects_blob_builder is None and redirects is None:
        redirects = {}
    articles = 0
    template_count = 0
    page = []
    inText = False
    if output_file:
        output = open(output_file, 'w', encoding=encoding)
    for line in file:
        #line = line.decode('utf-8')
        if '<' not in line:  # faster than doing re.search()
            if inText:
                page.append(line)
            continue
        m = tagRE.search(line)
        if not m:
            continue
        tag = m.group(2)
        if tag == 'page':
            page = []
        elif tag == 'title':
            title = m.group(3)
            if not output_file and not template_prefix:  # do not know it yet
                # we reconstruct it from the first title
                colon = title.find(':')
                if colon > 1:
                    templateNamespace = title[:colon]
                    template_prefix = title[:colon + 1]
            # FIXME: should reconstruct also moduleNamespace
        elif tag == 'text':
            tag_end = line.index('>', m.start(2))
            if line[tag_end - 1] == '/':
                # See the identical fix and comment in collect_pages()
                # -- self-closing <text .../> (a revision with no
                # content) previously left inText stuck True forever,
                # silently merging every subsequent line, including
                # the next template's own metadata and body, into
                # whatever entry was currently being built.
                continue
            inText = True
            line = line[m.start(3):m.end(3)]
            page.append(line)
            if m.lastindex == 4:  # open-close
                inText = False
        elif tag == '/text':
            if m.group(1):
                page.append(m.group(1))
            inText = False
        elif inText:
            page.append(line)
        elif tag == '/page':
            if title.startswith(template_prefix):
                if blob_builder is not None or redirects_blob_builder is not None:
                    result = resolve_template_page(title, page)
                    if result is not None:
                        kind, value = result
                        if kind == 'redirect':
                            redirects_blob_builder.add(title, value)
                        else:
                            blob_builder.add(title, value)
                else:
                    define_template(title, page, templates, redirects)
                template_count += 1
            # save templates and modules to file
            if output_file and (title.startswith(template_prefix) or
                                title.startswith(modulePrefix)):
                output.write('<page>\n')
                output.write('   <title>%s</title>\n' % title)
                output.write('   <ns>10</ns>\n')
                output.write('   <text>')
                for line in page:
                    output.write(line)
                output.write('</text>\n')
                output.write('</page>\n')
            page = []
            articles += 1
            if articles % 100000 == 0:
                logging.info("Preprocessed %d pages", articles)
    if output_file:
        output.close()
        logging.info("Saved %d templates to '%s'", template_count, output_file)
    return template_count, template_prefix


def decode_open(filename, mode='rt', encoding='utf-8'):
    """
    Open a file, decode and decompress, depending on extension `gz`, or 'bz2`.
    :param filename: the file to open.
    """
    ext = os.path.splitext(filename)[1]
    if ext == '.gz':
        import gzip
        return gzip.open(filename, mode, encoding=encoding)
    elif ext == '.bz2':
        return bz2.open(filename, mode=mode, encoding=encoding)
    else:
        return open(filename, mode, encoding=encoding)


def collect_pages(text):
    """
    :param text: the text of a wikipedia file dump.
    """
    # we collect individual lines, since str.join() is significantly faster
    # than concatenation
    page = []
    id = ''
    revid = ''
    ns = ''
    last_id = ''
    inText = False
    redirect = False
    for line in text:
        if '<' not in line:     # faster than doing re.search()
            if inText:
                page.append(line)
            continue
        m = tagRE.search(line)
        if not m:
            continue
        tag = m.group(2)
        if tag == 'page':
            page = []
            ns = ''
            redirect = False
        elif tag == 'id' and not id:
            id = m.group(3)
        elif tag == 'id' and id: # <revision> <id></id> </revision>
            revid = m.group(3)
        elif tag == 'title':
            title = m.group(3)
        elif tag == 'ns':
            ns = m.group(3)
        elif tag == 'redirect':
            redirect = True
        elif tag == 'text':
            tag_end = line.index('>', m.start(2))
            if line[tag_end - 1] == '/':
                # <text bytes="0" .../> -- a revision with no content
                # at all. No matching </text> will ever arrive for
                # this specific tag, since it's self-closing; treating
                # it as an ordinary, still-open text element (as
                # before this fix) left inText stuck True forever,
                # silently merging every subsequent line -- including
                # the next page's own metadata and content -- into
                # this (empty) page.
                continue
            inText = True
            line = line[m.start(3):m.end(3)]
            page.append(line)
            if m.lastindex == 4:  # open-close
                inText = False
        elif tag == '/text':
            if m.group(1):
                page.append(m.group(1))
            inText = False
        elif inText:
            page.append(line)
        elif tag == '/page':
            # Only the Main/Article namespace (ns=0) is ever wanted here --
            # not inferred from a colon in the title (that broke on
            # ordinary ns=0 titles that happen to contain one, e.g.
            # "Kill Bill: Volume 1" -- see issue #254), but read directly
            # from the page's own <ns> element, which is authoritative.
            if (ns == '0' and id != last_id and not redirect and
                    not title.startswith(templateNamespace)):
                yield (id, revid, title, page)
                last_id = id
            id = ''
            revid = ''
            ns = ''
            page = []
            inText = False
            redirect = False


def safe_qsize(queue):
    """
    Queue.qsize() is documented as an unreliable approximation in
    general, and specifically raises NotImplementedError on macOS due
    to a sem_getvalue() limitation there -- returns None instead of
    crashing the watchdog on that platform.
    """
    try:
        return queue.qsize()
    except NotImplementedError:
        return None


def get_memory_usage_mb(pid):
    """
    Reads the current resident set size (RSS -- actual physical memory
    currently in use, not virtual/reserved) for the given PID directly
    from /proc, in megabytes. Returns None if the process has already
    exited, if permission is denied, or on a non-Linux platform where
    /proc doesn't exist -- callers should treat None as "unknown", not
    as zero.
    """
    try:
        with open('/proc/%d/status' % pid) as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    # format: "VmRSS:\t    1632 kB"
                    kb = int(line.split()[1])
                    return kb / 1024
    except (FileNotFoundError, PermissionError, ValueError, ProcessLookupError):
        return None
    return None


def watchdog(jobs_queue, output_queue, reduce_proc, workers, stop_event, interval=60):
    """
    Periodically logs queue depths, process liveness, and memory usage,
    so a stalled run can be diagnosed even during long stretches with
    no per-page activity to log at all -- e.g. the mapper itself
    blocked on jobs_queue.put() because no worker is consuming, or
    reduce_process having been killed outright (an OOM kill, for
    instance, leaves no trace in the per-page logging at all: see the
    REDUCER_EXIT docstring in reduce_process() for why -- SIGKILL
    allows no Python-level cleanup, not even that). is_alive() queries
    actual OS process state directly, rather than inferring it from
    log silence. reduce_process's own memory usage is tracked
    specifically because that's where ordering_buffer lives -- an
    unbounded, growing figure there right up until it disappears
    (rather than a REDUCER_EXIT line) is direct, rather than inferred,
    evidence of an OOM kill.
    :param interval: seconds between checks.
    """
    while not stop_event.wait(interval):
        alive_workers = sum(1 for w in workers if w.is_alive())
        reduce_mem = get_memory_usage_mb(reduce_proc.pid) if reduce_proc.pid else None
        worker_mems = [m for m in (get_memory_usage_mb(w.pid) for w in workers if w.pid)
                       if m is not None]
        mapreduce_logger.debug(
            "WATCHDOG jobs_queue=%s output_queue=%s reduce_alive=%s "
            "reduce_mem_mb=%s workers_alive=%d/%d workers_total_mem_mb=%s",
            safe_qsize(jobs_queue), safe_qsize(output_queue), reduce_proc.is_alive(),
            "%.1f" % reduce_mem if reduce_mem is not None else "unknown",
            alive_workers, len(workers),
            "%.1f" % sum(worker_mems) if worker_mems else "unknown")


def load_and_compact_templates(template_file, input_file, input, template_prefix=''):
    """Loads templates and redirects (from template_file if given,
    else input_file itself) and compacts each into its own
    shared-memory-backed view.

    Returns (template_blob_ctx, redirects_blob_ctx, input, template_prefix)
    -- input is returned because, when template_file isn't given, this
    scans and then re-opens input_file itself, and the caller needs
    that updated handle; template_prefix is returned because, when the
    caller doesn't already know it (passes '' -- the --article path,
    which has no separate siteinfo scan of its own), this discovers it
    via load_templates()'s own self-bootstrap and the caller needs the
    result to construct real Extractor instances correctly.

    Streams parsed templates and redirects directly into their own
    template_blob.StreamingTemplateBlobBuilder rather than building
    full dicts first and compacting them as a separate pass -- at full
    EN scale (~900K templates), having both representations resident
    at once meant this function's own peak RSS was roughly double the
    size of the templates themselves, confirmed directly on a real,
    memory-constrained production run (mapper RSS: ~1.85GB after
    loading a plain dict, ~4.2GB once the blob was also built
    alongside it -- and that ~1.85GB never fully came back afterward
    either, even after the dict went out of scope and an explicit
    gc.collect(): CPython's own allocator only returns a fully-empty
    arena to the OS, and a dict with hundreds of thousands of
    individually-allocated strings, built while other, unrelated
    parsing allocations were happening at the same time, left enough
    live stragglers scattered across arenas that most of it stayed
    mapped). Streaming avoids the dict existing at all, so there's
    nothing separate left to free or that can fail to be freed -- the
    content blob's bytes are the only large allocation made, once,
    directly.

    redirects gets the identical treatment for a different reason:
    it's typically far smaller than templates, but it's read the same
    way -- an ordinary .get() on every template expansion -- and is
    exposed to the identical fork/COW privatization growth confirmed
    directly for templates itself (measured: 500K ordinary .get()
    calls against a 200K-entry plain dict left 26.9MB of Private_Dirty
    behind in a worker that never wrote to it directly at all).
    """
    builder = template_blob.StreamingTemplateBlobBuilder()
    redirects_builder = template_blob.StreamingTemplateBlobBuilder()
    template_load_start = default_timer()
    if template_file and os.path.exists(template_file):
        logging.info("Preprocessing '%s' to collect template definitions: this may take some time.", template_file)
        file = decode_open(template_file)
        template_count, template_prefix = load_templates(
            file, blob_builder=builder, redirects_blob_builder=redirects_builder,
            template_prefix=template_prefix)
        file.close()
    else:
        if input_file == '-':
            # can't scan then reset stdin; must error w/ suggestion to specify template_file
            raise ValueError("to use templates with stdin dump, must supply explicit template-file")
        logging.info("Preprocessing '%s' to collect template definitions: this may take some time.", input_file)
        template_count, template_prefix = load_templates(
            input, template_file, blob_builder=builder, redirects_blob_builder=redirects_builder,
            template_prefix=template_prefix)
        input.close()
        input = decode_open(input_file)
    logging.info("Loaded %d templates in %.1fs", template_count, default_timer() - template_load_start)

    # See template_blob.py's own module docstring for why compaction
    # itself is necessary (fork()'s copy-on-write doesn't protect a
    # dict of Python objects the way it looks like it should).
    # template_blob_ctx/redirects_blob_ctx get threaded through
    # explicitly to extract_process() below (and to the single-article
    # path in main()), which construct their own CompactedTemplates
    # views and pass them directly into each
    # Extractor(..., templates=..., redirects=...).
    compact_start = default_timer()
    template_blob_ctx = template_blob.compact_blobs(*builder.finish())
    redirects_blob_ctx = template_blob.compact_blobs(*redirects_builder.finish())
    logging.info("Compacted templates and redirects into shared memory in %.1fs",
                 default_timer() - compact_start)
    return template_blob_ctx, redirects_blob_ctx, input, template_prefix


def process_dump(input_file, template_file, out_file, file_size, file_compress,
                 process_count, html_safe, expand_templates=True, debug_map_reduce=False,
                 extractor_kwargs=None, log_level=logging.WARNING):
    """
    :param input_file: name of the wikipedia dump file; '-' to read from stdin
    :param template_file: optional file with template definitions.
    :param out_file: directory where to store extracted data, or '-' for stdout
    :param file_size: max size of each extracted file, or None for no max (one file)
    :param file_compress: whether to compress files with bzip.
    :param process_count: number of extraction processes to spawn.
    :html_safe: whether to convert entities in text to HTML.
    :param expand_templates: whether to expand templates.
    :param debug_map_reduce: enables mapreduce_logger's DEBUG-level
        messages (see configure_mapreduce_logging()) -- per-page
        timing, queue dispatch, reducer progress, watchdog status.
    :param extractor_kwargs: the CLI-derived subset of Extractor's own
        constructor arguments (keepLinks, HtmlFormatting, to_json,
        to_text, discard_empty, ignored_tag_patterns, and optionally
        acceptedNamespaces), built once by main()'s
        build_extractor_kwargs(). This function adds templatePrefix
        and knownNamespaces to a copy of it, once real siteinfo has
        been scanned below, and passes the complete result through to
        every worker and every Extractor(...) constructed anywhere in
        this run -- no piece of it is a shared global read implicitly
        by anything.
    :param log_level: the root logger's own level, as set up in
        main() -- passed through to reduce_process()/extract_process()
        so each can reapply it via configure_root_logging(), since
        main()'s own logging.basicConfig()/logger.setLevel() calls
        never run in a process started via "spawn" at all.
    """
    extractor_kwargs = dict(extractor_kwargs) if extractor_kwargs else {}

    global templateNamespace
    global moduleNamespace, modulePrefix

    urlbase = ''                # This is obtained from <siteinfo>
    known_namespaces = set(['Template'])
    template_prefix = ''

    input = decode_open(input_file)

    # collect siteinfo
    for line in input:
        line = line #.decode('utf-8')
        m = tagRE.search(line)
        if not m:
            continue
        tag = m.group(2)
        if tag == 'base':
            # discover urlbase from the xml dump file
            # /mediawiki/siteinfo/base
            base = m.group(3)
            urlbase = base[:base.rfind("/")]
        elif tag == 'namespace':
            known_namespaces.add(m.group(3))
            if re.search('key="10"', line):
                templateNamespace = m.group(3)
                template_prefix = templateNamespace + ':'
            elif re.search('key="828"', line):
                moduleNamespace = m.group(3)
                modulePrefix = moduleNamespace + ':'
        elif tag == '/siteinfo':
            break

    extractor_kwargs['templatePrefix'] = template_prefix
    extractor_kwargs['knownNamespaces'] = known_namespaces

    if expand_templates:
        template_blob_ctx, redirects_blob_ctx, input, template_prefix = load_and_compact_templates(
            template_file, input_file, input, template_prefix=template_prefix)
        extractor_kwargs['templatePrefix'] = template_prefix
    else:
        # nullcontext, not None -- keeps the with-statement below
        # uniform regardless of whether templates are in play at all,
        # rather than needing a separate branch for --no-templates.
        template_blob_ctx = contextlib.nullcontext((None, None))
        redirects_blob_ctx = contextlib.nullcontext((None, None))
    with template_blob_ctx as (_wrapper, template_blob_names), \
            redirects_blob_ctx as (_redirects_wrapper, redirects_blob_names):
        # process pages
        logging.info("Starting page extraction from %s.", input_file)
        extract_start = default_timer()

        # Parallel Map/Reduce:
        # - pages to be processed are dispatched to workers
        # - a reduce process collects the results, sort them and print them.
        # - worker processes for each available CPU
        #
        # A single, explicit context for every multiprocessing object
        # constructed below -- Process AND Queue/Value/Condition alike.
        #
        # "fork" specifically on non-Windows: real, measured startup
        # savings (workers inherit the already-imported module via
        # copy-on-write instead of each re-importing and
        # re-compiling it fresh).
        # Originally forced here to dodge a real MacOS pickle error
        #   TypeError: cannot pickle '_io.TextIOWrapper' object
        # but that particular issue is now fixed.
        #
        # "spawn" is Windows' only option
        # (get_context("fork") raises ValueError there).
        #
        # Deliberately NOT using the plain, top-level
        # Process/Queue/Value/Condition for any of these: those
        # default to the *platform's own* default context, which
        # happens to already match the explicit choice below on every
        # platform this currently runs on -- but that's true only
        # because it happens to be true today.
        #
        # It has already changed once in Python's own history (macOS's
        # own default flipped from fork to spawn in 3.8, which is why
        # the fork-forcing line above exists at all), and mixing an
        # object built under one context with a Process started under
        # a different one fails.  For example, this can produce:
        # "RuntimeError: A SemLock created in a fork context is being
        # shared with a process in a spawn context"
        mp_context = get_context("spawn") if platform.system().startswith("Windows") else get_context("fork")
        Process = mp_context.Process

        maxsize = 10 * process_count
        # output queue
        output_queue = mp_context.Queue(maxsize=maxsize)

        # Shared counter reduce_process updates every time it successfully
        # writes an ordinal out, so the mapper below can tell how far
        # ahead of the actually-written output it's gotten -- without this,
        # nothing stops the mapper from queueing (and workers from
        # completing) unboundedly many pages while reduce_process is stuck
        # waiting on one specific ordinal, e.g. a genuinely stuck or
        # extremely slow page: every other worker just keeps racing ahead,
        # and every one of their completed results piles up in
        # reduce_process's own ordering_buffer, which has no size limit at
        # all. This bounds how far ahead the pipeline is allowed to get,
        # at the mapper (job-dispatch) side specifically -- NOT inside
        # reduce_process itself, since reduce_process must keep draining
        # output_queue unconditionally to have any chance of ever finding
        # the specific ordinal it's waiting for (a plain multiprocessing
        # Queue only supports FIFO reads, with no way to selectively wait
        # for one specific item while ignoring others ahead of it in the
        # queue -- pausing reduce_process's own consumption was tried and
        # reverted after it produced a genuine deadlock in testing).
        next_ordinal_shared = mp_context.Value('l', 0)

        # Notified by reduce_process every time next_ordinal_shared
        # advances, so the mapper's throttle below can genuinely wake up
        # on that specific event, rather than polling the value on some
        # fixed interval regardless of whether anything happened.
        progress_condition = mp_context.Condition()

        # Reduce job that sorts and prints output
        reduce = Process(target=reduce_process,
                          args=(output_queue, out_file, file_size, file_compress, next_ordinal_shared,
                                progress_condition, debug_map_reduce, log_level))
        reduce.start()

        # initialize jobs queue
        jobs_queue = mp_context.Queue(maxsize=maxsize)

        # start worker processes
        logging.info("Using %d extract processes.", process_count)
        workers = []
        for _ in range(max(1, process_count)):
            extractor = Process(target=extract_process,
                                args=(jobs_queue, output_queue, html_safe, debug_map_reduce,
                                      template_blob_names, redirects_blob_names, extractor_kwargs,
                                      log_level))
            extractor.daemon = True  # only live while parent process lives
            extractor.start()
            workers.append(extractor)

        watchdog_stop = threading.Event()
        watchdog_thread = None
        if mapreduce_logger.isEnabledFor(logging.DEBUG):
            watchdog_thread = threading.Thread(
                target=watchdog, args=(jobs_queue, output_queue, reduce, workers, watchdog_stop),
                daemon=True)
            watchdog_thread.start()

        # Mapper process

        # we collect individual lines, since str.join() is significantly faster
        # than concatenation

        ordinal = 0  # page count
        # How far ahead of the last actually-written ordinal the mapper is
        # willing to get before pausing -- matches the same maxsize
        # convention already used for the queues themselves, so this stays
        # proportional to process_count.
        for id, revid, title, page in collect_pages(input):
            while ordinal - next_ordinal_shared.value > maxsize:
                # progress_condition is notified by reduce_process every
                # time next_ordinal_shared actually advances (see there),
                with progress_condition:
                    progress_condition.wait(timeout=1.0)
            job = (id, revid, urlbase, title, page, ordinal)
            jobs_queue.put(job)  # goes to any available extract_process
            mapreduce_logger.debug("JOB_QUEUED ordinal=%d id=%s title=%r", ordinal, id, title)
            ordinal += 1

        input.close()

        # signal termination
        for _ in workers:
            jobs_queue.put(None)
        # wait for workers to terminate
        for w in workers:
            w.join()

        # signal end of work to reduce process
        output_queue.put(None)
        # wait for it to finish
        reduce.join()

        watchdog_stop.set()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=5)

        # Every worker and the reducer have now exited (w.join()/reduce.join()
        # above already waited for that) -- safe for the with-block above
        # to release the shared memory for good on exit (close() + unlink()
        # via CompactedTemplatesOwner.__exit__, or a no-op if this ran with
        # --no-templates via nullcontext).

    extract_duration = default_timer() - extract_start
    extract_rate = ordinal / extract_duration
    logging.info("Finished %d-process extraction of %d articles in %.1fs (%.1f art/s)",
                 process_count, ordinal, extract_duration, extract_rate)


# ----------------------------------------------------------------------
# Multiprocess support


def extract_process(jobs_queue, output_queue, html_safe, debug_map_reduce=False,
                     template_blob_names=None, redirects_blob_names=None, extractor_kwargs=None,
                     log_level=logging.WARNING):
    """Pull tuples of raw page content, do CPU/regex-heavy fixup, push finished text
    :param jobs_queue: where to get jobs.
    :param output_queue: where to queue extracted text for output.
    :html_safe: whether to convert entities in text to HTML.
    :param debug_map_reduce: configures this process's own copy of
        mapreduce_logger (see configure_mapreduce_logging()) -- when
        enabled, logs each page's wall-clock start/elapsed time (two
        lines per page: PAGE_START when a worker begins it, PAGE_TIMING
        when it finishes) -- useful for isolating a specific slow/stuck
        page when extraction seems to hang: sort PAGE_TIMING lines by
        elapsed time to spot an outlier directly, or, for a page that
        never finishes at all (no amount of waiting produces a matching
        PAGE_TIMING line for it), find that worker's PID's last
        PAGE_START line -- that's exactly the page it's stuck on, with
        no need to infer it from surrounding pages or wait to see
        whether it was "just slow".
    :param template_blob_names: :param redirects_blob_names: names of
        the shared-memory segments built by template_blob.compact() in
        process_dump(), or None if extraction is running with
        --no-templates. Attached here, once, at worker startup, inside
        a with-block so this worker's own view is closed automatically
        when the loop below exits (never unlinked, though -- that's
        the creator's job alone via CompactedTemplatesOwner, since a
        worker unlinking the shared segment would destroy it out from
        under any sibling worker still using it). The resulting views
        are passed explicitly into every Extractor(...) constructed
        below -- not assigned to the extract module's own `templates`/
        `redirects` globals, which used to be how a worker's source
        for each got set up. Passing them as constructor arguments
        means extract.py's own behavior is visible in extract.py
        itself (Extractor takes templates/redirects arguments) rather
        than being silently swappable only from here; it's also what's
        actually required once this worker starts being launched via
        spawn instead of fork, which inherits nothing implicitly at
        all -- doing it this way now means extract_process()'s own
        body doesn't need to change shape later for that transition.
    :param extractor_kwargs: the rest of Extractor's own constructor
        arguments (templatePrefix, knownNamespaces, acceptedNamespaces,
        ignored_tag_patterns, keepLinks, keepSections, HtmlFormatting,
        to_json, to_text, discard_empty), built once by process_dump()
        and passed through unchanged here -- same reasoning as
        template_blob_names/redirects_blob_names above: every one of
        these used to be a module- or class-level global a worker
        picked up implicitly (several genuinely broken that way -- see
        Extractor's own docstring), now plain, explicit arguments.
    :param log_level: this process's own root logger level (see
        configure_root_logging()) -- without this, template/#expr
        warnings still show (they're already at WARNING, Python's own
        default), but this worker's part of any INFO-level diagnostics
        would silently be lost under "spawn", same underlying cause as
        debug_map_reduce/mapreduce_logger above.
    """
    extractor_kwargs = extractor_kwargs or {}
    configure_root_logging(log_level)
    configure_mapreduce_logging(debug_map_reduce)
    worker_ctx = (template_blob.attach(template_blob_names) if template_blob_names is not None
                  else contextlib.nullcontext(None))
    redirects_ctx = (template_blob.attach(redirects_blob_names) if redirects_blob_names is not None
                      else contextlib.nullcontext(None))
    with worker_ctx as worker_templates, redirects_ctx as worker_redirects:
        while True:
            job = jobs_queue.get()  # job is (id, revid, urlbase, title, page, ordinal)
            if job:
                page_id, _, _, title, _, ordinal = job
                start = time.time()
                mapreduce_logger.debug("PAGE_START pid=%d ordinal=%d id=%s title=%r",
                                       os.getpid(), ordinal, page_id, title)
                out = StringIO()  # memory buffer
                Extractor(*job[:-1], templates=worker_templates,
                          redirects=worker_redirects, **extractor_kwargs).extract(out, html_safe)  # (id, urlbase, title, page)
                finish = time.time()
                mapreduce_logger.debug(
                    "PAGE_TIMING pid=%d ordinal=%d id=%s title=%r elapsed=%.2fs",
                    os.getpid(), ordinal, page_id, title, finish - start)
                text = out.getvalue()
                output_queue.put((job[-1], text))  # (ordinal, extracted_text)
                out.close()
            else:
                break


def reduce_process(output_queue, out_file, file_size, file_compress, next_ordinal_shared, progress_condition,
                    debug_map_reduce=False, log_level=logging.WARNING):
    """
    Pull finished article text, write series of files (or stdout)
    :param output_queue: text to be output.
    :param out_file: path to write output to, or '-' for stdout.
    :param file_size: max size per output file (see OutputSplitter).
    :param file_compress: whether to bzip2-compress output files.
    :param next_ordinal_shared: multiprocessing.Value updated
        every time next_ordinal advances, so another process (the
        job-dispatching mapper) can throttle itself based on how far
        ahead it's gotten
    :param progress_condition: multiprocessing.Condition notified every
        time next_ordinal_shared advances, so the mapper's throttle can
        wake up on that specific event instead of polling the value.
    :param debug_map_reduce: configures this process's own copy of
        mapreduce_logger (see configure_mapreduce_logging()) -- when
        enabled, logs REDUCER_PROGRESS for every page written (ordinal,
        current buffer depth) -- shares the same logger as the workers'
        PAGE_START/PAGE_TIMING logging.
    :param log_level: this process's own root logger level (see
        configure_root_logging()) -- without this, every ordinary
        logging.info()/logging.warning() call below (progress lines,
        REDUCER_EXIT status) silently stops appearing under "spawn",
        since main()'s own level-setting code never runs in this
        process at all.

    Builds its own output/OutputSplitter here, rather than receiving
    an already-open one constructed by the parent process: an open
    file object (or BZ2File) generally can't be pickled, which this
    codebase previously worked around by forcing the "fork" process-
    start method elsewhere in this file -- a method that isn't even
    available on Windows at all. Accepting only plain, trivially
    picklable values here instead removes the need for that
    workaround, and this process opening (and, importantly, properly
    closing) the file itself is also what fixes a separate real bug:
    see the comment on close() below.

    On exit, always logs the status of the exit (except in the case
    of a SIGKILL)
    """
    configure_root_logging(log_level)
    configure_mapreduce_logging(debug_map_reduce)
    if out_file == '-':
        output = sys.stdout
        if file_compress:
            logging.warning("writing to stdout, so no output compression "
                             "(use an external tool)")
    else:
        nextFile = NextFile(out_file)
        output = OutputSplitter(nextFile, file_size, file_compress)

    interval_start = default_timer()
    period = 100000
    # FIXME: use a heap
    ordering_buffer = {}  # collected pages
    next_ordinal = 0  # sequence number of pages
    try:
        while True:
            if next_ordinal in ordering_buffer:
                output.write(ordering_buffer.pop(next_ordinal))
                next_ordinal += 1
                with progress_condition:
                    next_ordinal_shared.value = next_ordinal
                    progress_condition.notify_all()
                mapreduce_logger.debug("REDUCER_PROGRESS ordinal=%d buffered=%d",
                                       next_ordinal - 1, len(ordering_buffer))
                # progress report
                if next_ordinal % period == 0:
                    interval_rate = period / (default_timer() - interval_start)
                    logging.info("Extracted %d articles (%.1f art/s)",
                                 next_ordinal, interval_rate)
                    interval_start = default_timer()
            else:
                # mapper puts None to signal finish
                pair = output_queue.get()
                if not pair:
                    break
                ordinal, text = pair
                ordering_buffer[ordinal] = text
    finally:
        # Always logged (on the root logger, not gated by
        # --debug_map_reduce at all): whether this
        # process is exiting cleanly (every buffered page successfully
        # written, nothing left over) or with pages still stuck in
        # ordering_buffer -- which would mean the sentinel arrived
        # while next_ordinal's own entry had still never shown up on
        # output_queue at all, a real, distinct problem from a page
        # merely being slow. If this process gets SIGKILLed instead of
        # exiting through this path at all, this line simply never
        # appears -- the absence of it, following the last
        # REDUCER_PROGRESS line, is itself the signal.
        if ordering_buffer:
            logging.warning(
                "REDUCER_EXIT incomplete: next_ordinal=%d, %d page(s) still "
                "buffered and never written: ordinals=%s",
                next_ordinal, len(ordering_buffer),
                sorted(ordering_buffer.keys())[:20])
        else:
            logging.info("REDUCER_EXIT clean: wrote %d article(s) total",
                         next_ordinal)
        # This process's own writes are buffered in its own memory.
        # Since this process now opens its own output itself (rather
        # than receiving an already-open copy from the parent), there
        # is no other copy anywhere that could flush this one's
        # buffered data -- without this, its last buffered write(s)
        # are simply lost when it exits, with no error at all,
        # reliably dropping exactly the last page from every dump.
        if output != sys.stdout:
            output.close()


# ----------------------------------------------------------------------

# Minimum size of output files
minFileSize = 200 * 1024


def main():
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]),
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description=__doc__)
    parser.add_argument("input",
                        help="XML wiki dump file")
    groupO = parser.add_argument_group('Output')
    groupO.add_argument("-o", "--output", default="text",
                        help="directory for extracted files (or '-' for dumping to stdout)")
    groupO.add_argument("-b", "--bytes", default="1M",
                        help="maximum bytes per output file (default %(default)s); 0 means to put a single article per file",
                        metavar="n[KMG]")
    groupO.add_argument("-c", "--compress", action="store_true",
                        help="compress output files using bzip")
    groupOFormat = groupO.add_mutually_exclusive_group()
    groupOFormat.add_argument("--json", action="store_true",
                              help="write output in json format instead of the default <doc> format")
    groupOFormat.add_argument("--text", action="store_true",
                              help="write output in text format (body only, no title) instead of the default <doc> format")
    groupO.add_argument("--discard_empty", action="store_true",
                        help="discard empty articles (such as redirects) rather than writing just the title")

    groupP = parser.add_argument_group('Processing')
    groupP.add_argument("--html", action="store_true",
                        help="produce HTML output, subsumes --links")
    groupP.add_argument("-l", "--links", action="store_true",
                        help="preserve links")
    groupP.add_argument("-ns", "--namespaces", default="", metavar="ns1,ns2",
                        help="accepted namespaces")
    groupP.add_argument("--templates",
                        help="use or create file containing templates")
    groupP.add_argument("--no-templates", action="store_true",
                        help="Do not expand templates")
    groupP.add_argument("--html-safe", default=True,
                        help="use to produce HTML safe output within <doc>...</doc>")
    default_process_count = cpu_count() - 1
    parser.add_argument("--processes", type=int, default=default_process_count,
                        help="Number of processes to use (default %(default)s)")

    groupS = parser.add_argument_group('Special')
    groupS.add_argument("-q", "--quiet", action="store_true",
                        help="suppress reporting progress info")
    groupS.add_argument("--debug", action="store_true",
                        help="print debug info")
    groupS.add_argument("--debug_map_reduce", action="store_true",
                        help="enable map/reduce coordination diagnostics: PAGE_START "
                             "and PAGE_TIMING (elapsed) for every extracted page, "
                             "JOB_QUEUED for every dispatched page, REDUCER_PROGRESS "
                             "for every page written, and a periodic WATCHDOG status "
                             "line (queue depths, memory, process liveness). "
                             "Independent of --debug (which covers extraction "
                             "mechanics in extract.py instead), and far less noisy. "
                             "A page that never finishes still leaves its PAGE_START "
                             "line behind, so a stuck worker's last page is "
                             "identifiable even without waiting for it to complete: "
                             "sort PAGE_TIMING lines by elapsed time to spot an "
                             "outlier, or find a PID's dangling PAGE_START.")
    groupS.add_argument("-a", "--article", action="store_true",
                        help="analyze a file containing a single article (debug option)")
    groupS.add_argument("-v", "--version", action="version",
                        version='%(prog)s ' + __version__,
                        help="print program version")

    args = parser.parse_args()

    keepLinks = args.links
    if args.html:
        keepLinks = True
    extractor_kwargs = {
        'keepLinks': keepLinks,
        'HtmlFormatting': args.html,
        'to_json': args.json,
        'to_text': args.text,
        'discard_empty': args.discard_empty,
        # Default set plus 'a' when links aren't being kept -- built
        # explicitly, once, per run, rather than the old approach of
        # mutating extract.py's own module-level list at import time
        # (ignoreTag() is a pure function now: it returns the compiled
        # pattern instead of appending it anywhere).
        'ignored_tag_patterns': (list(_DEFAULT_IGNORED_TAG_PATTERNS) +
                                  ([] if keepLinks else [ignoreTag('a')])),
    }
    if args.namespaces:
        extractor_kwargs['acceptedNamespaces'] = set(args.namespaces.split(','))

    try:
        power = 'kmg'.find(args.bytes[-1].lower()) + 1
        # 0 bytes means put a single article per file.
        file_size = 0 if args.bytes == '0' else int(args.bytes[:-1]) * 1024 ** power
        if file_size and file_size < minFileSize:
            raise ValueError()
    except ValueError:
        logging.error('Insufficient or invalid size: %s', args.bytes)
        return

    FORMAT = '%(levelname)s: %(message)s'
    logging.basicConfig(format=FORMAT)

    logger = logging.getLogger()
    log_level = logging.WARNING  # Python's own default
    if not args.quiet:
        log_level = logging.INFO
    if args.debug:
        log_level = logging.DEBUG
    logger.setLevel(log_level)

    if args.json:
        logger.debug("Outputting to json format")
    elif args.text:
        logger.debug("Outputting to text format")
    else:
        logger.debug("Outputting to <doc> format")

    input_file = args.input

    if args.article:
        def compact_article_templates(templates_path):
            """Streams directly into blob builders, same as
            load_and_compact_templates() -- matches real behavior more
            closely, and --article exists partly to validate that.
            Returns (owner, owner, template_prefix) -- the discovered
            prefix from load_templates()'s own self-bootstrap, since
            this path has no separate siteinfo scan of its own."""
            builder = template_blob.StreamingTemplateBlobBuilder()
            redirects_builder = template_blob.StreamingTemplateBlobBuilder()
            with decode_open(templates_path) as file:
                _count, prefix = load_templates(file, blob_builder=builder,
                                                 redirects_blob_builder=redirects_builder)
            return (template_blob.compact_blobs(*builder.finish()),
                    template_blob.compact_blobs(*redirects_builder.finish()),
                    prefix)

        article_extractor_kwargs = dict(extractor_kwargs)
        if args.templates and os.path.exists(args.templates):
            # Same compaction as process_dump() -- deliberately not a
            # separate plain-dict path for --article just because
            # there's no forking here to protect against: --article
            # exists partly to validate real behavior, and exercising
            # a different templates lookup mechanism here than what
            # real multiprocess runs use would defeat that.
            article_templates_ctx, article_redirects_ctx, template_prefix = \
                compact_article_templates(args.templates)
            article_extractor_kwargs['templatePrefix'] = template_prefix
        else:
            article_templates_ctx = contextlib.nullcontext((None, None))
            article_redirects_ctx = contextlib.nullcontext((None, None))

        with article_templates_ctx as (article_templates, _names), \
                article_redirects_ctx as (article_redirects, _redirects_names):
            urlbase = ''
            with decode_open(input_file) as input:
                for id, revid, title, page in collect_pages(input):
                    Extractor(id, revid, urlbase, title, page,
                              templates=article_templates,
                              redirects=article_redirects,
                              **article_extractor_kwargs).extract(sys.stdout)
        return

    output_path = args.output
    if output_path != '-' and not os.path.isdir(output_path):
        try:
            os.makedirs(output_path)
        except:
            logging.error('Could not create: %s', output_path)
            return

    configure_mapreduce_logging(args.debug_map_reduce)
    process_dump(input_file, args.templates, output_path, file_size,
                 args.compress, args.processes, args.html_safe, not args.no_templates,
                 args.debug_map_reduce, extractor_kwargs=extractor_kwargs, log_level=log_level)

if __name__ == '__main__':
    main()
