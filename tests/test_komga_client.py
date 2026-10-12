"""Mocked pinned Komga catalog and acquisition contracts."""
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
import requests

from src.api.komga_client import KomgaClient

CONFIG = {"KOMGA_ENABLED": "true", "KOMGA_SERVER": "https://komga.test/reader",
          "KOMGA_API_KEY": "bridge-key"}


def response(data=None, content=b"", status=200, headers=None):
    """Stand-in for a streaming response; JSON bodies are served as raw bytes."""
    payload = content if content else (json.dumps(data).encode() if data is not None else b"")
    result = Mock(status_code=status, headers=dict(headers or {}))
    result.iter_content.return_value = [payload]
    result.json.return_value = data
    return result


def epub(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        archive.writestr("OEBPS/book.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>')
        archive.writestr("OEBPS/c.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="kobo.1.1">Hello</p></body></html>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def book(book_id="BOOK1", **kwargs):
    return {"id": book_id, "name": "A Book", "libraryId": "LIB1", "seriesId": "SER1",
            "seriesTitle": "Series", "deleted": False, "sizeBytes": 100,
            "fileHash": "hash", "fileLastModified": "2026-01-01T00:00:00Z",
            "media": {"mediaType": "application/epub+zip", "status": "READY"},
            "metadata": {"title": "Title", "authors": [{"name": "Writer", "role": "writer"}]}, **kwargs}


@pytest.fixture
def client(tmp_path):
    value = KomgaClient(data_dir=tmp_path, config=CONFIG)
    value.session.request = Mock()
    return value


def test_disabled_is_opt_in_and_never_calls_out(tmp_path):
    value = KomgaClient(data_dir=tmp_path, config={**CONFIG, "KOMGA_ENABLED": "false"})
    value.session.request = Mock()
    assert not value.is_configured()
    assert value.get_book("BOOK1") is None
    assert value.search_ebooks("book") == []
    assert value.download_ebook("BOOK1") is None
    value.session.request.assert_not_called()
    assert not KomgaClient(config={k: v for k, v in CONFIG.items() if k != "KOMGA_ENABLED"}).is_configured()


def test_explicit_sync_gate_off_disables_the_catalog(tmp_path):
    value = KomgaClient(data_dir=tmp_path, config={**CONFIG, "KOMGA_SYNC_ENABLED": "false"})
    value.session.request = Mock()
    assert not value.is_configured()
    assert value.search_ebooks("book") == []
    assert value.download_ebook("BOOK1") is None
    value.session.request.assert_not_called()


def test_exact_id_auth_and_base_path(client):
    client.session.request.return_value = response(book())
    result = client.get_book("BOOK1")
    assert result["id"] == "BOOK1"
    assert result["title"] == "Title"
    assert result["author"] == "Writer"
    method, url = client.session.request.call_args.args
    assert (method, url) == ("GET", "https://komga.test/reader/api/v1/books/BOOK1")
    assert client.session.request.call_args.kwargs["headers"]["X-API-Key"] == "bridge-key"
    assert client.session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("identifier", ["", "../BOOK1", "a/b", "a?token=secret", "a%2fb", None])
def test_invalid_identifiers_never_call_out(client, identifier):
    assert client.get_book(identifier) is None
    client.session.request.assert_not_called()


@pytest.mark.parametrize("row", [book("OTHER"), book(media={"mediaType": "application/pdf", "status": "READY"}), book(deleted=True)])
def test_wrong_identity_or_media_rejected(client, row):
    client.session.request.return_value = response(row)
    assert client.get_book("BOOK1") is None


def test_catalog_post_uses_booksearch_schema_and_paginates(client):
    client.session.request.side_effect = [response({"content": [book()], "number": 0, "last": False}),
                                           response({"content": [book("BOOK2")], "number": 1, "last": True})]
    assert [b["id"] for b in client.search_ebooks("needle")] == ["BOOK1", "BOOK2"]
    for index, call in enumerate(client.session.request.call_args_list):
        assert call.args == ("POST", "https://komga.test/reader/api/v1/books/list")
        assert call.kwargs["json"] == {"fullTextSearch": "needle"}
        assert call.kwargs["params"] == {"page": index, "size": 200}


def test_catalog_incomplete_or_duplicate_pages_fail_closed(client):
    client.session.request.side_effect = [response({"content": [book()], "number": 0, "last": False}), response({"content": [book()], "number": 1, "last": True})]
    assert client.search_ebooks("") == []


def test_source_download_exact_route_atomic_filename(client, tmp_path):
    content = epub()
    client.session.request.side_effect = [response(book()), response(content=content, headers={"Content-Length": str(len(content))})]
    filename = client.download_ebook("BOOK1")
    assert filename == "komga_BOOK1.epub"
    assert (tmp_path / "ebooks" / filename).read_bytes() == content
    assert client.session.request.call_args.args == ("GET", "https://komga.test/reader/api/v1/books/BOOK1/file")
    assert not list((tmp_path / "ebooks").glob("*.part"))


@pytest.mark.parametrize("payload,headers,status", [(b"not epub", {}, 200), (epub({"../escape": "bad"}), {}, 200), (epub({"/absolute": "bad"}), {}, 200), (epub(), {"Content-Length": "1"}, 200), (epub(), {"Location": "https://evil.test"}, 302), (epub(), {"Content-Length": "99999999999"}, 200)])
def test_bad_download_never_publishes(client, tmp_path, payload, headers, status):
    client.session.request.side_effect = [response(book()), response(content=payload, headers=headers, status=status)]
    assert client.download_ebook("BOOK1") is None
    assert not list(tmp_path.rglob("*.epub"))


def test_exception_never_exposes_secret_url(client, caplog):
    client.session.request.side_effect = requests.ConnectionError("https://komga.test/kobo/bridge-key/private")
    assert client.get_book("BOOK1") is None
    assert "bridge-key" not in caplog.text


@pytest.mark.parametrize("server", ["https://user:password@komga.test/reader", "https://komga.test/reader?key=x", "ftp://komga.test", "https://komga.test/../private", "https://komga.test/reader#x"])
def test_invalid_base_disables_transport(server):
    assert not KomgaClient(config={**CONFIG, "KOMGA_SERVER": server}).is_configured()


def test_rest_never_puts_the_key_in_the_url(client):
    client.session.request.return_value = response(book())
    client.get_book("BOOK1")
    assert "bridge-key" not in client.session.request.call_args.args[1]
    assert client.session.request.call_args.kwargs["headers"]["Accept-Encoding"] == "identity"


def test_check_connection(client):
    client.session.request.return_value = response({"content": [], "number": 0, "last": True})
    assert client.check_connection() is True
    method, url = client.session.request.call_args.args
    assert (method, url) == ("POST", "https://komga.test/reader/api/v1/books/list")
    client.session.request.return_value = response(status=500, content=b"")
    assert client.check_connection() is False


@pytest.mark.parametrize("payload", [{"content": "BOOK"}, {"content": [], "number": 0}, {"totalElements": 0}, []])
def test_catalog_shape_mismatch_fails_closed(client, payload):
    client.session.request.return_value = response(payload)
    assert client.search_ebooks("") == []


def test_library_filter_excludes_other_libraries(tmp_path):
    value = KomgaClient(data_dir=tmp_path, config={**CONFIG, "KOMGA_LIBRARY_ID": "LIB9"})
    value.session.request = Mock(return_value=response({"content": [book()], "number": 0, "last": True}))
    assert value.search_ebooks("") == []
    value.session.request.return_value = response({"content": [book(libraryId="LIB9")], "number": 0, "last": True})
    assert [b["id"] for b in value.search_ebooks("")] == ["BOOK1"]


def test_symlinked_books_dir_is_refused(client, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "ebooks").symlink_to(real, target_is_directory=True)
    client.session.request.side_effect = [response(book()), response(content=epub())]
    assert client.download_ebook("BOOK1") is None
    assert not list(real.iterdir())


def test_decompression_ratio_bomb_is_rejected(client):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("payload.bin", b"\0" * 4_000_000, compress_type=zipfile.ZIP_DEFLATED)
    bomb = output.getvalue()
    client.session.request.side_effect = [response(book()), response(content=bomb)]
    assert client.download_ebook("BOOK1") is None


@pytest.fixture(name="komga_live_server")
def _komga_live_server(tmp_path):
    """Real loopback HTTP server speaking the pinned Komga wire shapes."""
    store = {"book": book(), "content": epub(), "seen": [], "dir": tmp_path,
             "redirect": False, "status": {}, "state": [], "metadata": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload, headers=None):
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path = self.path
            route = self.path.split("?", 1)[0]
            store["seen"].append((self.command, path, self.headers.get("X-API-Key")))
            if store["redirect"]:
                return self._send(302, b"", {"Location": "https://evil.test/kobo/bridge-key/v1/library/BOOK1/state"})
            if route.endswith("/v1/initialization"):
                return self._send(200, b'{"Resources": {}}')
            if route.endswith("/library/BOOK1/state") and self.command == "PUT":
                store["state"] = json.loads(raw)["ReadingStates"]
                confirmation = {"RequestResult": "Success", "UpdateResults": [{
                    "EntitlementId": "BOOK1", "CurrentBookmarkResult": {"Result": "Success"},
                    "StatisticsResult": {"Result": "Ignored"}, "StatusInfoResult": {"Result": "Success"}}]}
                return self._send(200, json.dumps(confirmation).encode())
            if route.endswith("/library/BOOK1/state"):
                return self._send(200, json.dumps(store["state"]).encode())
            if route.endswith("/library/BOOK1/metadata"):
                return self._send(200, json.dumps(store["metadata"]).encode())
            if "/v1/books/BOOK1/file/epub" in route:
                return self._send(200, store["content"])
            if route.endswith("/api/v1/books/BOOK1"):
                return self._send(200, json.dumps(store["book"]).encode())
            if route.endswith("/api/v1/books/BOOK1/file"):
                return self._send(200, store["content"])
            if route.endswith("/api/v1/books/list"):
                search = json.loads(raw).get("fullTextSearch", "") if raw else ""
                rows = [store["book"]] if search in ("", "Book") else []
                return self._send(200, json.dumps({"content": rows, "number": 0, "last": True}).encode())
            return self._send(store["status"].get("code", 404), store["status"].get("body", b""))

        do_GET = do_POST = do_PUT = _handle

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_catalog_and_source_download_end_to_end(komga_live_server):
    value = KomgaClient(data_dir=komga_live_server["dir"], config={**CONFIG, "KOMGA_SERVER": komga_live_server["url"]})
    assert value.is_configured()
    assert value.check_connection() is True
    assert value.get_book("BOOK1")["title"] == "Title"
    assert [b["id"] for b in value.search_ebooks("Book")] == ["BOOK1"]
    assert value.search_ebooks("nothing") == []
    assert value.download_ebook("BOOK1") == "komga_BOOK1.epub"
    assert (komga_live_server["dir"] / "ebooks" / "komga_BOOK1.epub").read_bytes() == komga_live_server["content"]
    assert all(key == "bridge-key" for _, _, key in komga_live_server["seen"])
    assert any(path.endswith("/api/v1/books/BOOK1/file") for _, path, _ in komga_live_server["seen"])


def test_live_redirect_is_not_followed(komga_live_server):
    komga_live_server["redirect"] = True
    value = KomgaClient(data_dir=komga_live_server["dir"], config={**CONFIG, "KOMGA_SERVER": komga_live_server["url"]})
    assert value.get_book("BOOK1") is None
    assert value.download_ebook("BOOK1") is None
    assert len(komga_live_server["seen"]) == 2
