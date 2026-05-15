## 1. Schema and DB helpers

- [x] 1.1 Add `sync_status TEXT NOT NULL DEFAULT 'dirty'`, `sync_error TEXT`, `last_synced_at TEXT` to the `dramas` and `episodes` DDL in `app/db.py`.
- [x] 1.2 `db.set_drama_sync_status(slug, status, *, error=None, last_synced_at=None)`: updates the three columns. Refresh `updated_at`.
- [x] 1.3 `db.set_episode_sync_status(slug, ep_number, status, *, error=None, last_synced_at=None)`: same, for episodes.
- [x] 1.4 `db.mark_drama_dirty(slug)`: sets `sync_status='dirty'` only if not currently `pending_delete`. Refreshes `updated_at`.
- [x] 1.5 `db.mark_episode_dirty(slug, ep_number)`: same posture.
- [x] 1.6 `db.cascade_dirty_dramas_via_tag(tag_slug)`, `db.cascade_dirty_dramas_via_actor(actor_slug)`, `db.cascade_dirty_dramas_via_language(lang_code)`: bulk UPDATE that flips every dependent drama to dirty (skipping `pending_delete`).
- [x] 1.7 `db.list_episodes_needing_sync(slug)`: returns ep_numbers where `sync_status ∈ {dirty, pending_delete}`.
- [x] 1.8 `db.physical_delete_drama(slug)` and `db.physical_delete_episode(slug, ep_number)`: only invoked by the sync worker after a successful delete-sync.
- [x] 1.9 Reap-on-startup: `db.reap_orphaned_syncing()` flips every row with `sync_status='syncing'` to `sync_failed` with `error='orphaned by restart'`. Called from the lifespan startup hook.

## 2. Configuration

- [x] 2.1 Extend `app/config.py` `Settings` with `business_sync_base_url: str | None`, `business_sync_api_key: str | None`, `business_sync_timeout: int = 30`. Read from env.
- [x] 2.2 At startup (`lifespan`), if `business_sync_base_url` is set but `business_sync_api_key` is not, raise a clear error and exit non-zero.
- [x] 2.3 Log the sync mode at startup (`enabled; base=...` or `disabled`), without leaking the API key.

## 3. Dirty marking — wire into existing routes

