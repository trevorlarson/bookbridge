"""Per-user client registry (multi-user Phase 2).

Builds and caches a per-user bundle of API + sync clients, each constructed
with that user's stored credentials (overlaid on the global config). Catalog
services — database, ebook parser, alignment, transcriber, ollama — are SHARED
(passed in) because the library/mappings/alignments are shared across users.

A bundle mirrors the client wiring in di_container, but scoped to one user.
"""

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.utils.user_config import (
    PER_USER_CREDENTIAL_KEYS,
    _ALLOW_GLOBAL_FALLBACK_KEY,
    global_fallback_allowed,
    resolve_setting,
)

from src.api.api_clients import ABSClient, KoSyncClient
from src.api.storyteller_api import StorytellerAPIClient
from src.api.cwa_client import CWAClient
from src.api.cwa_sync_api import CWASyncApi
from src.api.komga_client import KomgaClient
from src.api.komga_sync_api import KomgaSyncApi
from src.api.bookorbit_client import BookOrbitClient
from src.api.bookfusion_client import BookFusionClient
from src.api.bookfusion_upload_client import BookFusionUploadClient
from src.api.booklore_client import BookloreClient
from src.api.kavita_client import KavitaClient, KavitaKoSyncClient
from src.api.hardcover_client import HardcoverClient
from src.api.storygraph_client import StorygraphClient

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.kavita_sync_client import KavitaSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.bookfusion_sync_client import BookFusionSyncClient
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
from src.sync_clients.cwa_sync_client import CWASyncClient
from src.sync_clients.komga_sync_client import KomgaSyncClient
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.storygraph_sync_client import StorygraphSyncClient
from src.services.library_service import LibraryService

logger = logging.getLogger(__name__)

# Komga settings a reader-scoped client resolves. Server URL is shared; the API
# key and the enable/sync/reverse gates are per-reader (see user_config).
_KOMGA_DEFAULTS = {
    "KOMGA_ENABLED": "false",
    "KOMGA_SERVER": "",
    "KOMGA_API_KEY": "",
    "KOMGA_SYNC_ENABLED": "false",
    "KOMGA_REVERSE_SYNC_ENABLED": "false",
}


class KomgaReaderConfig(Mapping):
    """Live, gate-aware Komga settings for one reader bundle.

    KomgaTransport reads an explicit config dict verbatim and never inherits the
    process environment, so the registry supplies a view that resolves each key
    through ``resolve_setting`` on every read. An already-built client therefore
    observes a later global gate change or credential update instead of a value
    frozen at construction, and a regular reader never inherits the primary
    admin's API key.
    """

    def __init__(self, credentials: dict):
        self.credentials = credentials

    def __getitem__(self, key: str) -> str:
        if key not in _KOMGA_DEFAULTS:
            raise KeyError(key)
        return resolve_setting(self.credentials, key, _KOMGA_DEFAULTS[key])

    def __iter__(self):
        return iter(_KOMGA_DEFAULTS)

    def __len__(self) -> int:
        return len(_KOMGA_DEFAULTS)

    def get(self, key: str, default=None):
        if key not in _KOMGA_DEFAULTS:
            return default
        return resolve_setting(self.credentials, key, _KOMGA_DEFAULTS[key])


@dataclass
class UserClients:
    """A user's API clients + sync-client dict (catalog services are shared)."""
    user_id: int
    abs_client: object
    kosync_client: object
    storyteller_client: object
    cwa_client: object
    bookorbit_client: object
    bookfusion_client: object
    bookfusion_upload_client: object
    booklore_client: object
    hardcover_client: object
    storygraph_client: object
    kavita_client: object = None
    library_service: object = None
    sync_clients: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    komga_client: object = None
    komga_sync_api: object = None


