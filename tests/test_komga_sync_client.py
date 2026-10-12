"""Komga progress adapter: gates, read mapping, exact placement and confirmation.

Exercised against the *real* :class:`KomgaSyncApi` / :class:`KomgaClient` with only
``requests.Session.request`` replaced, so the pinned wire payload, the preflight,
the read-back comparison and the content verification all run as shipped. The
KEPUB and source EPUB are synthetic archives built in-memory, never captured
Komga responses.
"""

import copy
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.komga_client import KomgaClient
from src.api.komga_sync_api import KomgaSyncApi
from src.services import write_tracker
from src.sync_clients.komga_sync_client import KomgaSyncClient, komga_ebook_filename
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from tests.test_kepub_locator import CONTAINER, _chapter_html
from tests.test_komga_client import book as catalog_row
from tests.test_komga_client import response

SERVER = "https://komga.test/reader"
KEY = "bridge-key"
BOOK_ID = "BOOK1"
CH1 = "OEBPS/c1.xhtml"
CH2 = "OEBPS/c2.xhtml"

SOURCE_DOCS = [("c1.xhtml", CH1, []), ("c2.xhtml", CH2, [])]
SPAN_DOCS = [("c1.xhtml", CH1, ["kobo.1.1", "kobo.1.2", "kobo.1.3"]),
             ("c2.xhtml", CH2, ["kobo.1.1", "kobo.1.2"])]


def epub_bytes(docs, *, spans: bool) -> bytes:
    """A minimal EPUB that also satisfies the transport's archive validation.

    ``spans=True`` produces the converted KEPUB Komga would serve (koboSpan
    elements); ``spans=False`` the plain source EPUB the bridge aligns against.
    """
    items = "".join(
        f'<item id="c{index}" href="{href}" media-type="application/xhtml+xml"/>'
        for index, (href, _path, _ids) in enumerate(docs)
    )
    refs = "".join(f'<itemref idref="c{index}"/>' for index in range(len(docs)))
    opf = ('<?xml version="1.0"?><package version="3.0" xmlns="http://www.idpf.org/2007/opf">'
           f"<manifest>{items}</manifest><spine>{refs}</spine></package>")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER.format(opf="OEBPS/content.opf"))
        archive.writestr("OEBPS/content.opf", opf)
        for _href, path, ids in docs:
            archive.writestr(path, _chapter_html(*ids) if spans
                             else b"<html><body><p>Plain source text.</p></body></html>")
    return output.getvalue()

OLD_DT = datetime.now(timezone.utc) - timedelta(hours=2)
OLD_TS = OLD_DT.isoformat(timespec="seconds").replace("+00:00", "Z")
OLD_EPOCH = OLD_DT.timestamp()

_MISSING = object()


def state(ts=OLD_TS, pct=20.0, chapter=35.0, status="Reading", source=CH1, span="kobo.1.1") -> dict:
    return {"EntitlementId": BOOK_ID, "LastModified": ts,
            "CurrentBookmark": {"LastModified": ts, "ProgressPercent": pct,
                                "ContentSourceProgressPercent": chapter,
                                "Location": {"Source": source, "Type": "KoboSpan", "Value": span}},
            "Statistics": {"LastModified": ts},
            "StatusInfo": {"LastModified": ts, "Status": status}}


def success() -> dict:
    return {"RequestResult": "Success", "UpdateResults": [{
        "EntitlementId": BOOK_ID, "CurrentBookmarkResult": {"Result": "Success"},
        "StatisticsResult": {"Result": "Ignored"}, "StatusInfoResult": {"Result": "Success"}}]}


