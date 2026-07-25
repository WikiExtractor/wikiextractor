#!/usr/bin/env python3
"""
report_page_timing.py

Summarize --page_timing log output from WikiExtractor.py, so you don't
have to read through the full (much smaller than --debug, but still
substantial on a real run) log by hand.

Reports two things:
  1. The slowest pages that have actually finished so far, sorted by
     elapsed time -- the usual suspects for "why is this taking so
     long" even when nothing is fully stuck.
  2. Any worker that logged a PAGE_START with no matching PAGE_TIMING
     yet, AND the run as a whole hasn't finished -- i.e., a worker
     that's currently stuck on a specific page, right now. Safe to run
     against a log file that's still being actively written to.

Usage:
    python3 report_page_timing.py timing.log
    python3 report_page_timing.py timing.log --top 20
    python3 report_page_timing.py -                      # read from stdin
"""

import argparse
import re
import sys
import time


START_RE = re.compile(
    r'PAGE_START pid=(\d+) ordinal=(\d+) id=(\S+) title=(.*?) start=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*$'
)
TIMING_RE = re.compile(
    r'PAGE_TIMING pid=(\d+) ordinal=(\d+) id=(\S+) title=(.*?) '
    r'start=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) finish=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) '
    r'elapsed=([\d.]+)s\s*$'
)
FINISHED_RE = re.compile(r'Finished \d+-process extraction')


def parse_log(lines):
    """
    Returns (timings, in_progress):
      timings: list of dicts (pid, ordinal, id, title, start, finish,
               elapsed) -- one per page that has fully completed.
      in_progress: dict of pid -> dict (ordinal, id, title, start) --
               one per worker whose most recent PAGE_START has no
               matching PAGE_TIMING yet.
    Lines are processed in order, so a later PAGE_START for the same
    pid correctly replaces an earlier, already-completed one, and a
    matching PAGE_TIMING clears that pid's in-progress entry.
    """
    timings = []
    in_progress = {}
    finished_run = False

    for line in lines:
        if FINISHED_RE.search(line):
            finished_run = True
            continue

        m = TIMING_RE.search(line)
        if m:
            pid, ordinal, page_id, title, start, finish, elapsed = m.groups()
            pid = int(pid)
            timings.append({
                'pid': pid, 'ordinal': int(ordinal), 'id': page_id,
                'title': title, 'start': start, 'finish': finish,
                'elapsed': float(elapsed),
            })
            in_progress.pop(pid, None)
            continue

        m = START_RE.search(line)
        if m:
            pid, ordinal, page_id, title, start = m.groups()
            in_progress[int(pid)] = {
                'ordinal': int(ordinal), 'id': page_id, 'title': title,
                'start': start,
            }
            continue

    # If the run has finished, nothing is genuinely "stuck" anymore --
    # process_dump() only prints this after every dispatched job has
    # completed and been consumed, so any leftover in_progress entries
    # at that point would just be a parsing artifact, not a real stall.
    if finished_run:
        in_progress = {}

    return timings, in_progress


def parse_timestamp(ts):
    return time.mktime(time.strptime(ts, '%Y-%m-%d %H:%M:%S'))


def report_trend(timings, buckets=10):
    """
    Splits completed pages into `buckets` chronological groups (by
    start time) and reports the average elapsed time per group, so a
    worsening trend (jobs taking progressively longer, e.g. from
    growing memory pressure or worsening contention) is visible at a
    glance, rather than buried in a flat top-N list that only shows
    the single slowest outliers regardless of when they happened.
    """
    if len(timings) < buckets:
        return
    ordered = sorted(timings, key=lambda t: parse_timestamp(t['start']))
    n = len(ordered)
    bucket_size = n / buckets
    print(f"=== Trend: average elapsed time across {buckets} chronological buckets "
          f"({n} completed pages, oldest to newest) ===")
    bucket_avgs = []
    for i in range(buckets):
        lo = int(i * bucket_size)
        hi = int((i + 1) * bucket_size) if i < buckets - 1 else n
        chunk = ordered[lo:hi]
        if not chunk:
            continue
        avg = sum(t['elapsed'] for t in chunk) / len(chunk)
        bucket_avgs.append(avg)
        bar = '#' * min(80, int(avg / 5))
        print(f"  bucket {i + 1:2}/{buckets} ({len(chunk):5} pages, "
              f"{ordered[lo]['start']} to {ordered[hi - 1]['start']}): "
              f"avg {avg:9.2f}s  {bar}")
    if len(bucket_avgs) >= 2 and bucket_avgs[0] > 0:
        ratio = bucket_avgs[-1] / bucket_avgs[0]
        direction = "SLOWER" if ratio > 1.2 else ("faster" if ratio < 0.8 else "roughly flat")
        print(f"  Last bucket is {ratio:.1f}x the first bucket's average -> {direction}")
    print()


