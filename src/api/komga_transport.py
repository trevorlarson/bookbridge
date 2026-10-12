"""Bounded, same-origin transport and non-extracting EPUB validation for Komga."""
from __future__ import annotations

import io
import json
import os
import posixpath
import re
import stat
import zipfile
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
from defusedxml import ElementTree

BOOK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000
# Mirrors the project's own disabled-setting tokens (src/utils/user_config.py).
FALSEY_GATE_VALUES = frozenset({"false", "0", "off", "no"})


def valid_book_id(value: Any) -> bool:
    """Accept opaque path-safe IDs without repairing source identifiers."""
    return isinstance(value, str) and BOOK_ID.fullmatch(value) is not None


def safe_path(path: str) -> bool:
    """Reject traversal, encoded separators, controls and ambiguous URL paths."""
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    decoded = unquote(path)
    return (decoded == path and "//" not in path
            and not any(part in {".", ".."} for part in path.split("/")))


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON number")


def validate_epub(content: bytes) -> bool:
    """Validate bounded ZIP members and EPUB package structure without extraction."""
    try:
        if not content or len(content) > MAX_DOWNLOAD_BYTES:
            return False
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if not 0 < len(members) <= MAX_ARCHIVE_ENTRIES:
                return False
            names: set[str] = set()
            total = 0
            for member in members:
                name = member.filename
                mode = member.external_attr >> 16
                total += member.file_size
                if (not safe_path(name) or name.startswith("/") or ":" in name
                        or name in names or member.flag_bits & 1 or stat.S_ISLNK(mode)
                        or total > MAX_EXPANDED_BYTES
                        or member.file_size > max(1, member.compress_size) * 200):
                    return False
                names.add(name)
            if archive.read("mimetype") != b"application/epub+zip":
                return False
            # Reading every bounded member verifies CRC and catches corrupt streams.
            for member in members:
                if member.is_dir():
                    continue
                data = archive.read(member)
                if member.filename.lower().endswith((".xml", ".opf", ".xhtml", ".ncx")):
                    ElementTree.fromstring(data, forbid_entities=True, forbid_external=True)
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            roots = container.findall("{*}rootfiles/{*}rootfile")
            if len(roots) != 1:
                return False
            package_path = roots[0].get("full-path", "")
            if not safe_path(package_path) or package_path not in names:
                return False
            package = ElementTree.fromstring(archive.read(package_path))
            if package.tag != "{http://www.idpf.org/2007/opf}package":
                return False
            manifest: dict[str, str] = {}
            for item in package.findall("{*}manifest/{*}item"):
                identifier, href = item.get("id", ""), item.get("href", "")
                if not identifier or identifier in manifest or not href:
                    return False
                parsed = urlsplit(href)
                if parsed.scheme or parsed.netloc:
                    continue
                resource = posixpath.normpath(posixpath.join(str(PurePosixPath(package_path).parent), unquote(parsed.path)))
                if resource.startswith("../") or resource.startswith("/") or resource not in names:
                    return False
                manifest[identifier] = resource
            spine = package.findall("{*}spine/{*}itemref")
            return bool(spine) and all(item.get("idref") in manifest for item in spine)
    except (ValueError, OSError, KeyError, RuntimeError, zipfile.BadZipFile,
            ElementTree.ParseError, Exception):
        # DefusedXmlException subclasses are intentionally collapsed at this boundary.
        # No archive names, URLs, body contents or exception strings escape.
        return False


