"""Content identity and exact placement for Komga-sourced EPUBs.

These tests use *synthetic* archives built in-memory (never captured Komga
responses) to pin the two things a written Kobo bookmark depends on: the served
KEPUB must be provably the same book as the canonical source EPUB, and a
position must resolve to an exact chapter rather than an approximate one.
"""

import io
import zipfile
from unittest.mock import patch

import pytest

from src.utils import kepub_locator, komga_content
from tests.test_kepub_locator import _build_kepub

SOURCE_DOCS = [
    ("c1.xhtml", "OEBPS/c1.xhtml", []),
    ("c2.xhtml", "OEBPS/c2.xhtml", []),
]

SPAN_DOCS = [
    ("c1.xhtml", "OEBPS/c1.xhtml", ["kobo.1.1", "kobo.1.2", "kobo.1.3"]),
    ("c2.xhtml", "OEBPS/c2.xhtml", ["kobo.1.1", "kobo.1.2"]),
]


@pytest.fixture
def source_epub():
    """A plain EPUB: the same spine documents, with no koboSpans at all."""
    return _build_kepub("OEBPS", SOURCE_DOCS, spans=False)


@pytest.fixture
def served_kepub():
    return _build_kepub("OEBPS", SPAN_DOCS)


# --------------------------------------------------------------- identity key

def test_content_key_is_stable_and_scoped():
    base = dict(server="https://komga.test", principal="p" * 64, book_id="BOOK1",
                revision=("hash", "2026-01-01T00:00:00Z", 10, "LIB1"))
    key = komga_content.content_key(**base)
    assert key == komga_content.content_key(**base)
    for field, value in (
        ("server", "https://other.test"),
        ("principal", "q" * 64),
        ("book_id", "BOOK2"),
        ("revision", ("other", "2026-01-01T00:00:00Z", 10, "LIB1")),
        ("revision", ("hash", "2026-02-01T00:00:00Z", 10, "LIB1")),
        ("revision", ("hash", "2026-01-01T00:00:00Z", 11, "LIB1")),
        ("revision", ("hash", "2026-01-01T00:00:00Z", 10, "LIB2")),
        ("version", komga_content.CONTENT_VERSION + 1),
    ):
        changed = {**base, field: value}
        assert komga_content.content_key(**changed) != key, field


@pytest.mark.parametrize("missing", ["server", "principal", "book_id"])
def test_content_key_requires_every_scope(missing):
    base = dict(server="https://komga.test", principal="p" * 64, book_id="BOOK1", revision=None)
    assert komga_content.content_key(**{**base, missing: ""}) is None


def test_content_key_never_embeds_a_secret():
    key = komga_content.content_key(server="https://komga.test", principal="deadbeef",
                                    book_id="BOOK1", revision=("hash", None, 1, "LIB1"))
    assert "deadbeef" not in key


# ------------------------------------------------------ source identity check

def test_source_spine_hrefs_from_a_plain_epub(source_epub):
    assert komga_content.source_spine_hrefs(source_epub) == ["OEBPS/c1.xhtml", "OEBPS/c2.xhtml"]


@pytest.mark.parametrize("value", [None, b"", b"not a zip"])
def test_source_spine_hrefs_fails_closed(value):
    assert komga_content.source_spine_hrefs(value) == []


