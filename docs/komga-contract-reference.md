# Komga contract reference (source-pinned)

Evidence for `src/api/komga_client.py`, `src/api/komga_sync_api.py` and
`src/api/komga_transport.py`. Everything below was read from public source; no
live Komga instance, credentials, device or deployment was involved.

## Pinned source

- Repository: `gotson/komga`
- Commit: `133d30d7bc7715fb4144be56486337db1ad4c525`
- Files retrieved (raw.githubusercontent.com, that commit):
  - `komga/src/main/kotlin/org/gotson/komga/interfaces/api/kobo/KoboController.kt`
  - `.../interfaces/api/kobo/dto/{ReadingStateDto,ReadingStateStateUpdateDto,ReadingStateUpdateResultDto,ResultDto,BookmarkDto,StatisticsDto,StatusInfoDto,LocationDto,KoboBookMetadataDto,BookEntitlementDto}.kt`
  - `.../interfaces/api/rest/BookController.kt`, `.../interfaces/api/CommonBookController.kt`
  - `.../domain/model/{BookSearch,SearchCondition}.kt`, `.../domain/service/BookLifecycle.kt`

## REST surface (`X-API-Key` principal)

| Need | Request | Notes |
| --- | --- | --- |
| Exact book | `GET /api/v1/books/{bookId}` (`BookController.getBook`) | Returns `BookDto`; `id`, `libraryId`, `seriesId`, `deleted`, `media{mediaType,status}`, `metadata{title,authors,numberSort}`, `fileHash`, `fileLastModified`, `sizeBytes` |
| Catalog search | `POST /api/v1/books/list` (`BookController.getBooks`) | Body is `BookSearch {condition?, fullTextSearch?}`; paginated `Page<BookDto>`; deprecated `GET /api/v1/books` is not used |
| Original file | `GET /api/v1/books/{bookId}/file` (`CommonBookController.downloadBookFile`) | Raw source bytes. Requires `FILE_DOWNLOAD`. `BookController.getBookFile` adds `convert_kepub=true` and returns a converted stream instead |

- `media.mediaType == "application/epub+zip"` and `media.status == "READY"` are
  the EPUB gates; `deleted` identifies soft-deleted books.
- Known contract gaps (fail closed locally): `id` is opaque and unchanged, and
  neither endpoint is documented as conditional, so a same-ID replacement is a
  real (documented) gap. `metadata.title` is not a unique identifier.
- `SearchCondition`/`BookSearch` deserialise with Jackson `Id.DEDUCTION`; the
  bridge only sends `fullTextSearch`, so no condition-shape guessing is needed.
- A Page body without `content`/`number`/`last` (or with repeated ids) is
  treated as a failed scan rather than a partial catalog.

## Native Kobo surface (bridge key = REST key)

`KoboController` is mapped at `/kobo/{authToken}/` and authenticates with the
same `KomgaPrincipal`/API key as REST (`contentRestrictionChecker` runs on both
paths), so one key and one principal serve REST and Kobo.

| Need | Request |
| --- | --- |
| State read | `GET /kobo/{key}/v1/library/{bookId}/state` → `List<ReadingStateDto>` (one element) |
| State write | `PUT /kobo/{key}/v1/library/{bookId}/state` → `RequestResultDto` |
| Metadata | `GET /kobo/{key}/v1/library/{bookId}/metadata` → `List<KoboBookMetadataDto>` |
| Download | `GET /kobo/{key}/v1/books/{bookId}/file/epub?convert_kepub=<bool>` |

Everything is `UpperCamelCase` on the wire (`@JsonNaming`), and nullable DTO
fields are omitted (`@JsonInclude(NON_NULL)`).

### State DTO shape

`ReadingStateDto`: `Created?`, `CurrentBookmark`, `EntitlementId`,
`LastModified`, `PriorityTimestamp?`, `Statistics`, `StatusInfo`.
`BookmarkDto`: `LastModified`, `ProgressPercent?` (0-100, whole book),
`ContentSourceProgressPercent?` (0-100, within the resource), `Location?` =
`LocationDto {Value?, Type? (default "KoboSpan"), Source}`.
`StatisticsDto`: `LastModified`, `RemainingTimeMinutes?`, `SpentReadingMinutes?`.
`StatusInfoDto`: `LastModified`, `Status`, `TimesStartedReading?`,
`LastTimeFinished?`, `LastTimeStartedReading?`. `StatusDto` values seen in
`toDto()` are `Reading`, `Finished`, `ReadyToRead`.

Each of the four timestamps is `LastModified`; `PriorityTimestamp` mirrors the
state `LastModified`. Komga derives all of them from the single
`ReadProgress.lastModifiedDate`, so — unlike CWA — there is no device-only
bookmark clock. Consumers must keep their own echo/freshness guard.

### Write request/response

- Request body (`ReadingStateStateUpdateDto`): `{"ReadingStates": [ReadingStateDto]}`.
  `KoboController.updateState` requires the first entry, and requires both
  `CurrentBookmark.Location` and `ContentSourceProgressPercent` to be non-null
  (`BAD_REQUEST` otherwise), so a whole-book percentage alone is rejected.
- `ProgressPercent` is **recomputed** server-side. `BookLifecycle.markProgression`
  matches the submitted `progression` against the book's stored EPUB positions
  and copies *that* position's `totalProgression` ("don't trust the provided
  total progression, the one from Kobo can be wrong"). A returned total
  percentage is therefore not proof of what was asked for.