class KomgaTransport:
    """Explicit opt-in transport; an explicit config never inherits another key."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config = config
        self.session = requests.Session()
        self.session.trust_env = False
        self.timeout = (5, 30)

    def _cfg(self, key: str, default: Any = "") -> Any:
        return self._config.get(key, default) if self._config is not None else os.environ.get(key, default)

    def _gate_allows(self, key: str) -> bool:
        """Respect a gate the caller explicitly supplied, without inventing one.

        Komga settings carry separate enable flags for the source integration and
        for reverse writes. When the caller passes one it must be honoured; when
        it is absent (or blank, the shape an unset setting can take in a config
        mapping) this transport keeps `KOMGA_ENABLED` as the only gate. Only the
        project's explicit falsey tokens disable it.
        """
        value = self._cfg(key, None)
        return str(value or "").strip().lower() not in FALSEY_GATE_VALUES

    @property
    def base_url(self) -> str:
        """Configured origin and reverse-proxy prefix, without inferred rewriting."""
        value = self._cfg("KOMGA_SERVER", "")
        return value.rstrip("/") if isinstance(value, str) else ""

    @property
    def api_key(self) -> str:
        """The single bridge API key used for REST and Kobo authentication."""
        value = self._cfg("KOMGA_API_KEY", "")
        return value if isinstance(value, str) else ""

    def is_configured(self) -> bool:
        """Require an explicit enabled flag, valid URL and path-safe API key.

        Opt-in means `KOMGA_ENABLED` must be present and non-falsey; an unset or
        blank flag is off, never inheriting from another service's setting.
        """
        try:
            parsed = urlsplit(self.base_url)
            enabled = str(self._cfg("KOMGA_ENABLED", "") or "").strip().lower()
            return (bool(enabled) and enabled not in FALSEY_GATE_VALUES
                    and self._gate_allows("KOMGA_SYNC_ENABLED")
                    and parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                    and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment
                    and safe_path(parsed.path or "/") and parsed.port != 0
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", self.api_key) is not None)
        except ValueError:
            return False

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _allowed_url(self, url: str) -> bool:
        try:
            base, target = urlsplit(self.base_url), urlsplit(url)
            def origin(part):
                return (part.scheme, part.hostname, part.port or (443 if part.scheme == "https" else 80))
            prefix = base.path.rstrip("/") + "/"
            return (origin(base) == origin(target) and not target.username and not target.password
                    and not target.fragment and safe_path(target.path)
                    and target.path.startswith(prefix)
                    and not any(ord(char) < 32 or ord(char) == 127 for char in url))
        except (ValueError, TypeError):
            return False

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response | None:
        if not self.is_configured() or not self._allowed_url(url):
            return None
        try:
            headers = {"X-API-Key": self.api_key, "Accept": "application/json",
                       # Identity encoding keeps Content-Length equal to the body
                       # we verify; a transparent gzip stream would break the
                       # size assertion and be rejected as ambiguous.
                       "Accept-Encoding": "identity"}
            return self.session.request(method, url, headers=headers,
                                        timeout=kwargs.pop("timeout", self.timeout),
                                        allow_redirects=False, stream=True, **kwargs)
        except requests.RequestException:
            return None

    @staticmethod
    def _read(response: requests.Response | None, limit: int) -> bytes | None:
        if response is None:
            return None
        try:
            if response.status_code != 200:
                return None
            raw_length = response.headers.get("Content-Length")
            length = None if raw_length is None else int(raw_length)
            if length is not None and not 0 <= length <= limit:
                return None
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in {"", "identity"}:
                return None
            chunks: list[bytes] = []
            count = 0
            for chunk in response.iter_content(chunk_size=65536):
                count += len(chunk)
                if count > limit:
                    return None
                chunks.append(chunk)
            if length is not None and count != length:
                return None
            return b"".join(chunks)
        except (requests.RequestException, ValueError, TypeError):
            return None
        finally:
            response.close()

    def _json(self, response: requests.Response | None) -> Any:
        content = self._read(response, MAX_JSON_BYTES)
        if content is None:
            return None
        try:
            return json.loads(content, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, TypeError):
            return None

    def _epub(self, url: str) -> bytes | None:
        content = self._read(self._request("GET", url, timeout=(5, 180)), MAX_DOWNLOAD_BYTES)
        return content if content is not None and validate_epub(content) else None
