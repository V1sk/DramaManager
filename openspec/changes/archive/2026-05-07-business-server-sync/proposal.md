## Why

The five preceding changes built a complete staging editor: drama as an entity, multi-language metadata, tags, actors, subtitles, the polished admin UX, and OSS staging/prod path separation. Operators can now create / review / play episodes locally — but **prod traffic still has nothing to serve**. This change is the bridge: a manually-triggered sync flow that pushes drama and episode state from this server (staging) to the business server (prod), with state-machine bookkeeping so operators always know what's been published and what hasn't.

The decisions established in earlier explore conversations are wired in here:
- Sync is **manual** — every push is a deliberate operator action so review-before-publish is the workflow's default.
- Drama-level and episode-level sync state are **independent**, so editing a drama's synopsis doesn't dirty its already-published episodes (or vice versa).
- Delete is **two-phase** for synced rows (mark `pending_delete`, sync the deletion, then physically remove) and one-phase for never-synced rows (physical delete immediately).
- The business server's wire protocol is **co-designed here** since it doesn't exist yet — this change defines both sides' contracts.

This is the final piece of the original 7-feature roadmap. After this change, the operator workflow is end-to-end functional.

## What Changes

### State machine

- Add `sync_status TEXT NOT NULL DEFAULT 'dirty'`, `sync_error TEXT`, `last_synced_at TEXT` columns to both `dramas` and `episodes`. Status values: `dirty` / `syncing` / `clean` / `sync_failed` / `pending_delete`.
- Mutating endpoints flip `sync_status='dirty'` on the affected drama or episode:
  - Drama dirty triggers: drama create, `PATCH /admin/dramas/{slug}`, all translation upserts/deletes (name/synopsis/poster), `PUT /admin/dramas/{slug}/tags`, `PUT /admin/dramas/{slug}/actors`.
  - Episode dirty triggers: episode upload (initial or re-encode), cover replace, subtitle add/replace/delete.
- Library mutations cascade dirty to dependent dramas:
  - Tag PATCH/translation upsert/delete → every drama referencing the tag goes dirty.
  - Actor PATCH/translation upsert/delete → every drama referencing the actor goes dirty.
  - Language `display_label` change → every drama with subtitles in that language (transitively, through episodes) goes dirty (the language label travels with subtitle metadata).
- Drama and episode dirty states are independent: editing a drama does not dirty its episodes; uploading a new episode does not dirty the drama.

### Sync action endpoints (HLS server)

- `POST /admin/dramas/{slug}/sync` — schedule a drama sync (drama metadata + every dirty episode + every pending_delete episode). Returns 202 Accepted with the drama's current sync state. Performs the actual work via a background sync worker.
- `POST /admin/episodes/{slug}/{ep}/sync` — schedule a single-episode sync. Returns 202 Accepted. Returns 409 if the drama has never been synced (`drama.last_synced_at IS NULL`).
- `GET /admin/sync` — overview page listing every dirty / sync_failed / pending_delete drama and episode for at-a-glance triage.

### Two-phase delete

- `DELETE /admin/episodes/{slug}/{ep}` and `DELETE /admin/dramas/{slug}` branch on `last_synced_at`:
  - **Never synced** (`last_synced_at IS NULL`): physical delete + local cleanup + staging OSS cleanup (existing path).
  - **Synced before**: row is marked `sync_status='pending_delete'` and **kept**. Local cleanup happens (the operator no longer wants the local files); staging OSS cleanup happens. The row is physically removed only after a successful sync that propagates the delete to the business server (and removes the prod-prefix OSS objects via `unpublish_*_from_prod`).

### Sync worker