def komga_book(**overrides) -> SimpleNamespace:
    fields = dict(abs_id="audio-1", abs_title="A Book", ebook_source="Komga",
                  ebook_source_id=BOOK_ID, ebook_filename=komga_ebook_filename(BOOK_ID),
                  original_ebook_filename=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


class Server:
    """Stand-in for the Komga Kobo routes this adapter uses.

    The PUT stores the submitted DTO as the real service would — optionally
    transformed by ``put_transform``, so "accepted but stored something else" is
    modelled honestly — which is what the read-back comparison then judges.
    """

    def __init__(self, *, kepub, persisted=None, put_result=None, put_transform=None):
        self.calls = []
        self.kepub = kepub
        self.persisted = copy.deepcopy(persisted) if persisted is not None else state()
        self.put_result = put_result if put_result is not None else success()
        self.put_transform = put_transform

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/state"):
            if method == "GET":
                return response([copy.deepcopy(self.persisted)])
            submitted = copy.deepcopy(kwargs["json"]["ReadingStates"][0])
            if self.put_transform is not None:
                submitted = self.put_transform(submitted)
            if self.put_result is None:
                return response({"RequestResult": "Failure"})
            self.persisted = submitted
            return response(copy.deepcopy(self.put_result))
        if url.endswith("/metadata"):
            # Advertise the download for whichever key authenticated, as the
            # real controller does (the token is part of the advertised URL).
            key = re.search(r"/kobo/([^/]+)/", url).group(1)
            return response([{"EntitlementId": BOOK_ID, "DownloadUrls": [
                {"Format": "KEPUB",
                 "Url": f"{SERVER}/kobo/{key}/v1/books/{BOOK_ID}/file/epub?convert_kepub=true"}]}])
        if "file/epub" in url:
            return response(content=self.kepub, headers={"Content-Length": str(len(self.kepub))})
        raise AssertionError(f"unexpected Komga request: {method} {url}")

    def methods(self):
        return [call[0] for call in self.calls]


class Catalog:
    """Komga REST catalog stand-in (exact-ID lookup, then the original file)."""

    def __init__(self, revision="hash-1", serve=True, source_bytes=b""):
        self.revision = revision
        self.serve = serve
        self.source_bytes = source_bytes
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        if not self.serve:
            return response(status=403)
        if "/file" in url:
            return response(content=self.source_bytes,
                            headers={"Content-Length": str(len(self.source_bytes))})
        return response(catalog_row(BOOK_ID, fileHash=self.revision))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("KOMGA_KOBO_SPAN_SYNC", "true")
    for key in ("KOMGA_ENABLED", "KOMGA_SYNC_ENABLED", "KOMGA_REVERSE_SYNC_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()

    def build(*, reverse="true", sync="true", enabled="true", api_key=KEY, user_id=None,
              revision="hash-1", served=True, source=True, persisted=None,
              put_result=_MISSING, put_transform=None):
        config = {"KOMGA_ENABLED": enabled, "KOMGA_SYNC_ENABLED": sync,
                  "KOMGA_REVERSE_SYNC_ENABLED": reverse, "KOMGA_SERVER": SERVER,
                  "KOMGA_API_KEY": api_key}
        api = KomgaSyncApi(config=config, data_dir=tmp_path)
        client = KomgaClient(data_dir=tmp_path, config=config, books_dir=tmp_path / "cache")
        source_bytes = epub_bytes(SOURCE_DOCS, spans=False)
        server = Server(kepub=epub_bytes(SPAN_DOCS, spans=True) if served else b"",
                        persisted=persisted, put_transform=put_transform,
                        **({} if put_result is _MISSING else {"put_result": put_result}))
        catalog = Catalog(revision=revision, source_bytes=source_bytes)
        api.session.request = Mock(side_effect=server)
        client.session.request = Mock(side_effect=catalog)
        if source:
            directory = tmp_path / "cache"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / komga_ebook_filename(BOOK_ID)).write_bytes(source_bytes)
        adapter = KomgaSyncClient(api, client, Mock(), user_id=user_id, database_service=Mock())
        return SimpleNamespace(adapter=adapter, api=api, client=client, server=server,
                               catalog=catalog, config=config, tmp_path=tmp_path)
    yield build
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()


def write(adapter, *, pct=.5, href=CH1, chapter_progress=.5):
    return adapter.update_progress(komga_book(), UpdateProgressRequest(
        locator_result=LocatorResult(percentage=pct, href=href, chapter_progress=chapter_progress)))


# --------------------------------------------------------------------- gates

@pytest.mark.parametrize("reverse", ["false", "0", "off", "no"])
def test_reverse_writes_are_off_by_default_and_touch_nothing(harness, reverse):
    parts = harness(reverse=reverse)
    assert not parts.adapter.komga_sync_api.reverse_writes_enabled()
    result = write(parts.adapter)
    assert result.success is True and result.skipped is True and result.location == .5
    # Nothing was read this cycle, so the declined write records no position at
    # all — never the percentage the bridge chose not to write.
    assert result.updated_state == {"pct": None}
    assert parts.server.calls == [] and parts.catalog.calls == []


def test_a_declined_write_records_what_was_actually_read(harness):
    """The sync manager persists a successful follower result as that state row,
    so a skipped Komga write must carry this cycle's genuine read of Komga."""
    parts = harness(reverse="false")
    parts.server.persisted = state(pct=20.0, chapter=35.0, span="kobo.1.2")
    parts.adapter.get_service_state(komga_book(), None)
    parts.server.calls.clear()

    result = write(parts.adapter, pct=.9, href=CH1, chapter_progress=.5)

    assert result.skipped is True
    assert result.updated_state["pct"] == pytest.approx(.2)
    assert result.updated_state["href"] == CH1
    assert result.updated_state["frag"] == "kobo.1.2"
    assert result.updated_state["chapter_progress"] == pytest.approx(.35)
    assert parts.server.calls == []


def test_disabled_source_never_reads_or_writes(harness):
    parts = harness(enabled="false", reverse="true")
    assert parts.adapter.is_configured() is False
    assert parts.adapter.get_service_state(komga_book(), None) is None
    # A switched-off adapter is an intentional no-write, not a failed one.
    result = write(parts.adapter)
    assert result.skipped is True and result.success is True
    assert parts.server.calls == []


def test_supports_book_only_for_an_explicit_komga_id(harness):
    adapter = harness().adapter
    assert adapter.supports_book(komga_book())
    assert adapter.supports_book(komga_book(ebook_source="komga"))
    assert not adapter.supports_book(komga_book(ebook_source="CWA"))
    assert not adapter.supports_book(komga_book(ebook_source_id=None))
    assert not adapter.supports_book(komga_book(ebook_source_id="../../etc/passwd"))
    assert adapter.get_supported_sync_types() == {"audiobook", "ebook"}


# ---------------------------------------------------------------------- read

def test_service_state_maps_progress_without_persisting_wire_data(harness):
    parts = harness()
    parts.server.persisted = state(pct=20.0, chapter=35.0, status="Reading")
    service = parts.adapter.get_service_state(komga_book(), SimpleNamespace(percentage=.1))
    assert service.current["pct"] == pytest.approx(.2)
    assert service.current["chapter_progress"] == pytest.approx(.35)
    assert service.current["href"] == CH1 and service.current["frag"] == "kobo.1.1"
    assert service.current["status"] == "Reading"
    assert service.current["service_updated_at"] == pytest.approx(OLD_EPOCH)
    assert service.previous_pct == .1 and service.delta == pytest.approx(.1)
    assert service.display[0] == "Komga"
    # The wire DTO is a write-side artifact; it must not leak into the persisted
    # fingerprint dict the sync manager stores.
    assert "wire" not in service.current and "identity" not in service.current
    assert parts.server.methods() == ["GET"]
    assert parts.catalog.calls == []


@pytest.mark.parametrize("persisted", [
    state(pct=None), state(chapter=None), state(status="Unknown"), state(span=None),
    state(source=None), state(ts=None), {"ReadingStates": [state()]},
])
def test_unknown_state_is_no_state_never_a_reset(harness, persisted):
    parts = harness(persisted=persisted)
    assert parts.adapter.get_service_state(komga_book(), SimpleNamespace(percentage=.9)) is None


def test_read_path_does_not_download_the_kepub(harness):
    parts = harness()
    parts.adapter.get_service_state(komga_book(), None)
    assert parts.server.methods() == ["GET"]
    assert parts.server.calls[0][1].endswith("/state")
    assert parts.catalog.calls == []


def test_chapter_progress_prefers_a_verified_map_once_cached(harness):
    parts = harness()
    adapter = parts.adapter
    write(adapter)  # a write path verifies and caches the served span map
    parts.server.calls.clear()
    parts.server.persisted = state(pct=50.0, chapter=1.0, source=CH1, span="kobo.1.3")
    service = adapter.get_service_state(komga_book(), None)
    # The span's own position wins over the (deliberately wrong) reported value.
    assert service.current["chapter_progress"] < .99
    assert parts.server.methods() == ["GET"]


def test_text_lookup_uses_the_canonical_local_filename(harness):
    parts = harness()
    parser = Mock()
    parts.adapter.ebook_parser = parser
    parts.adapter.get_text_from_current_state(komga_book(), SimpleNamespace(current={"pct": .4}))
    parser.get_text_at_percentage.assert_called_once_with(komga_ebook_filename(BOOK_ID), .4)


def test_invalid_book_or_missing_percentage_is_a_failure_not_a_skip(harness):
    parts = harness()
    assert write(parts.adapter, pct=None).success is False
    assert parts.adapter.update_progress(
        komga_book(ebook_source_id="../bad"),
        UpdateProgressRequest(locator_result=LocatorResult(percentage=.5))).success is False
    for bad in (float("nan"), float("inf"), float("-inf"), -0.1):
        assert write(parts.adapter, pct=bad).success is False
    assert parts.server.calls == []


# ------------------------------------------------------------ exact placement

@pytest.mark.parametrize("locator", [
    LocatorResult(percentage=.5),
    LocatorResult(percentage=.5, href=CH1),
    LocatorResult(percentage=.5, chapter_progress=.5),
    LocatorResult(percentage=.5, href=CH1, chapter_progress=None),
])
def test_percentage_only_locator_skips_without_a_download(harness, locator):
    parts = harness()
    result = parts.adapter.update_progress(komga_book(), UpdateProgressRequest(locator_result=locator))
    assert result.skipped is True and result.success is True
    assert parts.server.calls == [] and parts.catalog.calls == []


def test_unverified_source_epub_skips_even_with_a_full_locator(harness):
    """Bytes whose chapters are not our source EPUB must not produce a bookmark."""
    parts = harness(source=False)
    parts.catalog.source_bytes = b"not an epub"
    result = write(parts.adapter)
    assert result.skipped is True
    assert "PUT" not in parts.server.methods()


def test_span_sync_can_be_turned_off(harness, monkeypatch):
    monkeypatch.setenv("KOMGA_KOBO_SPAN_SYNC", "false")
    parts = harness()
    result = write(parts.adapter)
    assert result.skipped is True
    assert parts.server.calls == []


def test_unknown_chapter_href_is_never_written(harness):
    parts = harness()
    result = write(parts.adapter, href="OEBPS/other.xhtml", chapter_progress=.5)
    assert result.skipped is True
    assert "PUT" not in parts.server.methods()


# --------------------------------------------------------------------- write

def test_confirmed_write_sends_exact_served_span_and_returns_readback(harness):
    parts = harness(user_id=7)
    adapter = parts.adapter
    adapter.get_service_state(komga_book(), None)  # the cycle's baseline read
    parts.server.calls.clear()

    result = write(adapter, pct=.5, chapter_progress=.99)

    assert result.success is True and result.skipped is False
    # The result reports what the service stored and re-read, not the request
    # (Komga recomputes the total from its own positions — see the API contract test).
    assert result.location == pytest.approx(result.updated_state["pct"])
    assert result.updated_state["href"] == CH1
    assert result.updated_state["frag"] == "kobo.1.3"

    puts = [call for call in parts.server.calls if call[0] == "PUT"]
    assert len(puts) == 1
    sent = puts[0][2]["json"]["ReadingStates"][0]
    assert sent["CurrentBookmark"]["Location"] == {"Source": CH1, "Type": "KoboSpan", "Value": "kobo.1.3"}
    assert sent["CurrentBookmark"]["ProgressPercent"] == pytest.approx(50)
    assert sent["CurrentBookmark"]["ContentSourceProgressPercent"] < 99
    assert sent["StatusInfo"]["Status"] == "Reading"
    for component in (sent, sent["CurrentBookmark"], sent["Statistics"], sent["StatusInfo"]):
        assert component["LastModified"]
    # metadata, served KEPUB, preflight, PUT, read-back.
    assert parts.server.methods() == ["GET", "GET", "GET", "PUT", "GET"]
    assert write_tracker.get_recent_write("Komga", "audio-1", suppression_window=300, user_id=7) is not None


def test_write_reports_the_served_total_not_the_requested_one(harness):
    """Komga derives the whole-book percentage; the bridge must not echo its own."""
    def recompute(row):
        bookmark = dict(row["CurrentBookmark"])
        bookmark["ProgressPercent"] = 49.9
        return {**row, "CurrentBookmark": bookmark}

    parts = harness(put_transform=recompute)
    parts.adapter.get_service_state(komga_book(), None)
    result = write(parts.adapter, pct=.5)
    assert result.success is True
    assert result.location == pytest.approx(.499)
    assert result.updated_state["pct"] == pytest.approx(.499)


def test_rejected_put_is_a_failure_and_records_no_write(harness):
    parts = harness()
    parts.adapter.get_service_state(komga_book(), None)
    parts.server.put_result = None  # RequestResult != Success
    result = write(parts.adapter)
    assert result.success is False
    assert result.updated_state == {"pct": .5}
    assert parts.server.methods().count("PUT") == 1
    assert write_tracker.get_recent_write("Komga", "audio-1", suppression_window=300) is None


def test_accepted_put_with_a_different_stored_location_is_not_confirmed(harness):
    def move_elsewhere(row):
        bookmark = dict(row["CurrentBookmark"])
        bookmark["Location"] = {"Source": CH2, "Type": "KoboSpan", "Value": "kobo.1.2"}
        return {**row, "CurrentBookmark": bookmark}

    parts = harness(put_transform=move_elsewhere)
    parts.adapter.get_service_state(komga_book(), None)
    result = write(parts.adapter)
    assert result.success is False
    assert write_tracker.get_recent_write("Komga", "audio-1", suppression_window=300) is None


def test_no_baseline_is_re_read_and_a_refused_read_skips_the_write(harness):
    parts = harness()
    original = parts.server.__call__

    def refuse_state(method, url, **kwargs):
        if method == "GET" and url.endswith("/state"):
            parts.server.calls.append((method, url, kwargs))
            return response(status=403)
        return original(method, url, **kwargs)

    parts.api.session.request = Mock(side_effect=refuse_state)
    result = write(parts.adapter)
    assert result.skipped is True
    assert "PUT" not in parts.server.methods()


def test_credential_rotation_between_read_and_write_is_refused(harness):
    parts = harness()
    parts.adapter.get_service_state(komga_book(), None)
    assert parts.adapter._last_reads[BOOK_ID]["identity"]["book_id"] == BOOK_ID
    # The reader's key is replaced; the cached baseline belongs to the old principal.
    parts.config["KOMGA_API_KEY"] = "rotated-key"
    result = write(parts.adapter)
    assert result.success is False
    assert "PUT" not in parts.server.methods()


@pytest.mark.parametrize("pct,expected", [(0.0, "ReadyToRead"), (0.01, "Reading"),
                                          (0.98, "Reading"), (0.99, "Finished"), (1.0, "Finished")])
def test_status_derivation(harness, pct, expected):
    assert harness().adapter._status_for(pct) == expected


def test_content_revision_change_invalidates_the_cached_span_map(harness):
    parts = harness()
    adapter = parts.adapter
    adapter.get_service_state(komga_book(), None)
    write(adapter)

    # Same revision: the verified map is reused, so no metadata/KEPUB download.
    parts.server.calls.clear()
    write(adapter, pct=.6)
    assert parts.server.methods() == ["GET", "PUT", "GET"]

    # A changed catalog revision is a different content key: re-download+verify.
    parts.catalog.revision = "hash-2"
    parts.server.calls.clear()
    write(adapter, pct=.7)
    assert parts.server.methods().count("GET") == 4
    assert parts.server.methods().count("PUT") == 1


def test_readers_keep_independent_echo_namespaces(harness):
    for user_id in (1, 2):
        parts = harness(user_id=user_id)
        parts.adapter.get_service_state(komga_book(), None)
        write(parts.adapter)
    for user_id in (1, 2):
        assert write_tracker.get_recent_write(
            "Komga", "audio-1", suppression_window=300, user_id=user_id) is not None


def test_check_connection_delegates(harness):
    parts = harness()
    parts.api.session.request = Mock(side_effect=lambda method, url, **kw: response({"Resources": {}}))
    assert parts.adapter.check_connection() is True
    parts.api.session.request = Mock(side_effect=lambda method, url, **kw: response({}))
    assert parts.adapter.check_connection() is False


def test_per_book_bookkeeping_stays_bounded(harness):
    """A long-lived adapter must not accumulate one entry per book forever."""
    from src.sync_clients.komga_sync_client import BOOK_STATE_CAPACITY

    parts = harness()
    adapter = parts.adapter
    for index in range(BOOK_STATE_CAPACITY + 10):
        adapter._remember(adapter._last_reads, f"book-{index}", {"read": index})
        adapter._remember(adapter._content_keys, f"book-{index}", ("principal", f"key-{index}"))
    assert len(adapter._last_reads) == BOOK_STATE_CAPACITY
    assert len(adapter._content_keys) == BOOK_STATE_CAPACITY
    assert "book-0" not in adapter._last_reads
    assert f"book-{BOOK_STATE_CAPACITY + 9}" in adapter._last_reads
