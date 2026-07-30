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
import logging
import os.path
import re  # TODO use regex when it will be standard
import sys
import threading
import time
from io import StringIO
from multiprocessing import Queue, get_context, cpu_count
from timeit import default_timer

from .extract import Extractor, ignoreTag, define_template, acceptedNamespaces

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
    logger after import -- confirmed directly (a spawned child's
    logger showed effective level WARNING, not DEBUG, despite the
    parent having set DEBUG on the same named logger beforehand).
    Calling this at the top of each of those functions -- rather than
    passing a boolean into every individual logging call -- is what
    lets call sites just be unconditional mapreduce_logger.debug(...)
    calls throughout the rest of this file.
    """
    mapreduce_logger.setLevel(logging.DEBUG if enabled else logging.WARNING)
    if not mapreduce_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(levelname)s: %(asctime)s %(message)s'))
        mapreduce_logger.addHandler(handler)
    mapreduce_logger.propagate = False

##
# Defined in <siteinfo>
# We include as default Template, when loading external template file.
knownNamespaces = set(['Template'])

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
        if self.file.tell() + size > self.max_file_size:
            self.close()
            self.file = self.open(self.nextFile.next())

    def write(self, data):
        self.reserve(len(data))
        if self.compress:
            self.file.write(data)
        else:
            self.file.write(data)

    def close(self):
        self.file.close()

    def open(self, filename):
        if self.compress:
            return bz2.BZ2File(filename + '.bz2', 'w')
        else:
            return open(filename, 'w')


# ----------------------------------------------------------------------
# READER

tagRE = re.compile(r'(.*?)<(/?\w+)[^>]*>(?:([^<]*)(<.*?>)?)?')
#                    1     2               3      4


def load_templates(file, output_file=None):
    """
    Load templates from :param file:.
    :param output_file: file where to save templates and modules.
    :return: number of templates loaded.
    """
    global templateNamespace
    global moduleNamespace, modulePrefix
    modulePrefix = moduleNamespace + ':'
    articles = 0
    templates = 0
    page = []
    inText = False
    if output_file:
        output = open(output_file, 'w')
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
            if not output_file and not templateNamespace:  # do not know it yet
                # we reconstruct it from the first title
                colon = title.find(':')
                if colon > 1:
                    templateNamespace = title[:colon]
                    Extractor.templatePrefix = title[:colon + 1]
            # FIXME: should reconstruct also moduleNamespace
        elif tag == 'text':
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
            if title.startswith(Extractor.templatePrefix):
                define_template(title, page)
                templates += 1
            # save templates and modules to file
            if output_file and (title.startswith(Extractor.templatePrefix) or
                                title.startswith(modulePrefix)):
                output.write('<page>\n')
                output.write('   <title>%s</title>\n' % title)
                output.write('   <ns>10</ns>\n')
                output.write('   <text>')
                for line in page:
                    output.write(line)
                output.write('   </text>\n')
                output.write('</page>\n')
            page = []
            articles += 1
            if articles % 100000 == 0:
                logging.info("Preprocessed %d pages", articles)
    if output_file:
        output.close()
        logging.info("Saved %d templates to '%s'", templates, output_file)
    return templates


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
            redirect = False
        elif tag == 'id' and not id:
            id = m.group(3)
        elif tag == 'id' and id: # <revision> <id></id> </revision>
            revid = m.group(3)
        elif tag == 'title':
            title = m.group(3)
        elif tag == 'redirect':
            redirect = True
        elif tag == 'text':
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
            colon = title.find(':')
            if (colon < 0 or (title[:colon] in acceptedNamespaces) and id != last_id and
                    not redirect and not title.startswith(templateNamespace)):
                yield (id, revid, title, page)
                last_id = id
            id = ''
            revid = ''
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


def process_dump(input_file, template_file, out_file, file_size, file_compress,
                 process_count, html_safe, expand_templates=True, debug_map_reduce=False):
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
    """
    global knownNamespaces
    global templateNamespace
    global moduleNamespace, modulePrefix

    urlbase = ''                # This is obtained from <siteinfo>

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
            knownNamespaces.add(m.group(3))
            if re.search('key="10"', line):
                templateNamespace = m.group(3)
                Extractor.templatePrefix = templateNamespace + ':'
            elif re.search('key="828"', line):
                moduleNamespace = m.group(3)
                modulePrefix = moduleNamespace + ':'
        elif tag == '/siteinfo':
            break

    if expand_templates:
        # preprocess
        template_load_start = default_timer()
        if template_file and os.path.exists(template_file):
            logging.info("Preprocessing '%s' to collect template definitions: this may take some time.", template_file)
            file = decode_open(template_file)
            templates = load_templates(file)
            file.close()
        else:
            if input_file == '-':
                # can't scan then reset stdin; must error w/ suggestion to specify template_file
                raise ValueError("to use templates with stdin dump, must supply explicit template-file")
            logging.info("Preprocessing '%s' to collect template definitions: this may take some time.", input_file)
            templates = load_templates(input, template_file)
            input.close()
            input = decode_open(input_file)
        template_load_elapsed = default_timer() - template_load_start
        logging.info("Loaded %d templates in %.1fs", templates, template_load_elapsed)

    # process pages
    logging.info("Starting page extraction from %s.", input_file)
    extract_start = default_timer()

    # Parallel Map/Reduce:
    # - pages to be processed are dispatched to workers
    # - a reduce process collects the results, sort them and print them.

    # fixes MacOS error: TypeError: cannot pickle '_io.TextIOWrapper' object
    Process = get_context("fork").Process

    maxsize = 10 * process_count
    # output queue
    output_queue = Queue(maxsize=maxsize)

    # Reduce job that sorts and prints output
    reduce = Process(target=reduce_process,
                      args=(output_queue, out_file, file_size, file_compress, debug_map_reduce))
    reduce.start()

    # initialize jobs queue
    jobs_queue = Queue(maxsize=maxsize)

    # start worker processes
    logging.info("Using %d extract processes.", process_count)
    workers = []
    for _ in range(max(1, process_count)):
        extractor = Process(target=extract_process,
                            args=(jobs_queue, output_queue, html_safe, debug_map_reduce))
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
    for id, revid, title, page in collect_pages(input):
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

    extract_duration = default_timer() - extract_start
    extract_rate = ordinal / extract_duration
    logging.info("Finished %d-process extraction of %d articles in %.1fs (%.1f art/s)",
                 process_count, ordinal, extract_duration, extract_rate)


