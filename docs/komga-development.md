# Komga integration — development status and gates

This branch (`feature/komga-integration`, based on upstream `dev`) adds an opt-in
Komga ebook/progress adapter to BookBridge. It is **not** a replacement for
Komga's own Kobo service: a stock Kobo keeps using the `api_endpoint` it already
has, and BookBridge only reads/writes Komga's REST and native Kobo reading-state
APIs with a reader's own API key.

Source-pinned API evidence (commit hashes, DTO shapes, route tables) is in
`docs/komga-contract-reference.md`; the settings are documented in
`docs/configuration.md`.

## Development boundary

- **Commit and push to the author's fork are authorized for review. No PR or
  issue is authorized.** Opening an upstream PR requires explicit confirmation
  and coordination; upstream feature PRs target `dev`, whose Python CI does not
  currently run on pull requests to it.
- **No deployment, no DNS/proxy change, no migration of production data.**
- **No contact with a private Komga/Audiobookshelf instance and no credential
  handling.** Every test is offline: outbound `requests.Session.request` is
  replaced, or a loopback test server is used for the transport tests.
- **Stock Kobo delivery is untouched.** No code path changes a device's Komga
  URL, and BookBridge never proxies book downloads to the device.

## Review status: implemented does not mean device-verified

Catalog access, explicit pairing, settings, polling, incoming progress parsing
and concrete-bookmark outgoing writes are implemented and tested offline.
Neither direction has been validated against a running Komga/ABS pair or a
physical Kobo. All enable flags remain off by default.

There is **no percentage-only fallback**: an outgoing write includes a concrete
chapter-and-span bookmark plus separate chapter and whole-book progress. If the
bookmark cannot be resolved, the adapter skips the write and preserves observed
state. This is a safety restriction, not a missing percentage-write feature.
The reverse-enable toggle is a manual operational gate, not proof that a device
test has passed; content checks compare server artifacts, not the physical
device's installed copy.

Offline results on the implementation tree: 250 new Komga tests passed; the
full suite had 5559 passed, 34 skipped, 404 subtests passed and five failures
caused by missing Lua, also reproduced on unmodified upstream `dev`.
The critical flake8 error gate and diff checks passed. Full coverage validation
remains incomplete due to a SQLAlchemy setup error under coverage tracing; do
not infer CI coverage success from the passing non-coverage tests.

## What is implemented

| Area | Files |
| --- | --- |
| Bounded, same-origin transport + archive validation | `src/api/komga_transport.py` |
| REST catalog (exact ID + paged search) and original-EPUB acquisition | `src/api/komga_client.py` |
| Native Kobo reading state, advertised KEPUB, full timestamped envelope, preflight/read-back | `src/api/komga_sync_api.py` |
| Content identity, served-KEPUB verification, exact placement | `src/utils/komga_content.py` |
| `KomgaSyncClient` (the existing `SyncClient` contract) | `src/sync_clients/komga_sync_client.py` |
| Settings, per-reader key/gates, DI, registry, library acquisition, polling | `src/utils/*.py`, `src/services/*.py` |
| Catalog selection, exact-ID pairing hash, connection test, source labels | `src/web_server.py`, `templates/*` |

Read direction: `GET /kobo/{key}/v1/library/{id}/state` → normalised
`ServiceState` (`pct`, `href`, `frag`, `chapter_progress`, `service_updated_at`,
`status`). An unreadable, ambiguous, malformed, non-finite or future-dated row
yields **no state at all** — it is never converted into a reset or a synthetic
zero.

Write direction: resolve an exact span in the KEPUB Komga advertises for the
device, send the full `ReadingStates` envelope with separate total and
chapter progress, re-read the exact target and only report the position Komga
actually stored.

## Safety defaults and the reverse-write gate

Everything is opt-in and off by default:

- `KOMGA_ENABLED=false`, `KOMGA_SYNC_ENABLED=false`, `KOMGA_REVERSE_SYNC_ENABLED=false`.
- `KOMGA_REVERSE_SYNC_ENABLED` is the **explicit reverse gate**. It is a real
  per-reader setting (admin switch plus reader toggle, both in the UI) that is
  re-read on every call, so disabling it stops writes without a restart. It is
  checked in three places: the settings UI documents it, `KomgaSyncApi.reverse_writes_enabled()`
  is the enforcement point, and `KomgaSyncClient.update_progress` returns
  `SyncResult(success=True, skipped=True)` with a logged reason so a disabled
  gate is visible rather than a silent no-op.
- Enabling it is not enough on its own. A write still requires all of:
  1. a verified span map of the served KEPUB whose chapters are documents of the
     canonical source EPUB the bridge aligned against;
  2. an exact (never suffix-matched) chapter href and a finite in-chapter
     progression that resolves to a concrete `kobo.N.M` span;
  3. a baseline read of the same book from the same cycle whose principal
     fingerprint still matches (a rotated or different key refuses);
  4. a positive read-back of the exact target whose location, chapter progress
     and status match what was sent.
  Anything else is a skip or a failure. There is no percentage-only write,
  because Komga rejects a null `Location` and a Kobo only moves for a span.
