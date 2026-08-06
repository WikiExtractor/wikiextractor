"""
template_blob.py

A read-only, dict-like view over {title: template_text}, backed by
three flat byte buffers (a sorted, binary-searchable title index;
concatenated title bytes; concatenated content bytes) instead of one
Python dict holding ~900K individual str objects.

Why this exists: fork()'s copy-on-write sharing does not actually
protect a plain dict full of strings the way it looks like it should.
Every *read* of a Python object -- even `title in templates`, which
looks read-only -- increments that object's own refcount, which lives
in the same memory page as the object itself. That increment is a
write at the memory level, and writing to any part of a page is
exactly what triggers copy-on-write. So a worker process doing nothing
but ordinary template lookups still ends up silently, irreversibly
privatizing large fractions of the dict's pages over its lifetime, one
touched title at a time -- defeating COW despite the worker never
intentionally mutating anything.

Confirmed directly on a real, memory-constrained production run (8GB/
job, 31 workers, ~897K templates): via /proc/<pid>/smaps_rollup, a
worker doing real template lookups showed Private_Dirty 5-8x higher
than the reducer/mapper (which mostly relay already-processed text,
touching far fewer distinct templates) -- tracking exactly with how
much each process type actually reads from `templates`, not with
anything either of them explicitly writes.

Raw shared-memory bytes sidestep this entirely: there are no per-title
Python objects sitting in the buffer waiting to be referenced and
refcounted. A lookup touches a handful of small slices during binary
search, decodes the ONE matched (title, content) pair into a fresh
str, and that transient object is garbage collected shortly after use
-- nothing about a lookup privatizes any shared page. Verified earlier
(see prescan/lru_cache work) that this holds at real scale: worker RSS
stayed flat around 18-24MB regardless of corpus size, using this exact
three-blob design.

Layout:
  content_blob: concatenated raw template text, UTF-8
  titles_blob:  concatenated title text, UTF-8, same order as records
  records:      fixed-width array, SORTED by title (binary search),
                one record per template:
                  >QQQQ = title_offset, title_length, content_offset, content_length
                (all big-endian unsigned 64-bit, 32 bytes/record)

Used identically from both the multiprocess path (process_dump) and
the single-article (--article) path -- deliberately not a separate
plain-dict branch for the latter, since exercising a different lookup
mechanism there would defeat --article's use as a way to validate real
behavior, not just a fast path for one-off runs.
"""

import struct
from functools import lru_cache
from multiprocessing import shared_memory

RECORD_FORMAT = '>QQQQ'
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)


def build_template_blobs(templates_dict):
    """templates_dict: {title: text}, e.g. extract.templates as
    populated by load_templates()/define_template() before compaction.
    Returns (records_bytes, titles_bytes, content_bytes)."""
    content_parts = []
    content_offset = 0
    content_offsets = {}
    for title, text in templates_dict.items():
        encoded = text.encode('utf-8')
        content_offsets[title] = (content_offset, len(encoded))
        content_parts.append(encoded)
        content_offset += len(encoded)
    content_blob = b''.join(content_parts)

    title_parts = []
    title_offset = 0
    records = []
    for title in sorted(templates_dict.keys()):  # sorted -> binary search works
        encoded_title = title.encode('utf-8')
        c_off, c_len = content_offsets[title]
        records.append(struct.pack(RECORD_FORMAT, title_offset, len(encoded_title), c_off, c_len))
        title_parts.append(encoded_title)
        title_offset += len(encoded_title)
    titles_blob = b''.join(title_parts)
    records_blob = b''.join(records)

    return records_blob, titles_blob, content_blob