# ----------------------------------------------------------------------
# Multiprocess support


def extract_process(jobs_queue, output_queue, html_safe, debug_map_reduce=False):
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
    """
    configure_mapreduce_logging(debug_map_reduce)
    while True:
        job = jobs_queue.get()  # job is (id, revid, urlbase, title, page, ordinal)
        if job:
            page_id, _, _, title, _, ordinal = job
            start = time.time()
            mapreduce_logger.debug("PAGE_START pid=%d ordinal=%d id=%s title=%r",
                                   os.getpid(), ordinal, page_id, title)
            out = StringIO()  # memory buffer
            Extractor(*job[:-1]).extract(out, html_safe)  # (id, urlbase, title, page)
            finish = time.time()
            mapreduce_logger.debug(
                "PAGE_TIMING pid=%d ordinal=%d id=%s title=%r elapsed=%.2fs",
                os.getpid(), ordinal, page_id, title, finish - start)
            text = out.getvalue()
            output_queue.put((job[-1], text))  # (ordinal, extracted_text)
            out.close()
        else:
            break


def reduce_process(output_queue, out_file, file_size, file_compress, debug_map_reduce=False):
    """
    Pull finished article text, write series of files (or stdout)
    :param output_queue: text to be output.
    :param out_file: path to write output to, or '-' for stdout.
    :param file_size: max size per output file (see OutputSplitter).
    :param file_compress: whether to bzip2-compress output files.
    :param debug_map_reduce: configures this process's own copy of
        mapreduce_logger (see configure_mapreduce_logging()) -- when
        enabled, logs REDUCER_PROGRESS for every page written (ordinal,
        current buffer depth) -- shares the same logger as the workers'
        PAGE_START/PAGE_TIMING logging.

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
        # are simply lost when it exits, with no error at all.
        # Confirmed directly: this reliably dropped exactly the last
        # page from every dump tested, regardless of size.
        if output != sys.stdout:
            output.close()


# ----------------------------------------------------------------------

# Minimum size of output files
minFileSize = 200 * 1024


def main():
    global acceptedNamespaces
    global templateCache

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

    Extractor.keepLinks = args.links
    Extractor.HtmlFormatting = args.html
    if args.html:
        Extractor.keepLinks = True
    Extractor.to_json = args.json
    Extractor.to_text = args.text
    Extractor.discard_empty = args.discard_empty

    try:
        power = 'kmg'.find(args.bytes[-1].lower()) + 1
        # 0 bytes means put a single article per file.
        file_size = 0 if args.bytes == '0' else int(args.bytes[:-1]) * 1024 ** power
        if file_size and file_size < minFileSize:
            raise ValueError()
    except ValueError:
        logging.error('Insufficient or invalid size: %s', args.bytes)
        return

    if args.namespaces:
        acceptedNamespaces = set(args.namespaces.split(','))

    FORMAT = '%(levelname)s: %(message)s'
    logging.basicConfig(format=FORMAT)

    logger = logging.getLogger()
    if not args.quiet:
        logger.setLevel(logging.INFO)
    if args.debug:
        logger.setLevel(logging.DEBUG)

    if args.json:
        logger.debug("Outputting to json format")
    elif args.text:
        logger.debug("Outputting to text format")
    else:
        logger.debug("Outputting to <doc> format")

    input_file = args.input

    if not Extractor.keepLinks:
        ignoreTag('a')

    # sharing cache of parser templates is too slow:
    # manager = Manager()
    # templateCache = manager.dict()

    if args.article:
        if args.templates:
            if os.path.exists(args.templates):
                with open(args.templates) as file:
                    load_templates(file)

        urlbase = ''
        with open(input_file) as input:
            for id, revid, title, page in collect_pages(input):
                Extractor(id, revid, urlbase, title, page).extract(sys.stdout)
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
                 args.debug_map_reduce)

if __name__ == '__main__':
    main()
