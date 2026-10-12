"""Pinned Komga Kobo wire protocol, conflict and confirmation tests."""
import copy
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from src.api.komga_sync_api import KomgaSyncApi
from tests.test_komga_client import (
    CONFIG,
    _komga_live_server,  # noqa: F401  (pytest fixture: komga_live_server)
    epub,
    response,
)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-10-12T10:00:00Z"
NEW = "2026-10-12T12:00:00Z"
LOCATION = {"Source": "OEBPS/c.xhtml", "Type": "KoboSpan", "Value": "kobo.1.1"}


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def state(timestamp=OLD, pct=20.0, chapter=35.0, status="Reading", location=None, book_id="BOOK1"):
    return {"EntitlementId": book_id, "LastModified": timestamp,
            "CurrentBookmark": {"LastModified": timestamp, "ProgressPercent": pct,
                                "ContentSourceProgressPercent": chapter, "Location": location or copy.deepcopy(LOCATION)},
            "Statistics": {"LastModified": timestamp},
            "StatusInfo": {"LastModified": timestamp, "Status": status}}


def success():
    return {"RequestResult": "Success", "UpdateResults": [{"EntitlementId": "BOOK1",
            "CurrentBookmarkResult": {"Result": "Success"}, "StatisticsResult": {"Result": "Ignored"},
            "StatusInfoResult": {"Result": "Success"}}]}


@pytest.fixture
def api(tmp_path):
    value = KomgaSyncApi(config=CONFIG, data_dir=tmp_path)
    value.session.request = Mock()
    value._now = lambda: NOW
    return value


def expected(api):
    api.session.request.return_value = response([state()])
    result = api.get_reading_state("BOOK1")
    api.session.request.reset_mock()
    return result


def test_normalization_identity_and_distinct_percentages(api):
    api.session.request.return_value = response([state()])
    result = api.get_reading_state("BOOK1")
    assert result["progress_percent"] == .2
    assert result["chapter_progress"] == .35
    assert result["href"] == "OEBPS/c.xhtml"
    assert result["frag"] == "kobo.1.1"
    assert result["service_updated_at"] == OLD
    assert result["wire"] == state()
    assert result["identity"]["book_id"] == "BOOK1"
    assert "bridge-key" not in repr(result)
    assert api.session.request.call_args.args == ("GET", "https://komga.test/reader/kobo/bridge-key/v1/library/BOOK1/state")


@pytest.mark.parametrize("rows", [[], [state(), state()], [state(book_id="OTHER")], {"ReadingStates": [state()]}])
def test_state_exact_identity_and_cardinality(api, rows):
    api.session.request.return_value = response(rows)
    assert api.get_reading_state("BOOK1") is None


@pytest.mark.parametrize("section,key,value", [("CurrentBookmark", "ProgressPercent", None), ("CurrentBookmark", "ProgressPercent", float("nan")), ("CurrentBookmark", "ContentSourceProgressPercent", float("inf")), ("CurrentBookmark", "ProgressPercent", 101), ("StatusInfo", "Status", "Unknown"), ("Statistics", "LastModified", None), ("StatusInfo", "LastModified", "tomorrow"), ("CurrentBookmark", "LastModified", "2026-10-13T00:00:00Z"), ("CurrentBookmark", "Location", None), ("CurrentBookmark", "Location", {**LOCATION, "Value": None}), ("CurrentBookmark", "Location", {**LOCATION, "Type": "Unknown"})])
def test_malformed_or_future_state_rejected(api, section, key, value):
    row = state()
    row[section][key] = value
    api.session.request.return_value = response([row])
    assert api.get_reading_state("BOOK1") is None


def test_put_full_timestamped_payload_and_return_readback_not_request(api):
    old = expected(api)
    confirmed = state(NEW, 49.9, 70.0)
    api.session.request.side_effect = [response([state()]), response(success()), response([confirmed])]
    result = api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old)
    assert result["progress_percent"] == .499
    assert result["wire"] == confirmed
    calls = api.session.request.call_args_list
    assert [c.args[0] for c in calls] == ["GET", "PUT", "GET"]
    sent = calls[1].kwargs["json"]["ReadingStates"][0]
    assert sent["EntitlementId"] == "BOOK1"
    for component in [sent, sent["CurrentBookmark"], sent["Statistics"], sent["StatusInfo"]]:
        assert component["LastModified"] == NEW
    assert sent["CurrentBookmark"]["ProgressPercent"] == 50
    assert sent["CurrentBookmark"]["ContentSourceProgressPercent"] == 70
    assert sent["CurrentBookmark"]["Location"] == LOCATION
    assert sent["StatusInfo"]["Status"] == "Reading"
    assert all(c.args[1].endswith("/library/BOOK1/state") for c in calls)


