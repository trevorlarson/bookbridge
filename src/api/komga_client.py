"""Komga REST catalog and original EPUB acquisition.

Pinned contracts: gotson/komga 133d30d7bc7715fb4144be56486337db1ad4c525,
BookController.getBooks/getBook, BookSearch.fullTextSearch and
CommonBookController.downloadBookFile. No OPDS scraping or CWA inheritance.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from src.api.komga_transport import KomgaTransport, valid_book_id


class KomgaClient(KomgaTransport):
    """Opt-in catalog client using exact remote IDs and validated source EPUBs."""

    def __init__(self, ebook_parser: Any = None, data_dir: str | Path | None = None,
                 config: Mapping[str, Any] | None = None,
                 books_dir: str | Path | None = None) -> None:
        super().__init__(config)
        self.ebook_parser = ebook_parser
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", "data"))
        # Defaults to a Komga-owned directory; the application wires the shared
        # epub cache so a downloaded source EPUB is resolvable by filename the
        # same way every other provider's download is.
        self.books_dir = Path(books_dir) if books_dir else self.data_dir / "ebooks"

    def check_connection(self) -> bool:
        """Check authentication and catalog access without invoking Kobo sync."""
        data = self._json(self._request("POST", self._url("/api/v1/books/list"),
                                       json={}, params={"page": 0, "size": 1}))
        return isinstance(data, dict) and isinstance(data.get("content"), list)

    def _normalise(self, data: Any, book_id: str | None = None) -> dict[str, Any] | None:
        if not isinstance(data, dict) or not valid_book_id(data.get("id")):
            return None
        if book_id is not None and data["id"] != book_id:
            return None
        media, metadata = data.get("media"), data.get("metadata")
        if (not isinstance(media, dict) or not isinstance(metadata, dict)
                or media.get("mediaType") != "application/epub+zip"
                or media.get("status") != "READY" or data.get("deleted") is not False):
            return None
        library = str(self._cfg("KOMGA_LIBRARY_ID", "") or "")
        if library and data.get("libraryId") != library:
            return None
        authors = metadata.get("authors") or []
        if not isinstance(authors, list):
            return None
        names = [author["name"] for author in authors if isinstance(author, dict)
                 and isinstance(author.get("name"), str)]
        filename = f"komga_{data['id']}.epub"
        title = metadata.get("title") or data.get("name") or data["id"]
        return {"id": data["id"], "title": title, "author": ", ".join(names),
                "authors": ", ".join(names), "source": "Komga", "ext": "epub",
                "filename": filename, "fileName": filename,
                "series_id": data.get("seriesId"), "series_title": data.get("seriesTitle"),
                "seriesName": data.get("seriesTitle"), "seriesIndex": metadata.get("numberSort"),
                "library_id": data.get("libraryId"), "file_hash": data.get("fileHash"),
                "file_modified_at": data.get("fileLastModified"), "size_bytes": data.get("sizeBytes")}

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        """Get only this ID, rejecting deleted, unsupported and mismatched books."""
        if not valid_book_id(book_id):
            return None
        data = self._json(self._request("GET", self._url(f"/api/v1/books/{book_id}")))
        return self._normalise(data, book_id)

    def search_ebooks(self, query: str = "") -> list[dict[str, Any]]:
        """Search paginated BookSearch.fullTextSearch; fail closed on partial scans."""
        if not isinstance(query, str) or not self.is_configured():
            return []
        # BookSearch.condition is optional; fullTextSearch is the exact Kotlin field.
        body = {"fullTextSearch": query} if query else {}
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(100):
            data = self._json(self._request("POST", self._url("/api/v1/books/list"),
                                           json=body, params={"page": page, "size": 200}))
            if (not isinstance(data, dict) or not isinstance(data.get("content"), list)
                    or data.get("number") != page or not isinstance(data.get("last"), bool)):
                return []
            rows = data["content"]
            if len(rows) > 200 or not rows and not data["last"]:
                return []
            for row in rows:
                if not isinstance(row, dict) or not valid_book_id(row.get("id")) or row["id"] in seen:
                    return []
                seen.add(row["id"])
                book = self._normalise(row)
                if book is not None:
                    results.append(book)
            if data["last"]:
                return results
        return []

    def download_ebook(self, book_id: str) -> str | None:
        """Atomically publish validated original bytes; return the local filename."""
        book = self.get_book(book_id) if self.is_configured() else None
        if book is None:
            return None
        content = self._fetch_original(book_id)
        if content is None:
            return None
        return self._publish(book, content)

    def download_book(self, book_id: str) -> bytes | None:
        """Validated original-file bytes, matching the other library clients' API.

        Used by the sync manager's by-id recovery path, which writes the bytes
        into the bridge's own cache rather than a provider-owned file.
        """
        if self.get_book(book_id) is None:
            return None
        return self._fetch_original(book_id)

    def _fetch_original(self, book_id: str) -> bytes | None:
        """The book's own file bytes; the caller has already validated the ID."""
        return self._epub(self._url(f"/api/v1/books/{book_id}/file"))

    def _publish(self, book: dict[str, Any], content: bytes) -> str | None:
        """Atomically publish validated bytes under the book's canonical name."""
        temporary: str | None = None
        try:
            if self.books_dir.is_symlink():
                return None
            self.books_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.books_dir, suffix=".part", delete=False) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.books_dir / book["filename"])
            temporary = None
            return book["filename"]
        except OSError:
            return None
        finally:
            if temporary is not None:
                # Best-effort cleanup: the validated bytes are only ever published
                # by the atomic rename above, so a leftover .part is inert.
                with suppress(OSError):
                    Path(temporary).unlink(missing_ok=True)