def report_last_job_per_worker(timings, in_progress):
    """
    For every worker PID seen anywhere in the log, report when it most
    recently received a job at all -- whether that job has since
    completed or not. A worker with no current in-progress entry isn't
    necessarily idle-and-fine: if its last known activity was hours
    ago while the overall run is still going, that worker has gone
    quiet, most likely blocked somewhere between finishing extraction
    and looping back for its next job (e.g. output_queue.put()
    blocking because the single reduce_process consuming it has fallen
    behind or stalled) -- a step this instrumentation doesn't directly
    log, so a dangling PAGE_START is not the only sign of a stuck
    worker.
    """
    last_seen = {}
    for t in timings:
        pid = t['pid']
        ts = parse_timestamp(t['start'])
        if pid not in last_seen or ts > last_seen[pid][0]:
            last_seen[pid] = (ts, t['start'], t['ordinal'], t['id'], t['title'], 'finished')
    for pid, info in in_progress.items():
        ts = parse_timestamp(info['start'])
        if pid not in last_seen or ts > last_seen[pid][0]:
            last_seen[pid] = (ts, info['start'], info['ordinal'], info['id'],
                               info['title'], 'in progress')

    if not last_seen:
        return

    now = time.time()
    rows = sorted(last_seen.items(), key=lambda kv: kv[1][0])  # oldest activity first
    print(f"=== When each of the {len(rows)} worker(s) last received a job "
          f"(oldest first -- these are the ones to look at) ===")
    for pid, (ts, start_str, ordinal, page_id, title, status) in rows:
        idle_for = now - ts
        print(f"  pid={pid:<10} last job started {start_str}  "
              f"({idle_for:9.1f}s ago)  status={status:<12} "
              f"ordinal={ordinal:<10} id={page_id:<12} title={title}")
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help="Path to a --page_timing log file, or '-' for stdin")
    ap.add_argument('--top', type=int, default=15,
                     help='Show this many of the slowest completed pages (default 15)')
    ap.add_argument('--trend-buckets', type=int, default=10,
                     help='Number of chronological buckets for the trend report (default 10)')
    args = ap.parse_args()

    if args.input == '-':
        lines = sys.stdin.readlines()
    else:
        with open(args.input, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

    timings, in_progress = parse_log(lines)

    print(f"Parsed {len(timings)} completed page(s), "
          f"{len(in_progress)} currently in progress.\n")

    if timings:
        top_n = sorted(timings, key=lambda t: -t['elapsed'])[:args.top]
        print(f"=== {len(top_n)} slowest completed page(s) ===")
        for t in top_n:
            print(f"  {t['elapsed']:9.2f}s  pid={t['pid']:<8} ordinal={t['ordinal']:<10} "
                  f"id={t['id']:<12} title={t['title']}")
        print()

        report_trend(timings, buckets=args.trend_buckets)

    if in_progress:
        now = time.time()
        rows = []
        for pid, info in in_progress.items():
            try:
                running_for = now - parse_timestamp(info['start'])
                running_for_str = f"{running_for:9.1f}s"
                sort_key = running_for
            except ValueError:
                running_for_str = "  unknown"
                sort_key = -1
            rows.append((sort_key, pid, info, running_for_str))
        rows.sort(reverse=True)

        print(f"=== {len(rows)} worker(s) appear STUCK right now "
              f"(PAGE_START with no PAGE_TIMING yet) ===")
        for _, pid, info, running_for_str in rows:
            print(f"  pid={pid:<8} ordinal={info['ordinal']:<10} id={info['id']:<12} "
                  f"running for {running_for_str}  (started {info['start']})  "
                  f"title={info['title']}")
        print()
    else:
        if timings:
            print("No workers appear stuck right now "
                  "(every logged PAGE_START has a matching PAGE_TIMING, "
                  "or the run has already finished).\n"
                  "NOTE: this only covers the extraction step itself -- a worker "
                  "that finished extracting but is blocked handing its result off "
                  "(e.g. output_queue full because reduce_process has fallen behind) "
                  "would also show as 'not stuck' here. Check the per-worker "
                  "last-job-received report below if the run isn't actually "
                  "progressing despite no workers showing as stuck.\n")
        else:
            print("No PAGE_START/PAGE_TIMING lines found -- was --page_timing enabled?")

    report_last_job_per_worker(timings, in_progress)


if __name__ == '__main__':
    main()