def test_preflight_conflict_never_puts(api):
    old = expected(api)
    api.session.request.return_value = response([state("2026-10-12T11:00:00Z", 30)])
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET"]


@pytest.mark.parametrize("changes", [{"location": None}, {"chapter_progress": None}, {"progress_percent": float("nan")}, {"status": "Unknown"}, {"expected_state": None}, {"location": {**LOCATION, "Source": ""}}])
def test_invalid_update_has_no_network(api, changes):
    values = {"progress_percent": .5, "location": LOCATION, "chapter_progress": .7, "expected_state": expected(api)}
    values.update(changes)
    assert api.update_reading_state("BOOK1", **values) is None
    api.session.request.assert_not_called()


@pytest.mark.parametrize("mutate", ["request_failure", "bookmark_ignored", "status_failure", "statistics_failure", "other_id", "missing_results", "duplicate_results"])
def test_semantic_failure_even_http200(api, mutate):
    old = expected(api)
    data = success()
    item = data["UpdateResults"][0]
    if mutate == "request_failure":
        data["RequestResult"] = "Failure"
    elif mutate == "bookmark_ignored":
        item["CurrentBookmarkResult"]["Result"] = "Ignored"
    elif mutate == "status_failure":
        item["StatusInfoResult"]["Result"] = "Failure"
    elif mutate == "statistics_failure":
        item["StatisticsResult"]["Result"] = "Failure"
    elif mutate == "other_id":
        item["EntitlementId"] = "OTHER"
    elif mutate == "missing_results":
        data.pop("UpdateResults")
    elif mutate == "duplicate_results":
        data["UpdateResults"].append(item)
    api.session.request.side_effect = [response([state()]), response(data), response([state(NEW, 50, 70)])]
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("ambiguous", [requests.ReadTimeout("/kobo/bridge-key"), requests.ConnectionError("/kobo/bridge-key")])
def test_ambiguous_put_readback_without_retry(api, ambiguous):
    old = expected(api)
    api.session.request.side_effect = [response([state()]), ambiguous, response([state(NEW, 49.9, 70)])]
    result = api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old)
    assert result["progress_percent"] == .499
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("row", [state(), state(NEW, 50, 70, location={**LOCATION, "Value": "kobo.2.1"}), state(NEW, 50, 70, book_id="OTHER"), state(NEW, 50, 40), state(NEW, 50, 70, status="Finished")])
def test_success_or_timeout_without_exact_confirmation_fails(api, row):
    old = expected(api)
    api.session.request.side_effect = [response([state()]), response(success()), response([row])]
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None


def metadata(url="https://komga.test/reader/kobo/bridge-key/v1/books/BOOK1/file/epub?convert_kepub=true", fmt="KEPUB", book_id="BOOK1"):
    return {"EntitlementId": book_id, "RevisionId": book_id, "Title": "Title", "DownloadUrls": [{"Format": fmt, "Url": url, "Size": 100}]}


def test_advertised_kepub_keeps_conversion_query_and_proxy_prefix(api):
    payload = epub()
    api.session.request.side_effect = [response([metadata()]), response(content=payload)]
    assert api.download_kepub("BOOK1") == payload
    assert api.session.request.call_args.args == ("GET", metadata()["DownloadUrls"][0]["Url"])
    assert api.session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("row", [metadata(fmt="EPUB3"), metadata(book_id="OTHER"), metadata(url="https://evil.test/kobo/bridge-key/v1/books/BOOK1/file/epub?convert_kepub=true"), metadata(url="https://komga.test/reader/kobo/other-key/v1/books/BOOK1/file/epub?convert_kepub=true"), metadata(url="https://komga.test/reader/kobo/bridge-key/v1/books/OTHER/file/epub?convert_kepub=true"), metadata(url="https://komga.test/reader/kobo/bridge-key/v1/books/BOOK1/file/epub"), metadata(url="https://komga.test/reader/../kobo/bridge-key/v1/books/BOOK1/file/epub?convert_kepub=true")])
def test_unsupported_or_unsafe_download_never_fetched(api, row):
    api.session.request.return_value = response([row])
    assert api.download_kepub("BOOK1") is None
    assert api.session.request.call_count == 1