def test_served_map_requires_the_source_book(served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    assert [chapter.source_href for chapter in span_map.chapters] == ["OEBPS/c1.xhtml", "OEBPS/c2.xhtml"]


def test_served_map_rejected_when_it_is_a_different_edition(served_kepub):
    """A converted KEPUB whose chapters are not in our source EPUB is not ours."""
    other_source = _build_kepub("OEBPS", [("c9.xhtml", "OEBPS/c9.xhtml", [])], spans=False)
    assert komga_content.build_served_span_map(served_kepub, other_source) is None


def test_served_map_rejected_for_a_plain_epub_or_missing_source(source_epub, served_kepub):
    assert komga_content.build_served_span_map(source_epub, source_epub) is None
    assert komga_content.build_served_span_map(served_kepub, None) is None
    assert komga_content.build_served_span_map(None, source_epub) is None


def test_served_map_rejected_when_the_spine_cannot_be_read(served_kepub):
    assert komga_content.build_served_span_map(served_kepub, b"garbage") is None


# ------------------------------------------------------------ strict placement

def test_strict_chapter_never_suffix_matches(served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    assert komga_content.strict_chapter(span_map, "OEBPS/c1.xhtml").source_href == "OEBPS/c1.xhtml"
    # kepub_locator's own resolver accepts these suffix hits; a write must not.
    assert kepub_locator.resolve_span(span_map, "c1.xhtml") is not None
    assert komga_content.strict_chapter(span_map, "c1.xhtml") is None
    assert komga_content.strict_chapter(span_map, "OEBPS/missing.xhtml") is None
    assert komga_content.strict_chapter(span_map, None) is None


def test_resolve_placement_is_exact_and_reports_the_span_it_chose(served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    placement = komga_content.resolve_placement(span_map, "OEBPS/c1.xhtml", 0.99)
    assert (placement.source_href, placement.span_id) == ("OEBPS/c1.xhtml", "kobo.1.3")
    # The progression reported is where that span actually sits, not the request.
    assert 0 <= placement.chapter_progress < 0.99
    assert placement == komga_content.resolve_placement(span_map, "OEBPS/c1.xhtml", placement.chapter_progress)


@pytest.mark.parametrize("progress", [None, float("nan"), float("inf"), -0.1, 1.1, "half", True])
def test_resolve_placement_rejects_unknown_progress(progress, served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    assert komga_content.resolve_placement(span_map, "OEBPS/c1.xhtml", progress) is None


def test_resolve_placement_has_no_whole_book_fallback(served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    assert komga_content.resolve_placement(span_map, None, 0.5) is None
    assert komga_content.resolve_placement(span_map, "OEBPS/c1.xhtml", None) is None


def test_chapter_progress_for_span_is_chapter_scoped(served_kepub, source_epub):
    span_map = komga_content.build_served_span_map(served_kepub, source_epub)
    first = komga_content.chapter_progress_for_span(span_map, "OEBPS/c1.xhtml", "kobo.1.3")
    second = komga_content.chapter_progress_for_span(span_map, "OEBPS/c2.xhtml", "kobo.1.2")
    assert first is not None and second is not None and first > second
    # The same span id exists in both chapters; the href decides which one.
    assert komga_content.chapter_progress_for_span(span_map, "OEBPS/missing.xhtml", "kobo.1.3") is None
    assert komga_content.chapter_progress_for_span(span_map, "OEBPS/c1.xhtml", "kobo.9.9") is None


def test_source_href_matches_source(source_epub):
    assert komga_content.source_href_matches_source("OEBPS/c1.xhtml", source_epub)
    assert not komga_content.source_href_matches_source("OEBPS/c9.xhtml", source_epub)
    assert not komga_content.source_href_matches_source(None, source_epub)
    assert not komga_content.source_href_matches_source("OEBPS/c1.xhtml", None)


# -------------------------------------------------------------------- caching

def test_content_cache_is_bounded_and_keyed():
    cache = komga_content.ContentCache(capacity=2)
    cache.put("a", "map-a")
    cache.put("b", "map-b")
    assert cache.get("a") == "map-a"
    cache.put("c", "map-c")
    assert cache.get("b") is None  # least recently used evicted
    assert cache.get("c") == "map-c"
    # A cached miss is a value, not an absence: it must not be retried per call.
    cache.put("miss", None)
    assert cache.get("miss") is None
    cache.put("miss2", False)
    assert cache.get("miss2") is False
    cache.put(None, "ignored")
    assert cache.get(None) is None
    cache.clear()
    assert cache.get("a") is None


def test_content_cache_is_thread_safe_under_contention():
    import threading

    cache = komga_content.ContentCache(capacity=4)

    def worker(offset):
        for index in range(200):
            cache.put(f"k{offset}-{index}", index)
            cache.get(f"k{offset}-{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(cache._entries) <= 4


def test_build_map_logs_no_archive_names_when_untrusted(served_kepub):
    with patch.object(komga_content.logger, "warning") as warning:
        assert komga_content.build_served_span_map(served_kepub, _build_kepub("OEBPS", [("c9.xhtml", "OEBPS/c9.xhtml", [])], spans=False)) is None
    assert warning.called


def _zip_names(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.namelist()


def test_synthetic_fixture_is_a_real_archive(served_kepub, source_epub):
    """Guard the fixtures themselves: both are real ZIPs with a readable OPF."""
    assert "META-INF/container.xml" in _zip_names(served_kepub)
    assert "META-INF/container.xml" in _zip_names(source_epub)
