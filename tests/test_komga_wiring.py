"""Isolated Komga settings, reader isolation, acquisition and polling contracts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.db.database_service import DatabaseService
from src.services.client_poller import ClientPoller
from src.services.library_service import LibraryService
from src.utils import secret_store
from src.utils.cache_paths import is_plain_basename
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG, ConfigLoader
from src.utils.ebook_sources import normalize_ebook_source, source_name_variants
from src.utils.user_config import (
    _ALLOW_GLOBAL_FALLBACK_KEY,
    ENGINE_MIRROR_KEYS,
    PER_USER_CREDENTIAL_KEYS,
    PER_USER_FIELD_GROUPS,
    SERVICE_ENABLE_KEYS,
    resolve_setting,
)

KOMGA_GATES = ("KOMGA_ENABLED", "KOMGA_SYNC_ENABLED", "KOMGA_REVERSE_SYNC_ENABLED")


@pytest.fixture(autouse=True)
def isolated_komga(monkeypatch, tmp_path):
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", Mock(side_effect=AssertionError("Live HTTP is forbidden")))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOOKS_DIR", str(tmp_path / "books"))
    for key in (
        "KOMGA_ENABLED", "KOMGA_SYNC_ENABLED", "KOMGA_REVERSE_SYNC_ENABLED",
        "KOMGA_API_KEY", "KOMGA_SERVER", "KOMGA_SYNC_POLL_MODE",
        "KOMGA_SYNC_POLL_SECONDS", "KOMGA_SYNC_POLL_WAIT_FOR_SETTLE",
    ):
        monkeypatch.delenv(key, raising=False)
    secret_store.reset_cache()
    yield
    secret_store.reset_cache()


def test_settings_default_to_disabled():
    expected = {
        "KOMGA_ENABLED": "false", "KOMGA_SERVER": "", "KOMGA_API_KEY": "",
        "KOMGA_SYNC_ENABLED": "false", "KOMGA_REVERSE_SYNC_ENABLED": "false",
        "KOMGA_SYNC_POLL_MODE": "global", "KOMGA_SYNC_POLL_SECONDS": "300",
        "KOMGA_SYNC_POLL_WAIT_FOR_SETTLE": "false",
    }
    assert {k: DEFAULT_CONFIG.get(k) for k in expected} == expected
    assert set(expected).issubset(ALL_SETTINGS)


def test_bootstrap_adds_komga_without_overwriting_existing(monkeypatch):
    db = Mock()
    db.get_all_settings.return_value = {"KOMGA_ENABLED": "false"}
    monkeypatch.setenv("KOMGA_SERVER", "https://komga.example")
    ConfigLoader.bootstrap_config(db)
    written = dict(call.args for call in db.set_setting.call_args_list)
    assert written["KOMGA_SERVER"] == "https://komga.example"
    assert written["KOMGA_SYNC_ENABLED"] == "false"
    assert "KOMGA_ENABLED" not in written


def test_bootstrap_continues_when_a_secret_refuses_storage():
    """A refusing Komga secret must not abort seeding of every other setting."""
    db = Mock()
    db.get_all_settings.return_value = {}
    written = []

    def set_setting(key, value):
        if key == "KOMGA_API_KEY":
            raise RuntimeError("credential encryption unavailable")
        written.append(key)

    db.set_setting.side_effect = set_setting
    ConfigLoader.bootstrap_config(db)
    assert "KOMGA_API_KEY" not in written
    assert "ABS_ENABLED" in written
    assert "KOMGA_ENABLED" in written


def test_reader_keys_groups_and_secret_registration():
    keys = {"KOMGA_ENABLED", "KOMGA_API_KEY", "KOMGA_SYNC_ENABLED", "KOMGA_REVERSE_SYNC_ENABLED"}
    assert keys.issubset(PER_USER_CREDENTIAL_KEYS)
    assert "KOMGA_SERVER" not in PER_USER_CREDENTIAL_KEYS
    assert (keys - {"KOMGA_API_KEY"}).issubset(SERVICE_ENABLE_KEYS)
    fields = dict((k, kind) for _, group in PER_USER_FIELD_GROUPS for k, _, kind in group)
    assert fields["KOMGA_API_KEY"] == "secret"
    assert fields["KOMGA_REVERSE_SYNC_ENABLED"] == "bool"
    assert "KOMGA_API_KEY" in ENGINE_MIRROR_KEYS
    assert not (keys - {"KOMGA_API_KEY"}).intersection(ENGINE_MIRROR_KEYS)
    assert "KOMGA_API_KEY" in secret_store.secret_keys()


@pytest.mark.parametrize("key", KOMGA_GATES)
def test_gates_follow_admin_killswitch_and_reader_toggle(monkeypatch, key):
    """The admin's global gate can only take capability away; the reader's own
    toggle is honored while the global gate is not switched off."""
    creds = {key: "true", _ALLOW_GLOBAL_FALLBACK_KEY: False}
    # No seeded global value is not a decision (matches every existing service).
    assert resolve_setting(creds, key, "false") == "true"
    monkeypatch.setenv(key, "true")
    assert resolve_setting(creds, key, "false") == "true"
    monkeypatch.setenv(key, "false")
    assert resolve_setting(creds, key, "false") == "false"
    creds[key] = "false"
    monkeypatch.setenv(key, "true")
    assert resolve_setting(creds, key, "false") == "false"


def test_komga_gates_are_opt_in_by_default():
    """A reader with no stored gate stays disabled because the seeded default is false."""
    for key in KOMGA_GATES:
        assert resolve_setting({_ALLOW_GLOBAL_FALLBACK_KEY: False}, key, "false") == "false"


def test_regular_reader_cannot_inherit_secret(monkeypatch):
    monkeypatch.setenv("KOMGA_API_KEY", "admin-key")
    assert resolve_setting({_ALLOW_GLOBAL_FALLBACK_KEY: False}, "KOMGA_API_KEY", "") == ""
    assert resolve_setting({_ALLOW_GLOBAL_FALLBACK_KEY: True}, "KOMGA_API_KEY", "") == "admin-key"


@pytest.mark.parametrize("user_secret", [False, True])
def test_unavailable_encryption_rejects_komga_storage(monkeypatch, tmp_path, user_secret):
    db = DatabaseService(str(tmp_path / "test.db"))
    user = db.create_user("reader", "test-password")
    monkeypatch.setattr(secret_store, "_get_fernet", lambda: None)
    try:
        with pytest.raises(RuntimeError, match="Komga|KOMGA|encryption"):
            if user_secret:
                db.set_user_credential(user.id, "KOMGA_API_KEY", "must-not-be-plaintext")
            else:
                db.set_setting("KOMGA_API_KEY", "must-not-be-plaintext")
        assert db.get_setting("KOMGA_API_KEY", "") == ""
        assert "KOMGA_API_KEY" not in db.get_user_credentials(user.id)
        assert secret_store.encrypt("legacy-provider") == "legacy-provider"
    finally:
        db.db_manager.close()


def test_komga_secret_is_encrypted_and_round_trips(tmp_path):
    from src.db.models import Setting, UserCredential
    db = DatabaseService(str(tmp_path / "test.db"))
    user = db.create_user("reader", "test-password")
    try:
        db.set_setting("KOMGA_API_KEY", "global-key")
        db.set_user_credential(user.id, "KOMGA_API_KEY", "reader-key")
        with db.get_session() as session:
            assert secret_store.is_encrypted(session.query(Setting).filter_by(key="KOMGA_API_KEY").one().value)
            assert secret_store.is_encrypted(session.query(UserCredential).filter_by(key="KOMGA_API_KEY").one().value)
        assert db.get_setting("KOMGA_API_KEY") == "global-key"
        assert db.get_user_credentials(user.id)["KOMGA_API_KEY"] == "reader-key"
    finally:
        db.db_manager.close()


def make_library(tmp_path, komga=None):
    others = [Mock(), Mock(), Mock(), Mock(), Mock()]
    service = LibraryService(Mock(), others[0], others[1], others[2], str(tmp_path / "cache"),
                             bookorbit_client=others[3], kavita_client=others[4], komga_client=komga)
    return service, others


def komga_book(**overrides):
    fields = dict(abs_id="audio", abs_title="Title", ebook_source="Komga", ebook_source_id="remote-id",
                  ebook_filename="mapped.epub", original_ebook_filename=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def fake_komga_client(tmp_path, *, configured=True, filename="komga_remote-id.epub",
                      content=b"x" * 2048, error=None):
    """Model the real KomgaClient: download_ebook(id) -> basename it wrote itself."""
    client = Mock()
    client.is_configured.return_value = configured
    directory = tmp_path / "komga-ebooks"
    directory.mkdir(parents=True, exist_ok=True)
    client.books_dir = directory
    if error is not None:
        client.download_ebook.side_effect = error
        return client
    client.download_ebook.return_value = filename
    if filename and content is not None and is_plain_basename(filename):
        (directory / filename).write_bytes(content)
    return client


def test_explicit_komga_downloads_exact_id(tmp_path):
    client = fake_komga_client(tmp_path)
    service, others = make_library(tmp_path, client)
    result = service.acquire_ebook({}, komga_book(ebook_source="komga"))
    assert Path(result).read_bytes() == b"x" * 2048
    client.download_ebook.assert_called_once_with("remote-id")
    for other in others:
        assert not other.method_calls


@pytest.mark.parametrize("failure", ["absent", "disabled", "denied", "empty", "small", "unsafe", "missing_id", "storyteller"])
def test_explicit_komga_denial_never_falls_back(tmp_path, failure):
    book = komga_book()
    kwargs = {}
    present = True
    if failure == "absent":
        present = False
    elif failure == "disabled":
        kwargs["configured"] = False
    elif failure == "denied":
        kwargs["error"] = PermissionError("denied")
    elif failure == "empty":
        kwargs["filename"] = None
    elif failure == "small":
        kwargs["content"] = b""
    elif failure == "unsafe":
        kwargs["filename"] = "../outside.epub"
    elif failure == "missing_id":
        book.ebook_source_id = None
    elif failure == "storyteller":
        book.ebook_filename = "storyteller_generated.epub"
    client = fake_komga_client(tmp_path, **kwargs) if present else None
    service, others = make_library(tmp_path, client)
    item = {"id": "audio", "media": {"metadata": {"title": "Title"}}}
    assert service.acquire_ebook(item, book) is None
    for other in others:
        assert not other.method_calls
    assert not list(tmp_path.rglob("outside.epub"))


def test_explicit_komga_does_not_trust_preexisting_files(tmp_path):
    client = fake_komga_client(tmp_path, filename=None)
    (client.books_dir / "mapped.epub").write_bytes(b"other-reader-bytes" * 200)
    service, others = make_library(tmp_path, client)
    assert service.acquire_ebook({}, komga_book()) is None
    client.download_ebook.assert_called_once_with("remote-id")
    for other in others:
        assert not other.method_calls


def test_explicit_komga_acquires_through_the_real_client(tmp_path):
    """The library layer is exercised against the actual KomgaClient (mocked transport)."""
    from src.api.komga_client import KomgaClient
    from tests.test_komga_client import CONFIG, epub, response
    from tests.test_komga_client import book as komga_row

    client = KomgaClient(data_dir=tmp_path / "komga", config=CONFIG)
    client.session.request = Mock()
    content = epub()
    client.session.request.side_effect = [
        response(komga_row(book_id="BOOK1")),
        response(content=content, headers={"Content-Length": str(len(content))}),
    ]
    service, others = make_library(tmp_path, client)
    result = service.acquire_ebook({}, komga_book(ebook_source_id="BOOK1"))
    assert result == str(client.books_dir / "komga_BOOK1.epub")
    assert Path(result).read_bytes() == content
    for other in others:
        assert not other.method_calls


def test_explicit_komga_real_client_denial_never_falls_back(tmp_path):
    from src.api.komga_client import KomgaClient
    from tests.test_komga_client import CONFIG, response
    from tests.test_komga_client import book as komga_row

    client = KomgaClient(data_dir=tmp_path / "komga", config=CONFIG)
    client.session.request = Mock()
    # Mismatched remote identity: the client refuses, so the library must too.
    client.session.request.return_value = response(komga_row(book_id="OTHER"))
    service, others = make_library(tmp_path, client)
    item = {"id": "audio", "media": {"metadata": {"title": "Title"}}}
    assert service.acquire_ebook(item, komga_book(ebook_source_id="BOOK1")) is None
    for other in others:
        assert not other.method_calls


def test_source_identity_is_canonical():
    assert normalize_ebook_source("komga") == "Komga"
    assert source_name_variants("Komga") == ("komga",)


class ImmediateThread:
    def __init__(self, target, kwargs, daemon):
        self.target, self.kwargs = target, kwargs

    def start(self):
        self.target(**self.kwargs)


@pytest.fixture
def polling(monkeypatch):
    import src.services.client_poller as module
    from src.services import write_tracker
    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    db, manager, client = Mock(), Mock(), Mock()
    db.get_linked_abs_ids.return_value = {"audio"}
    client.is_configured.return_value = True
    client.supports_book.return_value = True
    client.get_service_state.return_value = SimpleNamespace(current={"pct": .5, "ts": 2, "fragment": "new"})
    poller = ClientPoller(db, manager, {"Komga": client})
    poller._last_known[(7, "Komga", "audio")] = poller._state_fingerprint({"pct": .5, "ts": 1, "fragment": "old"})
    yield poller, db, manager, client
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()


def test_komga_custom_and_global_poll_modes(monkeypatch, polling):
    poller, _, _, _ = polling
    poller._poll_client = Mock()
    monkeypatch.setenv("KOMGA_SYNC_POLL_MODE", "global")
    poller._poll_cycle()
    poller._poll_client.assert_not_called()
    monkeypatch.setenv("KOMGA_SYNC_POLL_MODE", "custom")
    monkeypatch.setenv("KOMGA_SYNC_POLL_SECONDS", "300")
    poller._poll_cycle()
    poller._poll_client.assert_called_once_with("Komga")
    poller._poll_cycle()
    poller._poll_client.assert_called_once()


@pytest.mark.parametrize("client_name", ["Komga", "Storyteller"])
def test_membership_error_fails_closed(polling, client_name):
    poller, db, manager, client = polling
    db.get_linked_abs_ids.side_effect = RuntimeError("database unavailable")
    assert poller._poll_client_for_user(client_name, client, 7, [komga_book()], False) == 0
    client.get_service_state.assert_not_called()
    manager.sync_cycle.assert_not_called()


def test_missing_membership_result_fails_closed(polling):
    poller, db, manager, client = polling
    db.get_linked_abs_ids.return_value = None
    assert poller._poll_client_for_user("Komga", client, 7, [komga_book()], False) == 0
    client.get_service_state.assert_not_called()
    manager.sync_cycle.assert_not_called()


def test_komga_global_write_cannot_suppress_reader_event(polling):
    from src.services import write_tracker
    poller, _, manager, client = polling
    write_tracker.record_write("Komga", "audio", .5, user_id=None)
    poller._poll_client_for_user("Komga", client, 7, [komga_book()], False)
    manager.sync_cycle.assert_called_once_with(target_abs_id="audio", user_id=7)


def test_komga_same_reader_echo_is_suppressed(polling):
    from src.services import write_tracker
    poller, _, manager, client = polling
    write_tracker.record_write("Komga", "audio", .5, user_id=7)
    poller._poll_client_for_user("Komga", client, 7, [komga_book()], False)
    manager.sync_cycle.assert_not_called()


def test_other_clients_retain_global_echo_compatibility(polling):
    from src.services import write_tracker
    poller, _, manager, client = polling
    write_tracker.record_write("Storyteller", "audio", .5, user_id=None)
    poller._last_known[(7, "Storyteller", "audio")] = .4
    poller._poll_client_for_user("Storyteller", client, 7, [komga_book()], False)
    manager.sync_cycle.assert_not_called()


def test_komga_settle_wait_preserves_reader_namespace(monkeypatch, polling):
    poller, _, manager, client = polling
    monkeypatch.setenv("KOMGA_SYNC_POLL_WAIT_FOR_SETTLE", "true")
    wait = poller._is_settle_wait_enabled("Komga")
    assert wait
    poller._poll_client_for_user("Komga", client, 7, [komga_book()], wait)
    manager.sync_cycle.assert_not_called()
    poller._poll_client_for_user("Komga", client, 7, [komga_book()], wait)
    manager.sync_cycle.assert_called_once_with(target_abs_id="audio", user_id=7)


@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_komga_user_enumeration_never_falls_back_to_global(polling, failure):
    poller, db, _, _ = polling
    poller._registry = Mock()
    db.list_users.return_value = []
    if failure == "exception":
        db.list_users.side_effect = RuntimeError("database unavailable")
    assert poller._poll_targets("Komga") == []


def test_registry_binds_reader_clients_parser_database_and_library(monkeypatch, tmp_path):
    from collections.abc import Mapping

    import src.services.user_client_registry as module
    db, parser, alignment = Mock(), Mock(), Mock()
    creds = {"KOMGA_ENABLED": "true", "KOMGA_SYNC_ENABLED": "true", "KOMGA_API_KEY": "reader-key"}
    db.get_user_credentials.return_value = creds
    db.get_user.return_value = SimpleNamespace(id=7, is_admin=False)
    catalog, progress, adapter = Mock(), Mock(), Mock()
    monkeypatch.setattr(module, "KomgaClient", catalog)
    monkeypatch.setattr(module, "KomgaSyncApi", progress)
    monkeypatch.setattr(module, "KomgaSyncClient", adapter)
    registry = module.UserClientRegistry(db, parser, alignment, epub_cache_dir=str(tmp_path / "cache"))
    bundle = registry.get_clients(7)
    assert bundle.komga_client is catalog.return_value
    assert bundle.komga_sync_api is progress.return_value
    assert bundle.sync_clients["Komga"] is adapter.return_value
    assert bundle.library_service.komga_client is bundle.komga_client
    assert catalog.call_args.kwargs["ebook_parser"] is parser
    assert adapter.call_args.kwargs == {"user_id": 7, "database_service": db}
    assert adapter.call_args.args == (progress.return_value, catalog.return_value, parser)
    assert bundle.credentials[_ALLOW_GLOBAL_FALLBACK_KEY] is False
    # The API clients share one live, gate-aware view of this reader's settings.
    view = catalog.call_args.kwargs["config"]
    assert isinstance(view, Mapping)
    assert view is progress.call_args.kwargs["config"]
    assert view is bundle.credentials or view.credentials is bundle.credentials
    assert view["KOMGA_API_KEY"] == "reader-key"
    assert view["KOMGA_ENABLED"] == "true"
    assert view["KOMGA_SYNC_ENABLED"] == "true"
    assert view["KOMGA_SERVER"] == ""  # no global server configured in this test


def test_di_registers_komga_with_shared_dependencies(tmp_path):
    from dependency_injector import providers

    from src.utils.di_container import Container
    container = Container()
    parser, db = Mock(), Mock()
    container.ebook_parser.override(providers.Object(parser))
    container.database_service.override(providers.Object(db))
    adapter = container.komga_sync_client()
    assert container.sync_clients.kwargs["Komga"] is container.komga_sync_client
    assert container.library_service.kwargs["komga_client"] is container.komga_client
    assert container.komga_client.kwargs["ebook_parser"] is container.ebook_parser
    assert container.komga_sync_client.kwargs["database_service"] is container.database_service
    assert container.komga_sync_client.kwargs["user_id"] is None
    assert adapter is container.komga_sync_client()


@pytest.mark.parametrize("role,primary", [("user", False), ("admin", False), ("admin", True)])
def test_real_registry_komga_inheritance_is_primary_admin_only(monkeypatch, role, primary):
    from src.services.user_client_registry import UserClientRegistry
    monkeypatch.setenv("KOMGA_ENABLED", "true")
    monkeypatch.setenv("KOMGA_SYNC_ENABLED", "true")
    monkeypatch.setenv("KOMGA_API_KEY", "global-reader-key")
    monkeypatch.setenv("KOMGA_SERVER", "https://komga.example")
    db = Mock()
    db.get_user_credentials.return_value = {}
    db.get_user.return_value = SimpleNamespace(id=7, is_admin=role == "admin")
    db.is_primary_admin.return_value = primary
    bundle = UserClientRegistry(db, Mock(), Mock()).get_clients(7)
    assert bundle.komga_client.is_configured() is primary
    assert bundle.komga_sync_api.is_configured() is primary
    assert bundle.sync_clients["Komga"].is_configured() is primary


def test_real_registry_cached_clients_observe_dynamic_global_and_reader_gates(monkeypatch):
    from src.services.user_client_registry import UserClientRegistry
    monkeypatch.setenv("KOMGA_ENABLED", "true")
    monkeypatch.setenv("KOMGA_SYNC_ENABLED", "true")
    monkeypatch.setenv("KOMGA_SERVER", "https://komga.example")
    db = Mock()
    db.get_user.return_value = SimpleNamespace(id=7, is_admin=False)
    db.get_user_credentials.return_value = {
        "KOMGA_ENABLED": "true", "KOMGA_SYNC_ENABLED": "true", "KOMGA_API_KEY": "reader-key",
    }
    bundle = UserClientRegistry(db, Mock(), Mock()).get_clients(7)
    assert bundle.komga_client.is_configured()
    assert bundle.sync_clients["Komga"].is_configured()
    # A cached bundle reflects a global gate change without being rebuilt:
    # the transport reads its config view (resolve_setting) at call time.
    monkeypatch.setenv("KOMGA_SYNC_ENABLED", "false")
    assert not bundle.sync_clients["Komga"].is_configured()
    assert not bundle.komga_client.is_configured()
    monkeypatch.setenv("KOMGA_SYNC_ENABLED", "true")
    monkeypatch.setenv("KOMGA_ENABLED", "false")
    assert not bundle.komga_client.is_configured()
    assert not bundle.sync_clients["Komga"].is_configured()
    monkeypatch.setenv("KOMGA_ENABLED", "true")
    # ...and a reader-side change to the same stored credentials dict is observed too.
    bundle.credentials["KOMGA_SYNC_ENABLED"] = "false"
    assert not bundle.sync_clients["Komga"].is_configured()
    assert not bundle.komga_client.is_configured()
    bundle.credentials["KOMGA_SYNC_ENABLED"] = "true"
    assert bundle.sync_clients["Komga"].is_configured()
    assert bundle.komga_client.is_configured()
