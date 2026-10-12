"""Content identity and exact Kobo placement for Komga-sourced EPUBs.

A stock Kobo navigates by ``CurrentBookmark.Location`` — a
``{Source, Type: "KoboSpan", Value: "kobo.N.M"}`` triple — and treats
``ProgressPercent`` as display-only. Komga serves the device an *advertised*
KEPUB (``?convert_kepub=``), which is a converted copy of the book's own file:
the span ids exist only in those served bytes, so the bridge can only write a
position the device will act on by resolving one from that exact copy.

Two things are deliberately stricter here than in
:mod:`src.utils.kepub_locator`, which stays the shared pure helper:

1. Chapter identity is an exact normalized-href match. ``kepub_locator`` falls
   back to a suffix comparison when no href matches exactly; that heuristic is
   fine for a read-only percentage refinement, but a *written* position must not
   land in the wrong chapter of a book that happens to share a document name.
2. ``resolve_placement`` never falls back to placing a whole-book percentage
   across concatenated chapters, and it reports the in-chapter progression of
   the span it actually resolved rather than the progression it was asked for.

The served KEPUB is only trusted once its chapters are provably documents of the
canonical source EPUB the bridge aligned against (:func:`build_served_span_map`).
A converter/version/content change produces a different content key
(:func:`content_key`), so caches keyed by it cannot serve a stale span namespace.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from src.utils import kepub_locator

logger = logging.getLogger(__name__)

# Bumped when the identity/placement contract below changes, so previously
# cached span maps cannot satisfy a request built under different rules.
CONTENT_VERSION = 1

# span maps are ~1 MB of parsed structure per book; a small cache is enough for
# the handful of books a poll cycle can touch.
DEFAULT_CAPACITY = 4


@dataclass
class Placement:
    """An exact addressable position in the served KEPUB."""

    source_href: str
    span_id: str
    # In-chapter progression of the resolved span (0-1). Sent to the service as
    # ContentSourceProgressPercent, so it describes the span we actually chose.
    chapter_progress: float


def content_key(*, server: str, principal: str, book_id: str, revision: Any,
                version: int = CONTENT_VERSION) -> Optional[str]:
    """Identity of one served content revision for one reader and book.

    ``principal`` must be a non-reversible principal fingerprint (the transport's
    own sha256 digest), never a token: the key is compared, cached and may be
    logged. Any change to the server, reader, book or catalog revision fields
    yields a different key, which is how a replaced file, a re-paired edition or
    a rotated credential invalidates the served span namespace.
    """
    if not server or not principal or not book_id:
        return None
    try:
        material = json.dumps(
            [version, server, principal, book_id, revision], sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def source_spine_hrefs(epub_bytes: Optional[bytes]) -> list[str]:
    """Spine document hrefs of a plain EPUB, in reading order.

    Returns ``[]`` when the archive cannot be read; callers treat that as
    "content identity unverified" and refuse precise placement.
    """
    if not epub_bytes:
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as archive:
            opf_path, opf_dir = kepub_locator._read_opf(archive)
            if not opf_path or opf_path not in set(archive.namelist()):
                return []
            opf_xml = archive.read(opf_path).decode("utf-8", "replace")
            hrefs = []
            for href in kepub_locator._spine_hrefs(opf_xml):
                normalized = kepub_locator.normalize_source_href(href, opf_dir)
                if normalized:
                    hrefs.append(normalized)
            return hrefs
    except (zipfile.BadZipFile, OSError, KeyError, ValueError) as exc:
        logger.debug("komga-content: could not read source EPUB spine: %s", exc)
        return []


def build_served_span_map(kepub_bytes: Optional[bytes],
                          source_epub_bytes: Optional[bytes]) -> Optional[kepub_locator.SpanMap]:
    """Span map of the served KEPUB, or None when identity is unverified.

    Verification is deliberately one-directional and cheap: every chapter the
    served KEPUB addresses must be a document of the canonical source EPUB. A
    KEPUB converted by a different converter, from a different edition, or from a
    different file cannot pass that check, and a plain (never-converted) EPUB
    carries no koboSpans at all so ``build_span_map`` already returns None.
    """
    span_map = kepub_locator.build_span_map(kepub_bytes) if kepub_bytes else None
    if span_map is None:
        return None
    source_hrefs = set(source_spine_hrefs(source_epub_bytes))
    if not source_hrefs:
        logger.debug("komga-content: no source EPUB spine; refusing to trust served spans")
        return None
    unmatched = [chapter.source_href for chapter in span_map.chapters
                 if chapter.source_href not in source_hrefs]
    if unmatched:
        logger.warning(
            "komga-content: served KEPUB addresses %d document(s) that the canonical "
            "source EPUB does not contain; refusing precise placement",
            len(unmatched),
        )
        return None
    return span_map


def strict_chapter(span_map: Optional[kepub_locator.SpanMap],
                   href: Optional[str]) -> Optional[kepub_locator.Chapter]:
    """The chapter for an exact normalized href, else None (never a guess)."""
    if span_map is None or not href:
        return None
    target = kepub_locator.normalize_source_href(href)
    if not target:
        return None
    for chapter in span_map.chapters:
        if chapter.source_href == target:
            return chapter
    return None


def chapter_progress_for_span(span_map: Optional[kepub_locator.SpanMap],
                              source_href: Optional[str],
                              span_id: Optional[str]) -> Optional[float]:
    """In-chapter progression of a device-reported span, or None.

    The read-side counterpart of :func:`resolve_placement`: only an exact chapter
    match counts, so a span id that also exists in another chapter cannot be
    resolved against the wrong one.
    """
    chapter = strict_chapter(span_map, source_href)
    if chapter is None or chapter.char_len <= 0:
        return None
    for span in chapter.spans:
        if span.span_id == span_id:
            return max(0.0, min(span.start / chapter.char_len, 1.0))
    return None


def resolve_placement(span_map: Optional[kepub_locator.SpanMap],
                      href: Optional[str],
                      chapter_progress: Optional[float]) -> Optional[Placement]:
    """Resolve a bridge locator to an exact span in the served KEPUB.

    Requires the locator's own chapter href to match exactly and a finite
    in-chapter progression; returns None instead of falling back to a whole-book
    percentage, because a written position that is only approximately placed is
    worse than leaving the device's bookmark untouched.
    """
    if chapter_progress is None or not isinstance(chapter_progress, (int, float)) \
            or isinstance(chapter_progress, bool):
        return None
    try:
        progress = float(chapter_progress)
    except (TypeError, ValueError):
        return None
    if progress != progress or progress in (float("inf"), float("-inf")) or not 0 <= progress <= 1:
        return None
    chapter = strict_chapter(span_map, href)
    if chapter is None or not chapter.spans:
        return None
    positioned = kepub_locator._span_at_progress(chapter, progress)
    if positioned is None:
        return None
    effective = (max(0.0, min(positioned.start / chapter.char_len, 1.0))
                 if chapter.char_len > 0 else 0.0)
    return Placement(source_href=chapter.source_href, span_id=positioned.span_id,
                     chapter_progress=effective)


def source_href_matches_source(href: Optional[str], source_epub_bytes: Optional[bytes]) -> bool:
    """True when ``href`` names a document the canonical source EPUB contains."""
    if not href:
        return False
    target = kepub_locator.normalize_source_href(href)
    if not target:
        return False
    source_hrefs = source_spine_hrefs(source_epub_bytes)
    return bool(source_hrefs) and target in set(source_hrefs)


class ContentCache:
    """Insertion-ordered per-content-key store of verified span maps.

    Keyed by :func:`content_key`, so a new revision, reader or book is a cache
    miss by construction instead of something a caller has to remember to
    invalidate. Misses are cached as ``None`` only for the lifetime of one key,
    which cannot outlive the content it misses on.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._entries: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Optional[str]) -> Any:
        if not key:
            return None
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
        return None

    def put(self, key: Optional[str], value: Any) -> None:
        if not key:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