- [x] 3.1 In `app/routers/admin.py` (drama create / patch / poster / translation endpoints) call `db.mark_drama_dirty(slug)` after each successful mutation. Drama-create handler sets the row's `sync_status='dirty'` initially.
- [x] 3.2 In tag / actor / language CRUD: after every state-affecting mutation that changes what would be sent to prod, call the appropriate `cascade_dirty_dramas_via_*` helper. Skip on language `is_active` toggles unless they affect drama-side behavior (decision: subtitle labels travel with sync; toggling `is_active=false` does not change the prior label, so no cascade for `is_active` changes alone — but `display_label` change does cascade).
- [x] 3.3 In the new episode upload endpoints (`POST /admin/dramas/{slug}/episodes`, `/admin/dramas/{slug}/episodes/{ep}`): freshly-created or re-uploaded episodes are inserted with `sync_status='dirty'` (via the existing upsert helper, which now sets the column).
- [x] 3.4 In cover upload (`POST /api/episodes/{slug}/{ep}/cover`): call `db.mark_episode_dirty(slug, ep)`.
- [x] 3.5 In subtitle upload / replace / delete: call `db.mark_episode_dirty(slug, ep)`.
- [x] 3.6 In `db.upsert_pending` (called by upload handlers): preserve `last_synced_at` across re-uploads (we don't lose history); set `sync_status='dirty'`.

## 4. HTTP client to business server

- [x] 4.1 Add `httpx` to `requirements.txt`.
- [x] 4.2 Create `app/sync_client.py` with a module-level `httpx.AsyncClient` initialized in lifespan startup with `base_url=settings.business_sync_base_url`, default timeout, and headers `X-API-Key`. Closed in lifespan shutdown.
- [x] 4.3 Helper `async def call_business(method, path, *, json=None) -> dict`: wraps the client, raises `SyncError(status_code, body_excerpt)` on non-2xx, returns parsed JSON on success.
- [x] 4.4 Define `class SyncError(Exception)` carrying `status_code` and a truncated body (~512 chars).

## 5. Sync worker

- [x] 5.1 Create `app/sync.py` with module-level `sync_queue: asyncio.Queue` and dataclasses `SyncDramaJob(slug)` / `SyncEpisodeJob(slug, ep_number)`.
- [x] 5.2 `build_drama_payload(slug)`: assemble the `POST /sync/dramas` body as in the spec. Compose `languages` list from translations + tags + actors + (transitively via episode subtitles) used languages.
- [x] 5.3 `build_episode_payload(slug, ep, playlists)`: assemble the `POST /sync/episodes` body. Compose absolute staging URLs for cover and subtitles using `BUSINESS_SYNC_STAGING_BASE_URL`-or-`PUBLIC_BASE_URL` (clarify a third config var or reuse `business_sync_base_url`'s sibling — see open question; tasks deferred until decision). Pull `key_base64` and `iv_hex` from the row.
- [x] 5.4 `async def handle_drama_sync(slug)`: implements the design's drama flow (read row → branch on pending_delete vs upsert → on success enqueue child episode jobs).
- [x] 5.5 `async def handle_episode_sync(slug, ep)`: implements the episode flow (preflight drama-synced check → branch on pending_delete vs upsert → calls `publish_ladder_to_prod` for each ladder → POST → finalize).
- [x] 5.6 `async def sync_worker_loop()`: long-running coroutine consuming `sync_queue`. Per-job exception handling (already-set status). Spawned in lifespan startup; cancelled in shutdown.
- [x] 5.7 Idempotency: re-running a sync for the same row from `clean` is a no-op (action endpoint already short-circuits, but defense-in-depth in the worker).

## 6. Sync action endpoints

- [x] 6.1 New router `app/routers/sync.py`. `POST /admin/dramas/{slug}/sync`: 503 if sync not configured; 404 if drama missing; 200 (no-op) if `sync_status='clean'` and no child needs sync; otherwise transition `syncing` (or preserve `pending_delete`) and enqueue. Respond 202 with the row.
- [x] 6.2 `POST /admin/episodes/{slug}/{ep}/sync`: 503 / 404 / 200 / 409 (drama never synced) / 202 transitions per the spec.
- [x] 6.3 `GET /admin/sync`: HTML page listing every non-clean drama / episode. Extends the shared base template.
- [x] 6.4 `GET /admin/sync/summary`: lightweight JSON `{"non_clean_count": N}` for the nav-bar polling.
- [x] 6.5 Mount the router in `app/main.py`.

## 7. Two-phase delete branching

- [x] 7.1 In `admin_delete_episode`: read `last_synced_at`. If NULL, existing physical-delete branch (now also doing staging OSS cleanup per step 5). If non-NULL, set `sync_status='pending_delete'`, do local + staging OSS cleanup, return `{"ok": true, ..., "pending_sync": true}`.
- [x] 7.2 In `admin_delete_drama`: same posture for drama. NEVER physically delete on the synced branch — that happens in the sync worker after a successful `DELETE /sync/dramas/{slug}`.

## 8. Aggregate read shape

- [x] 8.1 `GET /admin/dramas/{slug}/full` (defined in `admin-redesign`): include `sync_status`, `sync_error`, `last_synced_at` per drama and per episode element.
- [x] 8.2 `GET /admin/episodes` (admin list): include `sync_status` per row.
- [x] 8.3 Homepage card data (`db.list_dramas_for_homepage`): include count of `non_clean` items per drama for an indicator badge on the card.

## 9. UI integration

- [x] 9.1 Add CSS classes for sync badges in `app/static/admin.css`: `.sync-badge.clean`, `.sync-badge.dirty`, `.sync-badge.syncing` (with a small CSS spinner), `.sync-badge.failed`, `.sync-badge.pending-delete`.
- [x] 9.2 Drama detail template: render the drama's sync badge in the header. Wire "[同步整部剧]" button to POST + transition + poll. Handle 503 / 409 in the JS.
- [x] 9.3 Drama detail's episodes table: render per-row sync badges. Wire per-row "[同步本集]" button. Filter out `pending_delete` from the visible list.
- [x] 9.4 Episode detail template: render the episode's sync badge in the metadata header. Wire "[同步本集]" button. Disable with proper labels in the three blocked cases (clean / drama-never-synced / sync-disabled).
- [x] 9.5 Homepage cards: add a small badge in the corner showing `非 clean: N` per drama (suppressed when 0).
- [x] 9.6 Nav-bar `<div id="sync-zone">`: render the `需同步: N` link, polled via `GET /admin/sync/summary` every 5 seconds. Hide entirely when sync is disabled.
- [x] 9.7 Create `templates/sync.html`: the overview page listing every non-clean drama + episode with action buttons. "[同步全部]" button at top.

## 10. Documentation

- [x] 10.1 Update `CLAUDE.md`'s "Management server" section: document the sync env vars (`BUSINESS_SYNC_BASE_URL`, `BUSINESS_SYNC_API_KEY`, `BUSINESS_SYNC_TIMEOUT`).
- [x] 10.2 Add a new section "业务服务器同步" describing: the state machine, the manual-trigger UX, the dirty cascade rules, and the wire protocol summary.
- [x] 10.3 Document the business server contract: the four `/sync/*` endpoint shapes (proposal links to `business-server-sync` capability spec for full schemas).
- [x] 10.4 Document the deployment posture: HLS server needs network access to business server; both share the OSS bucket; CORS rules unchanged.

## 11. Manual verification

- [ ] 11.1 Set `BUSINESS_SYNC_BASE_URL=http://127.0.0.1:9999` (a placeholder mock) and `BUSINESS_SYNC_API_KEY=test`. Stand up a tiny mock business server (Python script) that returns 200 for `POST /sync/dramas` and `/sync/episodes`, and stores received payloads to disk for inspection.
- [ ] 11.2 Create a drama, upload an episode, add a subtitle. All rows appear `dirty`. `/admin/sync` overview lists them.
- [ ] 11.3 Click "[同步整部剧]" — drama transitions through `syncing` → `clean`; child episode is enqueued and transitions through `syncing` → `clean`. Mock server's stored payload matches the spec's drama and episode JSON shapes (poster_url present, m3u8 strings present with `Drama/prod/...`, key_base64 present).
- [ ] 11.4 Verify OSS prod prefix has the copied init/segment objects.
- [ ] 11.5 Edit the drama synopsis. Drama row goes `dirty`; episodes stay `clean`. Click sync — only drama goes through the loop.
- [ ] 11.6 Rename a tag's translation. Verify the dependent drama is now `dirty`. Independent dramas remain `clean`.
- [ ] 11.7 Delete an episode that's been synced. Row stays as `pending_delete`; local files gone. Click sync on the drama (or episode) — DELETE call goes out to mock; on 204, row is physically removed. Verify OSS prod prefix for that episode is gone.
- [ ] 11.8 Stop the mock server; click sync — drama enters `sync_failed` with the error visible in the UI. Restart the mock; click sync again — succeeds.
- [ ] 11.9 Crash the HLS server while a sync is in progress. Restart. Verify the row's `sync_status` was reaped to `sync_failed` with the orphan-by-restart message.

## 12. Spec sync

- [x] 12.1 `openspec validate business-server-sync --strict`.
- [x] 12.2 If all prior changes are archived, re-validate to ensure the four MODIFIED references resolve.