class UserClientRegistry:
    def __init__(self, database_service, ebook_parser, alignment_service,
                 transcriber=None, ollama_client=None, epub_cache_dir=None):
        self.database_service = database_service
        self.ebook_parser = ebook_parser
        self.alignment_service = alignment_service
        self.transcriber = transcriber
        self.ollama_client = ollama_client
        self.epub_cache_dir = epub_cache_dir
        self._cache = {}
        self._lock = threading.RLock()

    def _user_credentials(self, user_id: int) -> dict:
        """Per-user override dict, limited to recognized per-user keys.

        Raises on database exceptions so the caller can distinguish a transient
        DB failure from a legitimately empty credential set (which is valid and
        should be cached).
        """
        stored = self.database_service.get_user_credentials(user_id) or {}
        creds = {k: v for k, v in stored.items() if k in PER_USER_CREDENTIAL_KEYS}
        user = self.database_service.get_user(user_id)
        creds[_ALLOW_GLOBAL_FALLBACK_KEY] = global_fallback_allowed(self.database_service, user)
        return creds

    def get_clients(self, user_id: int) -> UserClients:
        """Return the cached per-user bundle, building it on first access.

        On a transient DB exception the bundle is returned but NOT cached, so
        a subsequent request re-reads the DB. A legitimately empty credential
        set is cached normally.
        """
        with self._lock:
            bundle = self._cache.get(user_id)
            if bundle is None:
                try:
                    creds = self._user_credentials(user_id)
                except Exception as exc:
                    logger.warning(
                        "Transient DB error building client bundle for user %s: %s "
                        "(returning fallback, not caching)",
                        user_id,
                        exc,
                        exc_info=True,
                    )
                    return self._build(user_id, {_ALLOW_GLOBAL_FALLBACK_KEY: False})
                bundle = self._build(user_id, creds)
                self._cache[user_id] = bundle
            return bundle

    def invalidate(self, user_id: int = None) -> None:
        """Drop cached bundle(s) — call after a user's credentials change."""
        with self._lock:
            if user_id is None:
                self._cache.clear()
            else:
                self._cache.pop(user_id, None)

    def _build(self, user_id: int, creds: dict) -> UserClients:
        ep = self.ebook_parser
        align = self.alignment_service
        db = self.database_service

        abs_client = ABSClient(credentials=creds)
        kosync_client = KoSyncClient(credentials=creds)
        storyteller_client = StorytellerAPIClient(credentials=creds)
        cwa_client = CWAClient(credentials=creds)
        cwa_sync_api = CWASyncApi(cwa_client=cwa_client, credentials=creds)
        komga_config = KomgaReaderConfig(creds)
        komga_client = KomgaClient(ebook_parser=ep, config=komga_config,
                                   books_dir=self.epub_cache_dir)
        komga_sync_api = KomgaSyncApi(config=komga_config)
        bookorbit_client = BookOrbitClient(ollama_client=self.ollama_client, credentials=creds)
        bookfusion_client = BookFusionClient(credentials=creds, database_service=db, user_id=user_id)
        bookfusion_upload_client = BookFusionUploadClient(credentials=creds, database_service=db, user_id=user_id)
        booklore_client = BookloreClient(database_service=db, ollama_client=self.ollama_client, credentials=creds)
        kavita_client = KavitaClient(credentials=creds)
        kavita_kosync_client = KavitaKoSyncClient(credentials=creds)
        hardcover_client = HardcoverClient(credentials=creds)
        storygraph_client = StorygraphClient(credentials=creds)

        # Per-user library service for ebook acquisition/search (uses this
        # user's abs/cwa/booklore/bookorbit clients). Requires an epub cache dir.
        library_service = None
        if self.epub_cache_dir is not None:
            library_service = LibraryService(
                database_service=db,
                booklore_client=booklore_client,
                cwa_client=cwa_client,
                abs_client=abs_client,
                epub_cache_dir=self.epub_cache_dir,
                bookorbit_client=bookorbit_client,
                kavita_client=kavita_client,
                komga_client=komga_client,
            )

        sync_clients = {
            "ABS": ABSSyncClient(abs_client, self.transcriber, ep, align),
            "ABSEbook": ABSEbookSyncClient(abs_client, ep),
            "KoSync": KoSyncSyncClient(kosync_client, ep),
            "Kavita": KavitaSyncClient(kavita_kosync_client, ep),
            "Storyteller": StorytellerSyncClient(storyteller_client, ep, db),
            "BookLore": BookloreSyncClient(booklore_client, ep),
            "BookFusion": BookFusionSyncClient(bookfusion_client, ep, database_service=db, user_id=user_id),
            "BookLoreAudio": BookLoreAudioSyncClient(booklore_client, ep, alignment_service=align),
            "BookOrbit": BookOrbitSyncClient(bookorbit_client, ep, database_service=db, user_id=user_id),
            "BookOrbitAudio": BookOrbitAudioSyncClient(bookorbit_client, ep, alignment_service=align, database_service=db, user_id=user_id),
            "CWA": CWASyncClient(cwa_sync_api, cwa_client, ep),
            "Komga": KomgaSyncClient(komga_sync_api, komga_client, ep, user_id=user_id, database_service=db),
            "Hardcover": HardcoverSyncClient(hardcover_client, ep, abs_client, db, ollama_client=self.ollama_client, booklore_client=booklore_client, bookorbit_client=bookorbit_client, kavita_client=kavita_client),
            "StoryGraph": StorygraphSyncClient(storygraph_client, ep, abs_client, db, ollama_client=self.ollama_client, booklore_client=booklore_client, bookorbit_client=bookorbit_client, kavita_client=kavita_client),
        }

        return UserClients(
            user_id=user_id,
            abs_client=abs_client,
            kosync_client=kosync_client,
            storyteller_client=storyteller_client,
            cwa_client=cwa_client,
            bookorbit_client=bookorbit_client,
            bookfusion_client=bookfusion_client,
            bookfusion_upload_client=bookfusion_upload_client,
            booklore_client=booklore_client,
            kavita_client=kavita_client,
            hardcover_client=hardcover_client,
            storygraph_client=storygraph_client,
            library_service=library_service,
            sync_clients=sync_clients,
            credentials=creds,
            komga_client=komga_client,
            komga_sync_api=komga_sync_api,
        )