- The submitted `modified` must be **strictly after** the stored
  `readDate` (`check { modified.isAfter(savedProgress.readDate) }`), otherwise
  the write fails; the clock is compared in the server's system zone. Sending an
  equal or older timestamp cannot succeed, so the bridge preflights instead of
  trying to win a race.
- Response (`RequestResultDto`): `RequestResult` ∈ `Success|Failure|Ignored`, and
  `UpdateResults: [ReadingStateUpdateResultDto{EntitlementId,
  CurrentBookmarkResult{Result}, StatisticsResult{Result}, StatusInfoResult{Result}}]`.
  On success Komga returns `CurrentBookmarkResult=Success`,
  `StatusInfoResult=Success`, `StatisticsResult=Ignored` (statistics are not
  stored from this endpoint), and HTTP is 200 even when `RequestResult=Failure`.
  Only the `Result` fields prove what was applied.

### Advertised download

`KoboController.withDownloadUrls` builds the URL from the current context path:
`{context}/kobo/{token}/v1/books/{bookId}/file/epub?convert_kepub={convert}`
with `Format = KEPUB` (and `convert=true`) when the file is already a kepub or
Kepubify is available, `EPUB3FL` for fixed layout, else `EPUB3`. Dropping the
query parameter fetches a different payload, and `convert_kepub=true` can fail
with `503` when conversion fails. A reverse-proxy base path is part of the
advertised URL, so the bridge compares it against its own configured origin and
prefix and never rewrites the host.

## Deliberate limitations

- Komga exposes no conditional write/CAS for reading state, so a bridge write
  can still interleave with a simultaneous stock-device write. Local
  serialization per (server, key, book) and the preflight compare make that
  window small but not zero; the reference clock is at 1s/ISO-8601 microsecond
  granularity, not sequence numbers.
- Unverified here (needs the separately authorised live spike): Kepubify
  availability on the target instance, `FILE_DOWNLOAD` for the bridge key, the
  precise `positions` data behind total-progression recomputation, and
  device/file identity of the served KEPUB.

## Python interface the adapter should consume

Both clients are explicit-config: `KomgaClient(ebook_parser=None, data_dir=None,
config=None)` and `KomgaSyncApi(config=None, data_dir=None)`. `config` is a
mapping of `KOMGA_*` settings (a per-reader bundle); when it is `None` the
module reads `os.environ`. `KOMGA_ENABLED` must be present and non-falsey
(opt-in, no inheritance); if `KOMGA_SYNC_ENABLED` is supplied it must not be
falsey, and `update_reading_state` additionally refuses when a supplied
`KOMGA_REVERSE_SYNC_ENABLED` is falsey. `KOMGA_SERVER` may carry a
reverse-proxy path prefix, which is preserved verbatim; `KOMGA_API_KEY` is the
single key used for REST and Kobo.

`KomgaClient`:

- `is_configured() -> bool` and `check_connection() -> bool` (a 1-item
  `POST /api/v1/books/list`, never the Kobo library-sync endpoint).
- `search_ebooks(query: str = "") -> list[dict]` — paginated
  `BookSearch.fullTextSearch`; each item carries `id`, `title`, `author`/
  `authors`, `filename`/`fileName`, `series_id`, `series_title`, `seriesIndex`,
  `library_id`, `file_hash`, `file_modified_at`, `size_bytes`, `source="Komga"`,
  `ext="epub"`. A partial/failed scan returns `[]`.
- `get_book(book_id) -> dict | None` — exact ID only; rejects deleted,
  non-EPUB, not-ready and mismatched records.
- `download_ebook(book_id) -> str | None` — validates and atomically publishes
  the **original** `GET /api/v1/books/{id}/file` bytes under `books_dir`
  (`data_dir/ebooks/komga_{id}.epub`) and returns the basename.

`KomgaSyncApi`:

- `get_reading_state(book_id) -> dict | None` with `progress_percent` (total,
  0-1), `chapter_progress` (0-1, distinct), `href`, `frag`, `status`,
  `service_updated_at` (Komga's shared progress clock — the poller fingerprint
  should use this, never the observation time), `wire` (the exact DTO to send
  back) and `identity` (`{book_id, server, principal-sha256}`). `None` means the
  response was ambiguous, unknown, malformed, non-finite, out of range or
  future-dated — never a reset-to-zero.
- `get_metadata(book_id) -> dict | None`, with the API key redacted from the
  advertised URLs.
- `download_kepub(book_id) -> bytes | None` — only an advertised `KEPUB` entry
  on the configured origin, the exact same-key
  `/kobo/{key}/v1/books/{id}/file/epub` path, and the `convert_kepub` query
  preserved; the bytes are archive-validated.
- `update_reading_state(book_id, progress_percent, status="Reading",
  location=None, *, chapter_progress=None, expected_state=None) -> dict | None`
  — pass the `location` `{Source, Type, Value}` (a Kobo needs a span, not just a
  percentage), the chapter progression separately, and `expected_state=`
  the previously read dict. It re-checks the live state, writes the fully
  timestamped DTO once, and returns the read-back normalised dict (as
  `get_reading_state`) or `None`. `None` is the only failure signal: never treat
  it as "already fine", and never blindly retry.
