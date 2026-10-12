"""Reader-facing Komga wiring: catalog selection, exact-ID pairing, connection test.

Nothing here talks to a live Komga: the client is always a stub, so these tests
pin the *integration* (which sources appear, which ID travels with a selection,
what the pairing hash is computed from) rather than the wire protocol.
"""

import io
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.utils.ebook_utils import is_linkable_ebook_filename

REPO = Path(__file__).resolve().parent.parent


def _clients(**overrides):
    base = dict(
        booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
        kavita_client=MagicMock(is_configured=MagicMock(return_value=False)),
        komga_client=MagicMock(is_configured=MagicMock(return_value=False)),
        abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _komga_row(book_id="BOOK1"):
    return {"id": book_id, "title": "A Book", "authors": "A Writer",
            "filename": f"komga_{book_id}.epub", "fileName": f"komga_{book_id}.epub",
            "source": "Komga", "ext": "epub", "series_title": "Series"}


class KomgaCatalogSelectionTest(unittest.TestCase):
    def test_search_surfaces_a_komga_result_with_its_exact_id(self):
        import src.web_server as ws

        client = MagicMock()
        client.is_configured.return_value = True
        client.search_ebooks.return_value = [_komga_row()]
        clients = _clients(komga_client=client)
        with patch.object(ws, "uc", return_value=clients), patch.object(
                ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True):
            results = ws.get_searchable_ebooks("A Book")

        self.assertEqual([r.source for r in results], ["Komga"])
        self.assertEqual(results[0].source_id, "BOOK1")
        self.assertEqual(results[0].name, "komga_BOOK1.epub")
        client.search_ebooks.assert_called_once_with("A Book")

    def test_a_failed_catalog_scan_never_blocks_the_other_sources(self):
        import src.web_server as ws

        client = MagicMock()
        client.is_configured.return_value = True
        client.search_ebooks.side_effect = RuntimeError("catalog unavailable")
        booklore = MagicMock()
        booklore.is_configured.return_value = True
        booklore.search_books.return_value = [
            {"id": "7", "fileName": "Other.epub", "title": "Other"}]
        with patch.object(ws, "uc", return_value=_clients(komga_client=client, booklore_client=booklore)), patch.object(
                ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True):
            results = ws.get_searchable_ebooks("Other")

        self.assertEqual([r.source for r in results], ["Grimmory"])

    def test_generated_filename_is_linkable(self):
        self.assertTrue(is_linkable_ebook_filename("komga_BOOK1.epub"))


class KomgaPairingHashTest(unittest.TestCase):
    def _run(self, client):
        import src.web_server as ws

        parser = MagicMock()
        parser.get_kosync_id_from_bytes.return_value = "hash-from-bytes"
        clients = _clients(komga_client=client)
        with patch.object(ws, "uc", return_value=clients), patch.object(
                ws, "container", MagicMock(ebook_parser=MagicMock(return_value=parser))), patch.object(
                ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True):
            value = ws.get_kosync_id_for_ebook("komga_BOOK1.epub", komga_id="BOOK1")
        return value, parser, clients

    def test_hash_comes_from_the_exact_id_download(self):
        client = MagicMock()
        client.is_configured.return_value = True
        client.download_book.return_value = b"epub-bytes"
        value, parser, clients = self._run(client)

        self.assertEqual(value, "hash-from-bytes")
        client.download_book.assert_called_once_with("BOOK1")
        parser.get_kosync_id_from_bytes.assert_called_once_with("komga_BOOK1.epub", b"epub-bytes")
        # An exact-ID pairing must not search the catalog or fall back elsewhere.
        client.search_ebooks.assert_not_called()
        clients.booklore_client.find_book_by_filename.assert_not_called()

    def test_a_refused_download_yields_no_hash(self):
        client = MagicMock()
        client.is_configured.return_value = True
        client.download_book.return_value = None
        value, parser, _clients_obj = self._run(client)
        self.assertIsNone(value)
        parser.get_kosync_id_from_bytes.assert_not_called()

    def test_disabled_reader_is_never_contacted(self):
        client = MagicMock()
        client.is_configured.return_value = False
        value, _parser, _clients_obj = self._run(client)
        self.assertIsNone(value)
        client.download_book.assert_not_called()


class KomgaConnectionTestTest(unittest.TestCase):
    def _call(self, enabled=True, url="https://komga.example", key="secret-key", connected=True):
        import src.web_server as ws

        with patch("src.api.komga_client.KomgaClient") as factory:
            factory.return_value.check_connection.return_value = connected
            result = ws._test_komga(enabled, url, key)
        return result, factory

    def test_success_message_carries_no_credentials(self):
        result, factory = self._call()
        self.assertTrue(result["ok"])
        self.assertNotIn("secret-key", result["message"])
        factory.assert_called_once_with(config={"KOMGA_ENABLED": "true", "KOMGA_SERVER": "https://komga.example",
                                               "KOMGA_API_KEY": "secret-key"})

    def test_rejection_is_reported_without_the_key(self):
        result, _factory = self._call(connected=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-key", result["message"])

    def test_disabled_or_incomplete_never_contacts_the_server(self):
        for kwargs in ({"enabled": False}, {"url": ""}, {"key": ""}):
            result, factory = self._call(**kwargs)
            self.assertFalse(result["ok"])
            factory.assert_not_called()


class KomgaSettingsSurfaceTest(unittest.TestCase):
    """Checkboxes only persist a 'false' when the key is in the save loop's list."""

    KOMGA_BOOL_KEYS = ("KOMGA_ENABLED", "KOMGA_SYNC_ENABLED",
                       "KOMGA_REVERSE_SYNC_ENABLED", "KOMGA_SYNC_POLL_WAIT_FOR_SETTLE")

    def test_komga_toggles_are_boolean_save_keys(self):
        source = io.open(REPO / "src" / "web_server.py", encoding="utf-8").read()
        start = source.index("        bool_keys = [")
        keys = set(re.findall(r"'([A-Z0-9_]+)'", source[start:source.index("]", start)]))
        for key in self.KOMGA_BOOL_KEYS:
            self.assertIn(key, keys)

    def test_every_komga_field_is_registered_and_rendered(self):
        from src.utils.config_loader import ALL_SETTINGS

        html = io.open(REPO / "templates" / "settings.html", encoding="utf-8").read()
        names = set(re.findall(r'<(?:input|select)[^>]*\bname="(KOMGA_[A-Z0-9_]+)"', html))
        self.assertTrue(names)
        self.assertTrue(names.issubset(set(ALL_SETTINGS)), sorted(names - set(ALL_SETTINGS)))

    def test_the_reverse_write_gate_is_explained_in_the_ui(self):
        html = io.open(REPO / "templates" / "settings.html", encoding="utf-8").read()
        section = html[html.index('data-svc="komga"'):]
        section = section[:section.index("<!-- Hardcover")]
        self.assertIn("KOMGA_REVERSE_SYNC_ENABLED", section)
        self.assertIn("off by default", section.lower())
        self.assertIn("docs/komga-development.md", section)

    def test_per_user_integration_pages_offer_the_komga_gate(self):
        for name in ("account_integrations.html", "admin_user_integrations.html"):
            html = io.open(REPO / "templates" / name, encoding="utf-8").read()
            self.assertIn("'Komga': 'KOMGA_ENABLED'", html, name)


class KomgaForgeItemTest(unittest.TestCase):
    def test_text_item_keeps_the_komga_id(self):
        import src.web_server as ws

        item = ws._build_forge_text_item("Komga", "BOOK1", "", "komga_BOOK1.epub")
        self.assertEqual(item["source"], "Komga")
        self.assertEqual(item["komga_id"], "BOOK1")
        # No provider path is invented for a Komga item.
        self.assertEqual(item["path"], "")

    def test_other_sources_are_unchanged(self):
        import src.web_server as ws

        item = ws._build_forge_text_item("Kavita", "K9", "", "x.epub")
        self.assertEqual(item["kavita_id"], "K9")
        self.assertNotIn("download_url", item)


class KomgaForgeServiceTest(unittest.TestCase):
    """The forge path materialises a Komga ebook from the paired ID."""

    def _service(self, **overrides):
        from src.services.forge_service import ForgeService

        fields = dict(database_service=MagicMock(), abs_client=MagicMock(), booklore_client=MagicMock(),
                      storyteller_client=MagicMock(), library_service=MagicMock(), ebook_parser=MagicMock(),
                      transcriber=MagicMock(), alignment_service=MagicMock())
        fields.update(overrides)
        return ForgeService(**fields)

    def test_source_name_is_canonical(self):
        service = self._service()
        self.assertEqual(service._normalize_text_source("komga"), "Komga")
        self.assertEqual(service._normalize_text_source("Komga"), "Komga")

    def test_reader_bundle_supplies_its_own_komga_client(self):
        global_client, reader_client = MagicMock(name="global"), MagicMock(name="reader")
        service = self._service(komga_client=global_client)
        bundle = SimpleNamespace(komga_client=reader_client)
        worker = service._for_client_bundle(bundle)
        self.assertIs(worker.komga_client, reader_client)
        # Objects deliberately not on the bundle keep the parent's value.
        self.assertIs(worker.kavita_client, service.kavita_client)