- New background `asyncio.Queue` (`sync_queue`) consumed by a single `sync_worker_loop` coroutine, separate from the existing pipeline worker. One sync job at a time across the whole process (FIFO), so the operator's manual triggers don't interleave unpredictably.
- Job types: `SyncDramaJob(slug)` and `SyncEpisodeJob(slug, ep_number)`. The drama job internally enqueues episode jobs for every dirty/pending_delete child.
- On startup, `sync_status='syncing'` rows are flipped to `sync_failed` with `error="orphaned by restart"` (mirrors the pipeline worker's reap behavior).

### Business server wire protocol

The business server (a separate codebase to be built later) MUST expose four endpoints under `/sync/*`. This change defines their request/response shapes; the business server's implementation honors these contracts.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sync/dramas` | Upsert a drama with all translations, tags, actors, languages used by the drama, and the drama-level posters (URLs the business server pulls from staging). |
| `DELETE` | `/sync/dramas/{slug}` | Remove a drama and everything attached to it. |
| `POST` | `/sync/episodes` | Upsert an episode, including the three prod-flavored m3u8 texts inline, DRM key + IV, cover URL (pull), and subtitle URL list (pull each). |
| `DELETE` | `/sync/episodes/{slug}/{ep}` | Remove an episode. |

Every request carries `X-API-Key: <shared secret>`. The shared secret is configured on the HLS side via env var `BUSINESS_SYNC_API_KEY` and on the business server via its own equivalent env. Mismatch → 401.

The HLS sync worker, before calling `POST /sync/episodes`, calls `publish_ladder_to_prod` (from `oss-staging-prod-separation`) for each ladder to copy `Drama/staging/{slug}/...` → `Drama/prod/{slug}/...` and obtain the prod-flavored m3u8 strings that go into the request body.

For deletes that propagate, the HLS worker also calls `unpublish_*_from_prod` after the business server returns 2xx, removing OSS prod objects.

The business server pulls cover / poster / subtitle binaries from the URLs in the payload (synchronously during the sync request); failure to pull any → 502.

### Configuration

- `BUSINESS_SYNC_API_KEY` (required when `BUSINESS_SYNC_BASE_URL` is set; otherwise sync is disabled and the action endpoints return 503).
- `BUSINESS_SYNC_BASE_URL` (e.g. `https://prod-internal.example.com`). When unset, sync is disabled.

### UI

- Drama-detail page's "[同步整部剧]" button (currently a disabled placeholder per `admin-redesign`) becomes functional. Disabled when drama is `clean` and no episodes are dirty/pending_delete.
- Episode-detail page's "[同步本集]" placeholder becomes functional.
- Per-row sync badges (slots reserved in step 4) render as colored badges: clean (green), dirty (yellow), syncing (blue with spinner), sync_failed (red, click to view error), pending_delete (orange).
- Homepage cards gain a small sync indicator (count of dirty/pending_delete things) in the top-right corner.
- Nav-bar `<div id="sync-zone">` shows a small "需同步: N" link to `/admin/sync`.

## Capabilities

### New Capabilities

- `business-server-sync`: the sync state machine (status fields + transitions + dirty cascade), HLS-side sync action endpoints, the sync worker, two-phase delete behavior, UI wiring, configuration env vars, and the wire protocol contract that the business server must honor (request/response schemas for `POST /sync/dramas`, `DELETE /sync/dramas/{slug}`, `POST /sync/episodes`, `DELETE /sync/episodes/{slug}/{ep}`).

### Modified Capabilities

- `drama-entity`: dramas table gains `sync_status` / `sync_error` / `last_synced_at` columns. Drama deletion endpoint behavior branches on `last_synced_at`.
- `hls-management-server`: episodes table gains the same three columns. The admin episode-list endpoint includes `sync_status`. Configuration adds two new env vars.
- `episode-deletion`: episode deletion endpoint behavior branches on `last_synced_at` (two-phase delete).
- `admin-redesign`: the reserved sync placeholder slots (`<div id="sync-zone">`, `<span class="sync-badge">`, "[同步整部剧]" / "[同步本集]" buttons) are populated with functional UI. Drama detail and episode detail polling refresh logic includes sync state.

## Impact

- **Code**: large additions across `app/db.py` (sync_status helpers, dirty cascade), new `app/sync.py` module (worker loop + business-server HTTP client), new `app/routers/sync.py` (admin sync endpoints + sync overview page), updates to existing routers (mark dirty on mutate, branch on delete), `templates/sync.html` (overview), updates to drama/episode/home templates (badge rendering + button wiring), `app/main.py` (start sync worker, reap orphaned syncing on restart), `requirements.txt` (`httpx` for the HTTP client).
- **Schema**: 6 new columns (3 each on dramas + episodes). Destructive recreate of `hls.db` is acceptable (no production data).
- **External contracts**:
  - SDK-facing endpoints (`/api/*`) on this server unchanged.
  - SDK-facing endpoints on the business server defined by this change but implemented elsewhere.
  - New `/sync/*` protocol owned by this change.
- **External dependencies**: `httpx` (preferred over `requests` for async). One-line additions to `requirements.txt`.
- **Operational**: the HLS server now needs network access to the business server when sync is enabled. Observable via the sync overview page and structured logs.
- **Final scope**: completes the original 7-feature roadmap.
