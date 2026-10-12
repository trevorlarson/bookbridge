"""Native Komga Kobo progress and advertised KEPUB client.

Wire contracts are pinned to gotson/komga commit
133d30d7bc7715fb4144be56486337db1ad4c525: KoboController,
ReadingStateDto, BookmarkDto, StatisticsDto, StatusInfoDto and ResultDto.
Local serialization and preflight prevent bridge conflicts, but Komga supplies
no conditional-write/CAS operation to exclude simultaneous device writes.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import threading
import weakref
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from src.api.komga_transport import KomgaTransport, safe_path, valid_book_id

STATUS_READING = "Reading"
STATUS_FINISHED = "Finished"
STATUS_READY = "ReadyToRead"
_STATUSES = {STATUS_READING, STATUS_FINISHED, STATUS_READY}
_LOCK_GUARD = threading.Lock()
_BOOK_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _lock_for(identity: tuple[str, str, str]) -> Any:
    with _LOCK_GUARD:
        lock = _BOOK_LOCKS.get(identity)
        if lock is None:
            lock = threading.RLock()
            _BOOK_LOCKS[identity] = lock
        return lock


def _percentage(value: Any, maximum: float) -> float | None:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= maximum else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except ValueError:
        return None


def _location(value: Any) -> dict[str, str] | None:
    if (not isinstance(value, dict) or value.get("Type") != "KoboSpan"
            or not isinstance(value.get("Source"), str) or not safe_path(value["Source"])
            or value["Source"].startswith("/") or urlsplit(value["Source"]).scheme
            or "?" in value["Source"] or "#" in value["Source"]
            or not isinstance(value.get("Value"), str)
            or re.fullmatch(r"kobo\.[0-9]+\.[0-9]+", value["Value"]) is None):
        return None
    return {key: value[key] for key in ("Source", "Type", "Value")}


class KomgaSyncApi(KomgaTransport):
    """Exact-target, strict native Kobo client using the REST principal's key."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 data_dir: str | Path | None = None) -> None:
        super().__init__(config)
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", "data"))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _path(self, book_id: str, resource: str) -> str:
        return f"/kobo/{self.api_key}/v1/library/{book_id}/{resource}"

    def content_identity(self) -> dict[str, str]:
        """Server plus a non-reversible fingerprint of the authenticating reader.

        Never the key itself: callers use this as a cache/provenance scope, so a
        rotated or different reader credential must change it without the value
        being usable as a credential.
        """
        return {"server": self.base_url,
                "principal": hashlib.sha256(self.api_key.encode()).hexdigest()}

    def _identity(self, book_id: str) -> dict[str, str]:
        return {"book_id": book_id, **self.content_identity()}

    def reverse_writes_enabled(self) -> bool:
        """True when this reader's settings permit writing progress to Komga.

        The single visible gate for the reverse direction. It is off by default
        (``KOMGA_REVERSE_SYNC_ENABLED=false``) until exact served-KEPUB placement
        and real-device navigation have been verified; readers and the admin can
        each only take it away, and the value is re-read on every call so a
        disabled install stops writing without a restart.
        """
        return self._gate_allows("KOMGA_REVERSE_SYNC_ENABLED")

    def check_connection(self) -> bool:
        """Read initialization without triggering a stock-device library sync."""
        data = self._json(self._request("GET", self._url(f"/kobo/{self.api_key}/v1/initialization")))
        return isinstance(data, dict) and isinstance(data.get("Resources"), dict)

    def _normalise(self, row: Any, book_id: str) -> dict[str, Any] | None:
        if not isinstance(row, dict) or row.get("EntitlementId") != book_id:
            return None
        bookmark, statistics, status = (row.get(key) for key in ("CurrentBookmark", "Statistics", "StatusInfo"))
        if not all(isinstance(component, dict) for component in (bookmark, statistics, status)):
            return None
        required = [row, bookmark, statistics, status]
        times = [_timestamp(component.get("LastModified")) for component in required]
        now = self._now()
        if any(value is None or value > now for value in times):
            return None
        for component, names in ((row, ("Created", "PriorityTimestamp")),
                                 (status, ("LastTimeFinished", "LastTimeStartedReading"))):
            for name in names:
                if name in component and component[name] is not None:
                    value = _timestamp(component[name])
                    if value is None or value > now:
                        return None
        total = _percentage(bookmark.get("ProgressPercent"), 100)
        chapter = _percentage(bookmark.get("ContentSourceProgressPercent"), 100)
        location = _location(bookmark.get("Location"))
        if total is None or chapter is None or location is None or status.get("Status") not in _STATUSES:
            return None
        for component, keys in ((statistics, ("RemainingTimeMinutes", "SpentReadingMinutes")),
                                (status, ("TimesStartedReading",))):
            for key in keys:
                if key in component and component[key] is not None:
                    value = component[key]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        return None
        # Copy only DTO fields: no unexpected response contents or secret URLs.
        wire = {"EntitlementId": book_id, "LastModified": row["LastModified"],
                "CurrentBookmark": {"LastModified": bookmark["LastModified"],
                                    "ProgressPercent": total, "ContentSourceProgressPercent": chapter,
                                    "Location": location},
                "Statistics": {key: statistics[key] for key in ("LastModified", "RemainingTimeMinutes", "SpentReadingMinutes") if key in statistics},
                "StatusInfo": {key: status[key] for key in ("LastModified", "Status", "TimesStartedReading", "LastTimeFinished", "LastTimeStartedReading") if key in status}}
        for key in ("Created", "PriorityTimestamp"):
            if key in row:
                wire[key] = row[key]
        return {"progress_percent": total / 100, "chapter_progress": chapter / 100,
                "href": location["Source"], "frag": location["Value"], "status": status["Status"],
                # Komga uses the shared progress modification time, not a
                # device-only bookmark clock. Consumers must retain echo guards.
                "service_updated_at": bookmark["LastModified"],
                "bookmark_last_modified": bookmark["LastModified"],
                "wire": wire, "identity": self._identity(book_id)}

    def get_reading_state(self, book_id: str) -> dict[str, Any] | None:
        """Read exactly one matching state, rejecting unknown or invalid position."""
        if not valid_book_id(book_id):
            return None
        rows = self._json(self._request("GET", self._url(self._path(book_id, "state"))))
        return self._normalise(rows[0], book_id) if isinstance(rows, list) and len(rows) == 1 else None

    def get_metadata(self, book_id: str) -> dict[str, Any] | None:
        """Read exactly this entitlement's metadata with the API key redacted.

        Komga advertises download URLs that embed the auth token in the path, so
        the returned DTO must never be logged verbatim. Only the entitlement's
        own record is returned; unknown or ambiguous identifiers fail closed.
        """
        raw = self._raw_metadata(book_id)
        return self._redact(raw) if raw is not None else None

    def _raw_metadata(self, book_id: str) -> dict[str, Any] | None:
        if not valid_book_id(book_id):
            return None
        rows = self._json(self._request("GET", self._url(self._path(book_id, "metadata"))))
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get("EntitlementId") != book_id):
            return None
        return rows[0]

    def _redact(self, value: Any) -> Any:
        """Replace the auth token wherever a response echoes it back at us."""
        key = self.api_key
        if isinstance(value, str):
            return value.replace(key, "<redacted>") if key else value
        if isinstance(value, dict):
            return {name: self._redact(item) for name, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def download_kepub(self, book_id: str) -> bytes | None:
        """Fetch only an exact, same-key advertised KEPUB; preserve its query."""
        metadata = self._raw_metadata(book_id)
        if metadata is None:
            return None
        urls = metadata.get("DownloadUrls")
        if not isinstance(urls, list):
            return None
        candidates = [entry for entry in urls if isinstance(entry, dict) and entry.get("Format") == "KEPUB"]
        if len(candidates) != 1:
            return None
        url = candidates[0].get("Url")
        if not isinstance(url, str) or not self._allowed_url(url):
            return None
        target = urlsplit(url)
        expected_path = urlsplit(self._url(f"/kobo/{self.api_key}/v1/books/{book_id}/file/epub")).path
        if target.path != expected_path or parse_qsl(target.query, keep_blank_values=True) not in (
                [("convert_kepub", "true")], [("convert_kepub", "false")]):
            return None
        return self._epub(url)

    @staticmethod
    def _semantic_success(data: Any, book_id: str) -> bool:
        if not isinstance(data, dict) or data.get("RequestResult") != "Success":
            return False
        results = data.get("UpdateResults")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            return False
        item = results[0]
        if item.get("EntitlementId") != book_id:
            return False
        for name, allowed in (("CurrentBookmarkResult", {"Success"}),
                              ("StatusInfoResult", {"Success"}),
                              ("StatisticsResult", {"Success", "Ignored"})):
            result = item.get(name)
            if not isinstance(result, dict) or result.get("Result") not in allowed:
                return False
        return True

    def update_reading_state(self, book_id: str, progress_percent: float,
                             status: str = STATUS_READING, location: dict[str, str] | None = None,
                             *, chapter_progress: float | None = None,
                             expected_state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Preflight, PUT once, then return exact normalized confirmation or None.

        Expected state must come from this client's GET. Chapter progression is
        separate from total progression. Failure/unknown results are never
        retried. A transport exception is uncertain and may succeed only when
        exact-target readback confirms the submitted locator and newer time.
        """
        if (not self.is_configured() or not valid_book_id(book_id)
                or not self.reverse_writes_enabled()
                or _percentage(progress_percent, 1) is None
                or _percentage(chapter_progress, 1) is None
                or status not in _STATUSES or _location(location) is None
                or not isinstance(expected_state, dict)
                or expected_state.get("identity") != self._identity(book_id)):
            return None
        expected = self._normalise(expected_state.get("wire"), book_id)
        if expected is None or expected != expected_state:
            return None
        lock = _lock_for((self.base_url, self.api_key, book_id))
        with lock:
            current = self.get_reading_state(book_id)
            if current != expected:
                return None
            now = self._now()
            previous_times = [_timestamp(component["LastModified"]) for component in (
                current["wire"], current["wire"]["CurrentBookmark"],
                current["wire"]["Statistics"], current["wire"]["StatusInfo"])]
            if any(now <= timestamp for timestamp in previous_times):
                # Never manufacture a future timestamp to beat equal/newer state.
                return None
            timestamp = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if now.microsecond == 0:
                timestamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
            row = copy.deepcopy(current["wire"])
            for component in (row, row["CurrentBookmark"], row["Statistics"], row["StatusInfo"]):
                component["LastModified"] = timestamp
            row["PriorityTimestamp"] = timestamp
            row["CurrentBookmark"].update({"ProgressPercent": progress_percent * 100,
                                           "ContentSourceProgressPercent": chapter_progress * 100,
                                           "Location": _location(location)})
            row["StatusInfo"]["Status"] = status
            response = self._request("PUT", self._url(self._path(book_id, "state")),
                                     json={"ReadingStates": [row]})
            http_status = None if response is None else response.status_code
            if http_status is not None and 400 <= http_status < 500:
                # A definitive client-side rejection means Komga did not apply
                # anything, so a readback would only add noise.
                response.close()
                return None
            uncertain = http_status is None or http_status >= 500
            semantic = False if uncertain else self._semantic_success(self._json(response), book_id)
            confirmed = self.get_reading_state(book_id)
            if confirmed is None or not (uncertain or semantic):
                return None
            wire = confirmed["wire"]
            modified = _timestamp(wire["LastModified"])
            bookmark_modified = _timestamp(wire["CurrentBookmark"]["LastModified"])
            if (modified < now or bookmark_modified < now
                    or wire["CurrentBookmark"]["Location"] != _location(location)
                    or not math.isclose(confirmed["chapter_progress"], chapter_progress, abs_tol=1e-6)
                    or confirmed["status"] != status):
                return None
            # Total percentage is recomputed by Komga against EPUB positions;
            # return what it persisted, never the requested total as a success.
            return confirmed