def test_metadata_cardinality(api):
    api.session.request.return_value = response([metadata(), metadata()])
    assert api.get_metadata("BOOK1") is None


def test_disabled_sync_has_no_requests():
    value = KomgaSyncApi(config={**CONFIG, "KOMGA_ENABLED": "false"})
    value.session.request = Mock()
    assert value.get_reading_state("BOOK1") is None
    assert value.download_kepub("BOOK1") is None
    value.session.request.assert_not_called()


@pytest.mark.parametrize("gate", ["KOMGA_SYNC_ENABLED", "KOMGA_REVERSE_SYNC_ENABLED"])
@pytest.mark.parametrize("value", ["true", "", None, "TRUE"])
def test_absent_blank_or_enabled_gates_do_not_block(gate, value):
    value_config = {**CONFIG, gate: value}
    assert KomgaSyncApi(config=value_config).is_configured()


@pytest.mark.parametrize("suffix", ["off", "no", "0"])
def test_project_falsey_tokens_disable_the_gate(suffix):
    value = KomgaSyncApi(config={**CONFIG, "KOMGA_SYNC_ENABLED": suffix})
    assert not value.is_configured()


def test_komga_enabled_is_the_primary_gate():
    value = KomgaSyncApi(config=CONFIG)
    assert value.is_configured()


@pytest.mark.parametrize("flag,expected", [("true", True), ("1", True), ("yes", True), ("on", True),
                                          ("false", False), ("0", False), ("", False), (None, False)])
def test_enable_flag_spellings(flag, expected):
    config = {key: value for key, value in CONFIG.items()}
    config["KOMGA_ENABLED"] = flag
    assert KomgaSyncApi(config=config).is_configured() is expected


def test_explicit_sync_gate_off_disables_every_read(tmp_path):
    value = KomgaSyncApi(config={**CONFIG, "KOMGA_SYNC_ENABLED": "false"}, data_dir=tmp_path)
    value.session.request = Mock()
    assert not value.is_configured()
    assert value.get_reading_state("BOOK1") is None
    assert value.download_kepub("BOOK1") is None
    value.session.request.assert_not_called()


def test_reverse_write_gate_blocks_writes_but_not_reads(api, tmp_path):
    gated = KomgaSyncApi(config={**CONFIG, "KOMGA_REVERSE_SYNC_ENABLED": "false"}, data_dir=tmp_path)
    gated.session.request = Mock(return_value=response([state()]))
    gated._now = lambda: NOW
    assert gated.is_configured()
    old = gated.get_reading_state("BOOK1")
    assert old["progress_percent"] == .2
    gated.session.request.reset_mock()
    assert gated.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    gated.session.request.assert_not_called()


def test_equal_timestamp_preflight_refuses_without_put(api):
    current = state(NEW, 20, 35)
    api.session.request.return_value = response([current])
    old = api.get_reading_state("BOOK1")
    api.session.request.reset_mock()
    api.session.request.return_value = response([current])
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET"]


def test_foreign_expected_state_is_refused(api):
    old = expected(api)
    foreign = dict(old, identity={**old["identity"], "principal": "0" * 64})
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=foreign) is None
    api.session.request.assert_not_called()


@pytest.mark.parametrize("code", [400, 404, 422])
def test_definitive_client_rejection_skips_readback(api, code):
    old = expected(api)
    api.session.request.side_effect = [response([state()]), response(status=code, content=b""), response([state(NEW, 50, 70)])]
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET", "PUT"]


def test_server_error_is_uncertain_and_confirmed_by_readback(api):
    old = expected(api)
    api.session.request.side_effect = [response([state()]), response(status=503, content=b"busy"), response([state(NEW, 50, 70)])]
    result = api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old)
    assert result["progress_percent"] == .5
    assert result["identity"] == old["identity"]


def test_timeout_without_confirmation_is_not_retried(api):
    old = expected(api)
    api.session.request.side_effect = [response([state()]), requests.ReadTimeout("/kobo"), response([state()])]
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [c.args[0] for c in api.session.request.call_args_list] == ["GET", "PUT", "GET"]


def test_wire_drops_unknown_response_fields(api):
    row = state()
    row["Secret"] = "token=abc"
    row["CurrentBookmark"]["DeviceId"] = "device"
    row["CurrentBookmark"]["Location"]["Extra"] = "https://komga.test/kobo/bridge-key"
    api.session.request.return_value = response([row])
    result = api.get_reading_state("BOOK1")
    assert set(result["wire"]) == {"EntitlementId", "LastModified", "CurrentBookmark", "Statistics", "StatusInfo"}
    assert result["wire"]["CurrentBookmark"]["Location"] == LOCATION
    assert "bridge-key" not in json.dumps(result)


