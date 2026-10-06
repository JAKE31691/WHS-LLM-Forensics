# Parser migration notes (v0.4)

Source basis: `semantic_diff_v0.8.9`.

The old tool mixed two concerns:

1. decode one Chromium artifact into logical records;
2. compare BEFORE/AFTER records and emit CREATED/DELETED/MODIFIED/RESIDUAL.

v0.4 retains only concern 1 in `llmtrace/common/parsers/`.

## Migrated parser paths

| New parser | Source concept reused | First-stage output policy |
|---|---|---|
| chromium_history | SQLite snapshot/table/row reader | every table row, original columns |
| chromium_cookies | same SQLite reader | every table row, original columns including blobs |
| chromium_dips | same SQLite reader | every table row, original columns |
| chromium_favicons | same SQLite reader | every table row, original columns |
| chromium_preferences | JSON load | original JSON tree |
| chromium_local_storage | CCL `LocalStoreDb.iter_all_records(include_deletions=True)` | every CCL record; no current/residual classification |
| chromium_session_storage | CCL `RawLevelDb.iterate_records_raw()` | every LevelDB record; no host/map reconstruction in stage 1 |
| chromium_cache | CCL cache backend/key/metadata/body reader | raw cache key, metadata object fields, raw body bytes; no previews/hashes/URL semantics |
| chromium_sessions | SNSS framing reader | SNSS version, command id, raw command payload |
| chromium_indexeddb | RawLevelDb reader | IndexedDB container 아래 LevelDB 원시 record |

## Deliberately not migrated into Stage 1

- BEFORE/AFTER comparison
- Change / ArtifactResult
- CREATED / DELETED / MODIFIED / RESIDUAL classification
- focus search/highlighting
- row-identity heuristics for diffing
- URL/service interpretation
- Cache body text preview, decoded-content hash and semantic fingerprint
- Cache current/obsolete guesses
- Sessions generation/position normalization
- ChatGPT/Claude fact_type or behavior attribution

These belong to later processing, not native artifact parsing.