class CompactedTemplates:
    """Read-only, dict-like view over the three blobs above. Supports
    exactly what extract.py's own template-lookup code uses:
    `title in templates` and `templates[title]` (plus `.get()`, for
    compatibility with the other tools built against extract.templates
    over the course of this project -- prescan_template_usage.py,
    extract_templates_by_id.py -- which do use it).

    Deliberately no __setitem__: nothing should be writing to
    `templates` after compaction (all real writes happen earlier, via
    define_template() during loading, against the plain dict this
    class replaces) -- a write attempted after compaction should fail
    loudly (a plain TypeError, from Python's own "object does not
    support item assignment"), not silently do the wrong thing.

    Keeps a small, bounded, title-keyed cache of already-decoded
    content strings. Without it, every lookup of `templates[title]` --
    even the 1000th lookup of the same, already-parse-cached template
    -- would still pay for a fresh bytes->str decode (and a fresh hash
    computation over that new string, for whatever lru_cache the
    result is about to be checked against) before Extractor's own
    _parse_template() cache ever gets a chance to short-circuit
    anything. At real EN scale, several templates are referenced by a
    large fraction of the entire multi-million-article corpus (String,
    Citation/CS1, etc.) -- redundantly decoding those on every single
    use, forever, is a real and avoidable cost. This cache absorbs
    that: a hit here returns the SAME decoded string object each time,
    so a downstream lru_cache benefits from CPython's own hash-caching
    on that object too, not just from skipping re-parsing.
    """

    def __init__(self, records_buf, titles_buf, content_buf, records_len=None, decode_cache_size=10000):
        self._records = records_buf
        self._titles = titles_buf
        self._content = content_buf
        # records_len lets the caller say "only the first N bytes of
        # records_buf are real data" -- needed because
        # SharedMemory's actual allocation can be larger than
        # requested (page-size rounding), so len(records_buf) alone
        # isn't reliably the true record count. Titles/content don't
        # need this: every offset stored in a record already points
        # within the real data range, so slicing with those offsets
        # is safe even if the buffer has unused trailing space.
        self._n = (records_len if records_len is not None else len(records_buf)) // RECORD_SIZE
        self._owned_shms = []  # populated by attach()/compact(), for cleanup

        # Instance-bound cache (not a bare @lru_cache on a method,
        # which would share state across every CompactedTemplates
        # instance ever constructed -- each worker's own instance
        # needs its own, independent cache).
        self._decode = lru_cache(maxsize=decode_cache_size)(self._decode_uncached)

    def _find(self, title):
        """Binary search over records, by title. Returns (content_off,
        content_len) or None."""
        query = title.encode('utf-8')
        lo, hi = 0, self._n - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            rec = self._records[mid * RECORD_SIZE:(mid + 1) * RECORD_SIZE]
            t_off, t_len, c_off, c_len = struct.unpack(RECORD_FORMAT, rec)
            mid_title = bytes(self._titles[t_off:t_off + t_len])
            if mid_title == query:
                return c_off, c_len
            elif mid_title < query:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def _decode_uncached(self, title):
        found = self._find(title)
        if found is None:
            return None
        c_off, c_len = found
        return bytes(self._content[c_off:c_off + c_len]).decode('utf-8')

    def __contains__(self, title):
        return self._decode(title) is not None

    def __getitem__(self, title):
        text = self._decode(title)
        if text is None:
            raise KeyError(title)
        return text

    def get(self, title, default=None):
        text = self._decode(title)
        return default if text is None else text

    def __len__(self):
        return self._n

    def close(self):
        """Detach from shared memory (this process only) -- call once
        this wrapper is no longer needed. Safe to call on an instance
        that doesn't own any shared_memory handles (e.g. one built
        in-process for a test); a no-op in that case.

        Releases this instance's own memoryview references first --
        SharedMemory.close() refuses to run ("cannot close exported
        pointers exist") while any memoryview derived from .buf is
        still alive, and self._records/_titles/_content (plus
        whatever the decode cache is holding) are exactly that.
        """
        self._decode.cache_clear()
        for attr in ('_records', '_titles', '_content'):
            buf = getattr(self, attr)
            if isinstance(buf, memoryview):
                buf.release()
        for shm in self._owned_shms:
            shm.close()

    def unlink(self):
        """Release the underlying shared memory for good -- call ONCE,
        from whichever process created it (the mapper / single-article
        path), only after every consumer (workers, or this same
        in-process extraction loop) is done reading it."""
        for shm in self._owned_shms:
            shm.unlink()


def compact(templates_dict):
    """Build shared-memory segments from templates_dict and return a
    (CompactedTemplates, cleanup) pair. cleanup() must be called
    exactly once, after every consumer (workers, or the caller itself
    in the single-article case) is done -- it closes and unlinks the
    three underlying shared_memory segments.

    The returned CompactedTemplates also owns and closes its own
    handles to these segments (via .close()); cleanup() additionally
    unlinks them (releasing the memory for real) -- kept separate
    since only the creator should ever unlink, while every consumer,
    including the creator itself, should close.
    """
    records_bytes, titles_bytes, content_bytes = build_template_blobs(templates_dict)

    records_shm = shared_memory.SharedMemory(create=True, size=max(1, len(records_bytes)))
    records_shm.buf[:len(records_bytes)] = records_bytes
    titles_shm = shared_memory.SharedMemory(create=True, size=max(1, len(titles_bytes)))
    titles_shm.buf[:len(titles_bytes)] = titles_bytes
    content_shm = shared_memory.SharedMemory(create=True, size=max(1, len(content_bytes)))
    content_shm.buf[:len(content_bytes)] = content_bytes

    wrapper = CompactedTemplates(
        records_shm.buf, titles_shm.buf, content_shm.buf,
        records_len=len(records_bytes),
    )
    wrapper._owned_shms = [records_shm, titles_shm, content_shm]

    names = (records_shm.name, titles_shm.name, content_shm.name, len(records_bytes))

    def cleanup():
        wrapper.close()
        for shm in (records_shm, titles_shm, content_shm):
            shm.unlink()

    return wrapper, names, cleanup


def attach(names):
    """Worker-side: attach to the three already-built shared-memory
    segments by name (as returned by compact()) and return a
    CompactedTemplates view over them. Cheap -- three small mmap
    attach calls, no data copied.

    names: (records_name, titles_name, content_name, records_len) --
    records_len (the real byte length of the records blob, as
    written) is carried through explicitly rather than trusting
    len(records_shm.buf) to equal it -- true on the platforms this has
    been tested on, but not a documented guarantee of the
    SharedMemory API, and not worth depending on silently.
    """
    records_name, titles_name, content_name, records_len = names
    records_shm = shared_memory.SharedMemory(name=records_name)
    titles_shm = shared_memory.SharedMemory(name=titles_name)
    content_shm = shared_memory.SharedMemory(name=content_name)

    wrapper = CompactedTemplates(records_shm.buf, titles_shm.buf, content_shm.buf,
                                  records_len=records_len)
    wrapper._owned_shms = [records_shm, titles_shm, content_shm]
    return wrapper