def test_metadata_output_redacts_the_shared_key(api):
    api.session.request.return_value = response([metadata()])
    result = api.get_metadata("BOOK1")
    assert result["EntitlementId"] == "BOOK1"
    assert "<redacted>" in result["DownloadUrls"][0]["Url"]
    assert "bridge-key" not in json.dumps(result)


def test_kepub_candidate_cardinality(api):
    row = metadata()
    row["DownloadUrls"].append(copy.deepcopy(row["DownloadUrls"][0]))
    api.session.request.return_value = response([row])
    assert api.download_kepub("BOOK1") is None
    assert api.session.request.call_count == 1


def test_identity_is_scoped_to_the_bridge_key(tmp_path):
    first = KomgaSyncApi(config={**CONFIG}, data_dir=tmp_path)
    second = KomgaSyncApi(config={**CONFIG, "KOMGA_API_KEY": "other-key"}, data_dir=tmp_path)
    past = state("2020-01-01T00:00:00Z")
    first.session.request = Mock(return_value=response([past]))
    second.session.request = Mock(return_value=response([past]))
    assert first.get_reading_state("BOOK1")["identity"] != second.get_reading_state("BOOK1")["identity"]


def test_check_connection_does_not_trigger_a_library_sync(api):
    api.session.request.return_value = response({"Resources": {}})
    assert api.check_connection() is True
    assert api.session.request.call_args.args == ("GET", "https://komga.test/reader/kobo/bridge-key/v1/initialization")
    assert "sync" not in api.session.request.call_args.args[1]


def test_live_state_update_and_kepub_download_end_to_end(komga_live_server):
    """Full real-HTTP round trip: preflight, timestamped PUT, exact readback."""
    komga_live_server["state"] = [state("2020-01-01T00:00:00Z", 20, 35)]
    advertised = metadata("http://127.0.0.1:0/placeholder?convert_kepub=true")
    komga_live_server["metadata"] = [advertised]
    api = KomgaSyncApi(config={**CONFIG, "KOMGA_SERVER": komga_live_server["url"]}, data_dir=komga_live_server["dir"])
    old = api.get_reading_state("BOOK1")
    assert old["progress_percent"] == .2
    # Advertised URL must live on the configured origin; rewrite it now that the port is known.
    advertised["DownloadUrls"][0]["Url"] = (f"{komga_live_server['url']}/kobo/bridge-key/v1/books/BOOK1/file/epub"
                                           "?convert_kepub=true")
    assert api.check_connection() is True
    assert api.download_kepub("BOOK1") == komga_live_server["content"]
    assert any(path.endswith("?convert_kepub=true") for _, path, _ in komga_live_server["seen"])
    result = api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old)
    assert result["progress_percent"] == .5
    assert result["chapter_progress"] == .7
    assert result["href"] == LOCATION["Source"] and result["frag"] == LOCATION["Value"]
    assert result["identity"] == old["identity"]
    submitted = komga_live_server["state"][0]
    for component in (submitted, submitted["CurrentBookmark"], submitted["Statistics"], submitted["StatusInfo"]):
        assert _parse(component["LastModified"]) >= _parse(old["service_updated_at"])
    assert all(key == "bridge-key" for _, _, key in komga_live_server["seen"])
    assert not any("evil.test" in path for _, path, _ in komga_live_server["seen"])


def test_live_conflict_leaves_the_server_untouched(komga_live_server):
    komga_live_server["state"] = [state("2020-01-01T00:00:00Z", 20, 35)]
    api = KomgaSyncApi(config={**CONFIG, "KOMGA_SERVER": komga_live_server["url"]}, data_dir=komga_live_server["dir"])
    old = api.get_reading_state("BOOK1")
    komga_live_server["state"] = [state("2020-01-02T00:00:00Z", 30, 35)]
    komga_live_server["seen"].clear()
    assert api.update_reading_state("BOOK1", .5, location=LOCATION, chapter_progress=.7, expected_state=old) is None
    assert [method for method, _, _ in komga_live_server["seen"]] == ["GET"]
    assert komga_live_server["state"][0]["CurrentBookmark"]["ProgressPercent"] == 30