- Secrets: `KOMGA_API_KEY` is encrypted at rest like the other credential fields,
  and enabling the integration is refused outright when credential encryption is
  unavailable rather than silently storing a plaintext key.
- A declined write (gate off, unplaceable position, missing baseline) reports
  `success=True, skipped=True` and records only what the bridge actually read
  from Komga this cycle — never the percentage it chose not to write, because the
  sync manager persists a successful follower result as that client's state row.
- Per reader: keys and gates resolve per user; only the primary admin's account
  keys mirror into the global engine clients. A reader's polling never falls
  back to another reader's client, and membership-lookup failure fails closed.
- Token-bearing URLs and errors are redacted; the API key never appears in a
  log, an error message or a rendered page.

## What deterministic tests can and cannot establish

The suites below prove the *code's* behaviour: contract shapes, rejection of
malformed/future/ambiguous data, preflight/read-back discipline, exact placement
against synthetic archives, cache invalidation, per-reader isolation and
settings/DI/pairing wiring. They are all offline and use synthetic EPUB/KEPUB
fixtures — none of them is a captured Komga response.

They **cannot** establish:

- that a particular Komga release behaves as the pinned source says (version,
  `Kepubify` availability, role/`FILE_DOWNLOAD` behaviour for the bridge key);
- that the KEPUB served to a physical Kobo carries the same addressable span
  namespace as the bytes this client downloads;
- that a stock Kobo actually moves to a written position, or that its own upload
  interacts with a bridge write as assumed;
- that the total/chapter percentage quantization and timestamp semantics match
  the device in practice.

Those are live/device facts and remain unverified here.

## Deferred work: the live + device gate (Stage A of the plan)

Before the reverse direction is enabled for a real reader:

1. Record the deployed Komga release and the device model/firmware, export the
   disposable release's OpenAPI, and re-check the Kobo contracts against the
   pinned source in `docs/komga-contract-reference.md`.
2. Create disposable Komga/ABS users and content; use a **separate API key** for
   the bridge; verify missing roles, private-library restriction and
   two-reader isolation before any write.
3. Read exact-ID metadata, state and download metadata; take the source EPUB and
   the advertised KEPUB, and compare the KEPUB on the device with the downloaded
   bytes (compressed digest may differ; compare uncompressed spine resources and
   the span namespace).
4. Read a marked paragraph mid-chapter on the Kobo, sync, and check the ABS
   position obtained from a real alignment.
5. Controlled reverse write, then re-open on the device and confirm it actually
   moved — a dashboard percentage is not proof.
6. Equal/older timestamp rejection, clock skew, a concurrent device update,
   finished/reset behaviour, bridge restart, and the next device-upload echo.

Until that passes, keep `KOMGA_REVERSE_SYNC_ENABLED` off. If placement cannot be
made exact, report the limitation instead of weakening the gate.

## Reproducing the validation

With the project's runtime and test dependencies installed in an isolated
environment, run the new suites from the repository root:

```sh
env -u PYTHONPATH python -m pytest tests/test_komga_client.py tests/test_komga_content.py tests/test_komga_pairing.py tests/test_komga_sync_api.py tests/test_komga_sync_client.py tests/test_komga_wiring.py -q
env -u PYTHONPATH python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
git diff --check
```

The following additional wrapper and raw logs are local-only artifacts, not
files included in the review branch:

```sh
bash data/komga-validation/run-validation.sh 2>&1 | tee data/komga-validation/full-run.log
```

The script uses the isolated virtualenv at
`/home/trevor/.hermes/cache/scratch/bookbridge-komga-venv`, removes any inherited
`PYTHONPATH`, points `DATA_DIR`/`BOOKS_DIR` at disposable paths under
`data/komga-validation/`, and writes per-suite logs plus a JUnit/extended log
next to this file. `data/` is ignored by git, so these artifacts are local-only.

## Open items

- The reverse gate is implemented but **unverified against a real device**
  (see above).
- **Completion/reset semantics are threshold-based** (`>= 99%` → Finished,
  explicit `0` → ReadyToRead, otherwise Reading). Komga derives its own
  completion from its analysed positions and its `ReadyToRead` is not a
  documented reset API, so this mapping is one of the facts the device check has
  to confirm before the write direction is relied on.
- **A second reader pairing a different Komga edition of the same ABS row**
  re-aligns the shared row: the existing `_preserve_or_reset_mapping_status`
  guard treats a changed `ebook_source`/`ebook_source_id` as a genuine re-map and
  refuses to keep the old alignment, which is safe but disruptive for the first
  reader. A reader-scoped Komga link (per-reader remote book + canonical artifact
  identity) would fix that and needs its own schema+persistence work, so it is
  deliberately not part of this change.
- `KomgaClient` caches nothing across polls by design; the served-KEPUB span map
  is cached per `(server, reader fingerprint, book, content revision)` and is
  re-fetched when any of those change.
- Komga exposes no conditional write (CAS), so a bridge write can still
  interleave with a simultaneous device write. BookBridge serializes writes per
  `(server, key, book)` and preflights, which narrows the window but does not
  close it.
