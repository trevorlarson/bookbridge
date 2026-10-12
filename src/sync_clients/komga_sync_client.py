"""Komga sync client — bridges native Komga Kobo progress with Audiobookshelf.

Komga keeps serving the stock Kobo its own library, covers, entitlement sync and
Kobo Store proxying through its existing ``api_endpoint``. This adapter is the
independent second half: it reads the reader's Komga reading state, resolves it
onto the bridge's audio/ebook alignment, and can write a position back.

Design rules (see ``docs/komga-development.md`` and
``docs/komga-contract-reference.md``):

* Reading progress is never converted into a reset. An unreadable, ambiguous,
  non-finite or future-dated Komga state yields ``None`` — no state at all —
  instead of a synthetic zero.
* The read side uses Komga's own ``ContentSourceProgressPercent`` (the device's
  in-chapter progression) and refines it with a served-KEPUB span lookup only
  when that map is already verified and cached.
* The write side is gated behind ``KOMGA_REVERSE_SYNC_ENABLED`` (admin kill
  switch and per-reader toggle, off by default) *and* requires a verified
  placement in the exact KEPUB Komga advertises, a baseline read from the same
  cycle, and a positive read-back. Anything less is a skip or a failure, never a
  blind write.
* The whole DTO envelope, the timestamp rules and the preflight/read-back checks
  live in :mod:`src.api.komga_sync_api`; this client only decides *what* to say.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Optional

from src.api.komga_client import KomgaClient
from src.api.komga_sync_api import (
    STATUS_FINISHED,
    STATUS_READING,
    STATUS_READY,
    KomgaSyncApi,
)
from src.api.komga_transport import valid_book_id
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import (
    ServiceState,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils import komga_content
from src.utils.config_loader import env_truthy
from src.utils.ebook_sources import normalize_ebook_source
from src.utils.progress_metadata import parse_service_timestamp

logger = logging.getLogger(__name__)

# Percentage-point deadband for "did this move", matching the other clients.
DELTA_ENV_KEY = "SYNC_DELTA_KOMGA_PERCENT"

# Per-book bookkeeping kept by a long-lived adapter. Both maps are insertion
# ordered, so the oldest entry is dropped first; they only ever hold transient
# read/cache state that a later cycle re-derives.
BOOK_STATE_CAPACITY = 256


def komga_ebook_filename(book_id: str) -> str:
    """Local basename of a Komga source EPUB (as published by :class:`KomgaClient`)."""
    return f"komga_{book_id}.epub"


class KomgaSyncClient(SyncClient):
    """Reader-scoped Komga progress adapter behind the existing SyncClient contract."""

    def __init__(self, komga_sync_api: KomgaSyncApi, komga_client: KomgaClient,
                 ebook_parser, user_id: Optional[int] = None, database_service=None) -> None:
        super().__init__(ebook_parser)
        self.komga_sync_api = komga_sync_api
        self.komga_client = komga_client
        # Explicit principal: the adapter must never infer the reader from an
        # ambient default or a thread-local the caller happened to set.
        self.user_id = user_id
        self.database_service = database_service
        self.delta_thresh = float(os.getenv(DELTA_ENV_KEY, 1)) / 100.0
        self._content = komga_content.ContentCache()
        # book_id -> (principal fingerprint, content key) for the span map this
        # client last verified. Lets the read path reuse a verified map without a
        # catalog call per poll, while still dropping it when the reader changes.
        self._content_keys: dict[str, tuple[str, str]] = {}
        # Baseline reads of this cycle, keyed by remote book id. Kept out of
        # ServiceState.current because that dict is persisted and fingerprinted.
        self._last_reads: dict[str, dict] = {}

    @staticmethod
    def _remember(store: dict, key: str, value) -> None:
        """Insert into a bounded insertion-ordered map, evicting the oldest."""
        store.pop(key, None)
        store[key] = value
        while len(store) > BOOK_STATE_CAPACITY:
            store.pop(next(iter(store)))

    # ------------------------------------------------------------------ status

    def is_configured(self) -> bool:
        return self.komga_sync_api.is_configured()

    def check_connection(self):
        return self.komga_sync_api.check_connection()

    def get_supported_sync_types(self) -> set:
        return {'audiobook', 'ebook'}

    def supports_book(self, book: Book) -> bool:
        """Applicable only to an explicitly paired Komga EPUB mapping."""
        if normalize_ebook_source(getattr(book, "ebook_source", None)) != "Komga":
            return False
        return valid_book_id(getattr(book, "ebook_source_id", None))

    # -------------------------------------------------------------------- read

    def get_service_state(self, book: Book, prev_state: Optional[State],
                          title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        book_id = getattr(book, "ebook_source_id", None)
        state = self.komga_sync_api.get_reading_state(book_id) if valid_book_id(book_id) else None
        if state is None:
            # Unknown/ambiguous/absent progress is not a position. Reporting
            # nothing keeps a stale or empty Komga row from becoming a reset.
            logger.debug("📖 Komga Sync: no usable reading state for '%s'", getattr(book, "abs_title", "?"))
            return None

        self._remember(self._last_reads, book_id, state)
        pct = state["progress_percent"]
        # A persisted row can legitimately carry no percentage at all (an
        # unobserved/declined write), so a missing value means "nothing known"
        # rather than a crash or a synthetic zero.
        prev_pct = prev_state.percentage if prev_state and prev_state.percentage is not None else 0
        current = {"pct": pct}
        if state.get("href"):
            current["href"] = state["href"]
        if state.get("frag"):
            current["frag"] = state["frag"]
        chapter_progress = self._chapter_progress(book_id, state)
        if chapter_progress is not None:
            current["chapter_progress"] = chapter_progress
        service_updated_at = parse_service_timestamp(state.get("service_updated_at"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at
        if state.get("status"):
            current["status"] = state["status"]

        return ServiceState(
            current=current,
            previous_pct=prev_pct,
            delta=abs(pct - prev_pct),
            threshold=self.delta_thresh,
            is_configured=self.komga_sync_api.is_configured(),
            display=("Komga", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

    def _chapter_progress(self, book_id: str, state: dict) -> Optional[float]:
        """In-chapter progression for a device span, preferring a verified map.

        Komga already reports the device's own ``ContentSourceProgressPercent``,
        which is the authoritative in-chapter value and needs no download. When a
        served span map is *already* verified and cached (a write path loaded it),
        the span is resolved against it instead, because that is the exact
        namespace the bridge addresses. A cache miss does not trigger a download
        here: a poll must not pay for a ~1 MB KEPUB per book per cycle.
        """
        reported = state.get("chapter_progress")
        if not env_truthy("KOMGA_KOBO_SPAN_SYNC", "true"):
            return reported
        span_map = self._cached_span_map(book_id)
        if not span_map:
            return reported
        resolved = komga_content.chapter_progress_for_span(span_map, state.get("href"), state.get("frag"))
        return reported if resolved is None else resolved

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        pct = state.current.get("pct")
        book_id = getattr(book, "ebook_source_id", None)
        if pct is None or not valid_book_id(book_id) or not self.ebook_parser:
            return None
        return self.ebook_parser.get_text_at_percentage(komga_ebook_filename(book_id), pct)

    # ----------------------------------------------------------- content/span

    def _cached_span_map(self, book_id: str):
        """A previously verified span map for this book and reader, else None.

        No I/O: reads only what a write path already downloaded and verified.
        """
        principal = self.komga_sync_api.content_identity()["principal"]
        entry = self._content_keys.get(book_id)
        if not entry or entry[0] != principal:
            return None
        return self._content.get(entry[1]) or None

    def _content_key(self, book_id: str) -> Optional[str]:
        """Cache key for this reader's view of one book's served content."""
        catalog = self.komga_client.get_book(book_id) if valid_book_id(book_id) else None
        if not catalog:
            return None
        identity = self.komga_sync_api.content_identity()
        return komga_content.content_key(
            server=identity["server"], principal=identity["principal"], book_id=book_id,
            revision=(catalog.get("file_hash"), catalog.get("file_modified_at"),
                      catalog.get("size_bytes"), catalog.get("library_id")),
        )

    def _source_epub_bytes(self, book_id: str) -> Optional[bytes]:
        """Canonical source EPUB bytes the bridge aligned against.

        Prefers the file the acquisition path already published; otherwise asks
        the client to (re)download it. Never a different provider's copy.
        """
        directory = getattr(self.komga_client, "books_dir", None)
        if directory is not None:
            try:
                published = Path(directory) / komga_ebook_filename(book_id)
                if published.is_file():
                    return published.read_bytes()
            except OSError as exc:
                logger.debug("📖 Komga Sync: could not read the published source EPUB: %s", exc)
        filename = None
        try:
            filename = self.komga_client.download_ebook(book_id)
        except Exception as exc:
            logger.warning("⚠️ Komga Sync: source EPUB download failed: %s", exc, exc_info=True)
        if not filename:
            return None
        directory = getattr(self.komga_client, "books_dir", None)
        if directory is None:
            return None
        try:
            return (Path(directory) / filename).read_bytes()
        except OSError:
            return None

    def _served_span_map(self, book_id: str):
        """Verified span map of the KEPUB Komga advertises, or None.

        Cached per content key, so a changed file revision, a re-paired edition
        or a rotated reader credential misses and re-downloads instead of
        reusing a span namespace that no longer matches what the device holds.
        """
        key = self._content_key(book_id)
        if not key:
            return None
        cached = self._content.get(key)
        if cached is not None:
            return cached or None
        try:
            kepub = self.komga_sync_api.download_kepub(book_id)
            span_map = komga_content.build_served_span_map(kepub, self._source_epub_bytes(book_id))
        except Exception as exc:
            logger.warning("⚠️ Komga Sync: could not build the served span map: %s", exc, exc_info=True)
            span_map = None
        if span_map is None:
            logger.info(
                "📖 Komga Sync: no verified served-KEPUB span map for '%s'; "
                "exact placement unavailable", book_id,
            )
        else:
            self._remember(self._content_keys, book_id,
                           (self.komga_sync_api.content_identity()["principal"], key))
        # Cache the miss under this key too: the whole point of the key is that it
        # changes when the content does.
        self._content.put(key, span_map if span_map is not None else False)
        return span_map

    @staticmethod
    def _status_for(pct: float) -> str:
        """Reading status implied by a (validated) whole-book percentage.

        Threshold-based on purpose and kept conservative: ``Finished`` needs
        >= 99%, an explicit 0 is a genuine reset, everything else is Reading.
        Komga derives its own completion from its analysed positions, so this is
        only the status field of the write and is re-checked in the read-back.
        """
        if pct >= 0.99:
            return STATUS_FINISHED
        if pct <= 0.0:
            return STATUS_READY
        return STATUS_READING

    def _baseline(self, book_id: str) -> Optional[dict]:
        """This cycle's read of the target, used as the write's expected state.

        A missing baseline is re-read once rather than invented. If even that
        fails there is no trustworthy preflight, so the write is skipped.
        """
        state = self._last_reads.get(book_id)
        if state is not None:
            return state
        state = self.komga_sync_api.get_reading_state(book_id)
        if state is not None:
            self._remember(self._last_reads, book_id, state)
        return state

    def _observed_state(self, book_id: str, request: UpdateProgressRequest,
                        pct: Optional[float]) -> dict:
        """What the bridge genuinely knows about Komga right now.

        A declined write is still reported ``success=True``, and the sync manager
        persists a successful follower result as that client's state row — so the
        dict must describe a *read* of Komga, never the position the bridge chose
        not to write. Preference order: this cycle's own read of Komga, then the
        state the caller already read this cycle, otherwise "unknown" rather than
        the requested percentage.
        """
        read = self._last_reads.get(book_id)
        if read is not None:
            return self._state_dict(read)
        current = getattr(request.current_state, "current", None) if request is not None else None
        if isinstance(current, dict) and current.get("pct") is not None:
            return dict(current)
        return {"pct": None}

    @staticmethod
    def _state_dict(read: dict) -> dict:
        """A read normalized into the state-dict shape the sync manager persists."""
        state = {"pct": read.get("progress_percent"), "href": read.get("href"),
                 "frag": read.get("frag"), "chapter_progress": read.get("chapter_progress")}
        service_updated_at = parse_service_timestamp(read.get("service_updated_at"))
        if service_updated_at is not None:
            state["service_updated_at"] = service_updated_at
        if read.get("status"):
            state["status"] = read["status"]
        return state

    # ------------------------------------------------------------------- write

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_id = getattr(book, "ebook_source_id", None)
        locator = request.locator_result
        pct = getattr(locator, "percentage", None)
        if not valid_book_id(book_id) or pct is None or isinstance(pct, bool) \
                or not isinstance(pct, (int, float)) or not math.isfinite(pct) or pct < 0:
            logger.warning("⚠️ Komga Sync: refusing to write an unusable position for '%s'",
                           getattr(book, "abs_title", "?"))
            return SyncResult(success=False)

        if not self.is_configured():
            # The adapter is switched off for this reader (or for the install):
            # an intentional no-write, not a failed one.
            return SyncResult(pct, True, self._observed_state(book_id, request, pct), skipped=True)

        if not self.komga_sync_api.reverse_writes_enabled():
            # Deliberate, visible policy gate: the reverse direction stays off
            # until the served-KEPUB/device placement gates are verified on a
            # real reader. Not a failure, and never a silent no-op wrapper.
            logger.info(
                "📖 Komga Sync: reverse writes are disabled (KOMGA_REVERSE_SYNC_ENABLED); "
                "leaving Komga's reading state untouched for '%s'",
                getattr(book, "abs_title", "?"),
            )
            return SyncResult(pct, True, self._observed_state(book_id, request, pct), skipped=True)

        href = getattr(locator, "href", None)
        chapter_progress = getattr(locator, "chapter_progress", None)
        placement = None
        if env_truthy("KOMGA_KOBO_SPAN_SYNC", "true") and href and chapter_progress is not None:
            # A locator with no chapter identity cannot be placed exactly, and
            # Komga rejects a null Location, so there is nothing to write and no
            # reason to spend a KEPUB download finding that out.
            placement = komga_content.resolve_placement(
                self._served_span_map(book_id), href=href, chapter_progress=chapter_progress,
            )
        if placement is None:
            # Komga rejects a null Location outright, so a percentage-only write
            # cannot move a Kobo: fail closed instead of writing a cosmetic number.
            logger.info(
                "📖 Komga Sync: no verified exact placement for '%s'; "
                "leaving Komga's reading state untouched",
                getattr(book, "abs_title", "?"),
            )
            return SyncResult(pct, True, self._observed_state(book_id, request, pct), skipped=True)

        baseline = self._baseline(book_id)
        if baseline is None:
            logger.warning("⚠️ Komga Sync: no trustworthy baseline read for '%s'; skipping write",
                           getattr(book, "abs_title", "?"))
            return SyncResult(pct, True, self._observed_state(book_id, request, pct), skipped=True)

        location = {"Source": placement.source_href, "Type": "KoboSpan", "Value": placement.span_id}
        confirmed = self.komga_sync_api.update_reading_state(
            book_id,
            float(pct),
            self._status_for(float(pct)),
            location,
            chapter_progress=placement.chapter_progress,
            expected_state=baseline,
        )
        if confirmed is None:
            # The API client returns a normalized read-back or nothing: a failure,
            # a conflict or an unconfirmed timeout is never reported as written.
            logger.warning("⚠️ Komga Sync: write was not confirmed by read-back for '%s'",
                           getattr(book, "abs_title", "?"))
            return SyncResult(pct, False, {"pct": pct})

        self._remember(self._last_reads, book_id, confirmed)
        updated_state = self._state_dict(confirmed)
        try:
            from src.services.write_tracker import record_write

            record_write('Komga', book.abs_id, pct, user_id=self.user_id)
        except ImportError:
            logger.debug("📖 Komga Sync: write tracker unavailable; echo suppression skipped")
        logger.info(
            "📖 Komga Sync: wrote koboSpan %s in %s for '%s' at %.2f%% (%s)",
            placement.span_id, placement.source_href, getattr(book, "abs_title", "?"),
            float(pct) * 100, confirmed.get("status"),
        )
        # The service recomputes the total percentage from its own positions, so
        # the result reports what Komga persisted, not what was requested.
        return SyncResult(confirmed["progress_percent"], True, updated_state)
