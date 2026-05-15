## Context

By the time this change starts implementation, the staging server is a complete drama-management editor with OSS path separation in place. The business server is undefined: it doesn't exist yet, and we're free to design its API. This change closes the loop between staging and prod and is the last item on the original 7-feature roadmap.

Operator UX target: the operator edits everything on staging, plays back via the embedded hls.js to verify, then explicitly clicks "同步" on either the whole drama or one episode. They watch the badge transition from yellow (dirty) → blue (syncing) → green (clean). On failure the badge is red and clicking it shows the error message; they fix and retry.

## Goals / Non-Goals

**Goals:**
- A small, explicit state machine that operators can read at a glance.
- Drama-level and episode-level dirty bits decouple, matching the user's mental model ("I edited the synopsis, but episode 3's video and subtitles haven't changed").
- Cascade dirty for library edits: changing a tag's name marks every drama using it dirty, so the operator's "what needs syncing" view is correct without manual bookkeeping.
- Two-phase delete that's safe: synced content can't be silently lost; the operator must explicitly sync the deletion.
- A single shared API key, set via env var, enforced at every `/sync/*` request.
- A wire protocol that's clean enough to drop onto a business server without surprises (HTTP, JSON, predictable status codes).
- A background sync worker so the HTTP request returns immediately and the operator UI polls for completion.

**Non-Goals:**
- No retry logic with exponential backoff. Failures land in `sync_failed`; the operator clicks "重试" (which is "同步" again). Adding scheduled retry is a future change if needed.
- No partial-sync recovery beyond restart-time `syncing → sync_failed` flip.
- No "sync queue" UI showing job order. Single FIFO worker; depth visible via the overview page.
- No sync of tags / actors / languages as standalone entities. They go inline with each drama sync. The library admin pages don't have sync buttons.
- No incremental delta sync (e.g. "only push the synopsis, not the whole drama"). Each sync sends the full state for that entity. Idempotent overwrites on the business server side.
- No conflict resolution between multiple staging servers. Single-staging-server architecture.

## Decisions

### Decision: `sync_status` is its own column, not derived

Status as a column means: read once, render once, no joins. Mutations explicitly write the column. The alternative (derive from `updated_at` vs `last_synced_at`) loses the `syncing` / `sync_failed` / `pending_delete` distinctions that the operator needs to see.

### Decision: dirty bits decoupled between drama and episode

Editing the drama row dirties the drama only. Uploading an episode dirties the episode only. This matches the wire protocol: drama and episode are separate `/sync/*` calls. It also matches operator intent: "I want to push my new synopsis without re-pushing all 50 episodes' subtitles." Independence falls out of "each entity has its own dirty bit."

### Decision: library cascade

When a tag's name (or actor's name, or language's display label) changes, every drama transitively referencing it must be re-pushed so the prod copy stays consistent. Implementation: each library mutation runs a single UPDATE:

```sql
UPDATE dramas SET sync_status='dirty', updated_at=now()
 WHERE slug IN (SELECT drama_slug FROM drama_tags WHERE tag_slug=?)
   AND sync_status != 'pending_delete';   -- pending_delete stays pending_delete
```

Mirrors for actors and (transitively via subtitles) languages. The cascade is **only when** the change actually affects what gets sent to prod — so a tag's `created_at` update doesn't cascade, but a translation upsert does.

### Decision: episode sync needs the drama to have been synced at least once

Without that precondition, the business server's `POST /sync/episodes` would have to handle "create-the-drama-implicitly," which complicates the protocol and obscures errors. Better: the HLS handler returns 409 if `drama.last_synced_at IS NULL`, with a body suggesting "同步整部剧 first."

After the drama has been synced once, individual episode syncs work even when `drama.sync_status='dirty'`. The drama metadata stays dirty (correct), the episode goes clean, the operator can sync the drama later. Independence holds.

### Decision: two-phase delete with `pending_delete`

The third path of the state machine. Two semantics:
- Never synced (`last_synced_at IS NULL`): operator delete = physical delete. Local cleanup, staging OSS cleanup, row gone. Same as today's behavior post `oss-staging-prod-separation`.
- Synced (`last_synced_at IS NOT NULL`): operator delete = mark `pending_delete`, do local + staging OSS cleanup, **keep the row**. The row stays so a subsequent sync can call `DELETE /sync/episodes/{slug}/{ep}` (or drama variant) on the business server. After a successful delete-sync, the row is physically removed and prod OSS objects are unpublished.

A `pending_delete` row is invisible to most read paths (admin episode list filters it out, drama detail's episodes table hides it) but appears on the sync overview page so the operator knows there's outstanding work.

### Decision: single sync worker, one job at a time, FIFO

The pipeline worker already has this exact shape. Sync gets its own queue and worker for the same reasons: predictable order, easy reasoning, no parallel race conditions on the same drama. Worker loop:

```
async def sync_worker_loop():
    while True:
        job = await sync_queue.get()
        try:
            if isinstance(job, SyncDramaJob):
                await handle_drama_sync(job.slug)
            elif isinstance(job, SyncEpisodeJob):
                await handle_episode_sync(job.slug, job.ep_number)
        except Exception:
            log.exception("sync worker error")
            # status updates already happened inside handle_*_sync
        finally:
            sync_queue.task_done()
```

Single worker handles both job types. Drama-sync internally enqueues episode-sync jobs for each dirty / pending_delete child. Operator's "[同步整部剧]" thus expands into "drama job + N episode jobs" — visible as transitions in the overview page.

### Decision: `POST /sync/dramas` payload shape

```json
{
  "slug": "ly",
  "default_lang": "zh-rCN",
  "client_updated_at": "2026-05-06T12:34:56Z",
  "translations": {
    "zh-rCN": {
      "name": "琅琊榜",
      "synopsis": "...",
      "poster_url": "https://staging.internal/videos/ly/poster/zh-rCN.jpg"
    },
    "en": {
      "name": "Langya Bang",
      "synopsis": null,
      "poster_url": null
    }
  },
  "tags": [
    {
      "slug": "urban",
      "default_lang": "zh-rCN",
      "translations": {"zh-rCN": "都市", "en": "Urban"}
    }
  ],
  "actors": [
    {
      "slug": "zhang-san",
      "default_lang": "zh-rCN",
      "translations": {"zh-rCN": "张三", "en": "Zhang San"}
    }
  ],
  "languages": [
    {"code": "zh-rCN", "display_label": "简体中文"},
    {"code": "en", "display_label": "English"}
  ]
}
```

The `languages` array contains every language code referenced by the drama's translations + tags + actors + (transitively) episode subtitles. Inline payload means the business server can `INSERT … ON CONFLICT … UPDATE` without prior dependencies.

`client_updated_at` is the HLS-side `dramas.updated_at` at the moment the payload was built. The business server stores it; if a stale payload arrives (older than what's already there), the business server SHALL reject with 409 to prevent out-of-order overwrites. (Single-worker FIFO on HLS side makes this rare but defensive.)

The business server, on receipt:
1. Validates `X-API-Key`.
2. Pulls every `poster_url` synchronously; on any pull failure → 502 with the failing URL named.
3. Upserts language rows (idempotent on `code`).
4. Upserts tag rows + tag translations.
5. Upserts actor rows + actor translations.
6. Upserts the drama row + drama translations.
7. Stores poster bytes at its own filesystem under `<biz_OUT_DIR>/{slug}/poster/{lang}.{ext}`.
8. Returns 200 with `{"ok": true, "client_updated_at": "...", "synced_at": "..."}`.

### Decision: `POST /sync/episodes` payload shape

```json
{
  "drama_slug": "ly",
  "ep_number": 3,
  "episode_id": "ly-ep-3",
  "client_updated_at": "2026-05-06T12:34:56Z",
  "duration_ms": 150000,
  "width": 720,
  "height": 1280,
  "drm": {
    "key_uri": "/drm/ly/ep-3/key",
    "key_base64": "QUJDREVGR0hJSktMTU5PUA==",
    "iv_hex": "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
  },
  "playlists": {
    "540p": "<full m3u8 text with prod URLs>",
    "720p": "<full m3u8 text with prod URLs>",
    "1080p": "<full m3u8 text with prod URLs>"
  },
  "cover_url": "https://staging.internal/videos/ly/ep-3/cover.jpg",
  "subtitles": [
    {
      "lang_code": "en",
      "label": "English",
      "url": "https://staging.internal/videos/ly/ep-3/subtitles/en.vtt"
    }
  ]
}
```

Business server, on receipt:
1. Validates `X-API-Key`.
2. Validates that drama `slug` exists in its DB; if not → 409 ("drama not synced first"). HLS-side preflight should prevent this.
3. Pulls `cover_url` and every subtitle `url` synchronously. Any failure → 502.
4. Writes the AES key (`key_base64` decoded → 16 bytes) to `<biz_OUT_DIR>/{slug}/keys/ep-{n}.key`.
5. For each ladder, writes `<biz_OUT_DIR>/{slug}/ep-{n}/{ladder}/media-{ladder}.m3u8` from `playlists[ladder]`. (The init.mp4 + .m4s are already in `Drama/prod/...` OSS courtesy of the HLS-side `publish_ladder_to_prod` call.)
6. Writes cover bytes to `<biz_OUT_DIR>/{slug}/ep-{n}/cover.jpg`.
7. For each subtitle, writes bytes to `<biz_OUT_DIR>/{slug}/ep-{n}/subtitles/{lang}.vtt`.
8. Upserts the episode row.
9. Returns 200 with `{"ok": true, "client_updated_at": "...", "synced_at": "..."}`.

### Decision: HLS-side handle_drama_sync flow

```python
async def handle_drama_sync(slug):
    drama = db.get_drama(slug)
    if drama['sync_status'] == 'pending_delete':
        return await _execute_drama_delete_sync(slug)

    db.set_drama_sync_status(slug, 'syncing')

    try:
        payload = build_drama_payload(slug)   # joins translations, tags, actors, languages
        # pull staging poster URLs into payload as URLs that the business server will fetch
        await call_business('POST', '/sync/dramas', json=payload)
        db.set_drama_sync_status(slug, 'clean', last_synced_at=now())
    except SyncError as e:
        db.set_drama_sync_status(slug, 'sync_failed', error=str(e))
        return                                 # don't proceed to episodes if drama failed

    # then enqueue episode syncs / delete-syncs for every dirty / pending_delete child
    for ep_n in db.list_episodes_needing_sync(slug):
        sync_queue.put_nowait(SyncEpisodeJob(slug, ep_n))
```

Episode syncs queued during a drama-sync are enqueued (not awaited) — they run sequentially after the drama job completes. The operator UI shows them transitioning one by one.

### Decision: HLS-side handle_episode_sync flow

```python
async def handle_episode_sync(slug, ep_n):
    episode = db.get_by_slug_ep(slug, ep_n)
    drama = db.get_drama(slug)
    if drama['last_synced_at'] is None:
        db.set_episode_sync_status(slug, ep_n, 'sync_failed',
                                   error='drama not synced; sync drama first')
        return

    if episode['sync_status'] == 'pending_delete':
        return await _execute_episode_delete_sync(slug, ep_n)

    db.set_episode_sync_status(slug, ep_n, 'syncing')

    try:
        # 1. publish each ladder to prod (OSS server-side copy + return prod m3u8)
        playlists = {}
        for ladder in ('540p', '720p', '1080p'):
            playlists[ladder] = await asyncio.to_thread(publish.publish_ladder_to_prod,
                                                       slug, f"ep-{ep_n}", ladder)

        # 2. build payload, including absolute staging URLs for cover + subtitles
        payload = build_episode_payload(slug, ep_n, playlists=playlists)

        # 3. POST to business server
        await call_business('POST', '/sync/episodes', json=payload)
        db.set_episode_sync_status(slug, ep_n, 'clean', last_synced_at=now())
    except SyncError as e:
        db.set_episode_sync_status(slug, ep_n, 'sync_failed', error=str(e))
```

### Decision: HLS-side delete-sync flow

```python
async def _execute_episode_delete_sync(slug, ep_n):
    db.set_episode_sync_status(slug, ep_n, 'syncing')
    try:
        await call_business('DELETE', f'/sync/episodes/{slug}/{ep_n}')
        # then clean up prod OSS objects (idempotent)
        for ladder in ('540p', '720p', '1080p'):
            await asyncio.to_thread(publish.unpublish_ladder_from_prod,
                                    slug, f"ep-{ep_n}", ladder)
        # finally drop the row
        db.delete_episode_row(slug, ep_n)   # actually deletes now
    except SyncError as e:
        db.set_episode_sync_status(slug, ep_n, 'sync_failed', error=str(e))
```

Drama-delete-sync mirrors with `unpublish_drama_from_prod` and `db.delete_drama_row` (the row removal that drama-as-entity gated behind 0-episode count).

### Decision: HLS HTTP client uses `httpx.AsyncClient`

Single shared client with timeout (default 30 s, configurable via `BUSINESS_SYNC_TIMEOUT`). Auth header injected on every call. Non-2xx → raise `SyncError` with status code + truncated body in the message. Idempotency on the business server side means retries are safe; the worker doesn't auto-retry on transient errors but the operator can click "同步" again after `sync_failed`.

### Decision: Configuration via env vars

| var | required | default | purpose |
|---|---|---|---|
| `BUSINESS_SYNC_BASE_URL` | no | none | When unset, sync UI is hidden / disabled and `POST /admin/dramas/{slug}/sync` returns 503. When set, must be `https://...` (no trailing slash). |
| `BUSINESS_SYNC_API_KEY` | required iff base URL set | none | Sent as `X-API-Key` header. Server fails fast at startup if `BUSINESS_SYNC_BASE_URL` is set without a matching key. |
| `BUSINESS_SYNC_TIMEOUT` | no | `30` | HTTP timeout in seconds for individual `/sync/*` calls. |

Sync is opt-in: a deploy without `BUSINESS_SYNC_BASE_URL` runs as a pure staging editor with no sync surface.

### Decision: `GET /admin/sync` overview

A single page listing:
- Every drama with `sync_status` ∈ {`dirty`, `syncing`, `sync_failed`, `pending_delete`}, with action buttons.
- Every episode with `sync_status` ∈ same set, with action buttons.
- Drama-level "[同步全部]" button that enqueues a sync for each dirty drama.
- Per-row "重试" / "查看错误" / "取消挂起删除" actions where applicable.

Provides at-a-glance triage. The nav bar's `<div id="sync-zone">` shows a small `需同步: N` link to this page (where N is the count of non-clean things).

## Risks / Trade-offs

- **Risk: m3u8 references the AES key as `/drm/{slug}/ep-{n}/key`, which on the business server must also be reachable.** The business server's `/drm/` endpoint is its responsibility; the HLS-defined wire protocol provides `key_base64` so the business server can write the file and serve it. Documented in the `POST /sync/episodes` payload contract.
- **Risk: large drama with many episodes ⇒ long sync time.** Background worker plus per-episode polling renders gracefully, but the operator might walk away for 10 minutes on a 50-episode drama. Trade-off accepted; if it bites, future change can parallelize with bounded concurrency.
- **Risk: the business server might be unreachable; sync fails repeatedly.** Operator sees `sync_failed` badge and a clear error. No silent degradation; no auto-retry.
- **Risk: synced rows can't be physically deleted without successful delete-sync.** If the business server is permanently dead, the operator might have rows stuck in `pending_delete`. → Future "force-delete" admin action that skips the sync; out of scope for this change.
- **Risk: dirty cascade on tag/actor/language change can mark many dramas dirty.** Acceptable: the operator made a library-wide change, and they should review what's affected. The overview page surfaces it; bulk-sync is one click.
- **Trade-off: cover / subtitle / poster pulled by business server from staging URLs.** Requires the business server to have network access to the staging server. Reasonable on internal VPN. Alternative (push as multipart bytes) is heavier and was rejected for protocol simplicity.
- **Trade-off: `client_updated_at` for ordering, no other concurrency control.** Single-staging architecture means writes don't interleave; FIFO worker means no in-flight sync collisions on the same entity. Sufficient.

## Migration Plan

1. Stop server.
2. Delete `hls.db` (sync_status columns are added to the schema; recreate from scratch — consistent with prior changes' destructive posture).
3. Configure env vars (`BUSINESS_SYNC_BASE_URL`, `BUSINESS_SYNC_API_KEY`) — or leave unset to disable sync.
4. Deploy new HLS code; `init_db()` creates the schema with sync columns. All existing dramas/episodes start as `dirty`.
5. Implement / deploy the business server matching the `/sync/*` contract and exposing `/api/*` for SDK traffic. (Separate codebase.)
6. Operator opens `/admin/sync` overview, clicks "[同步全部]", watches transitions complete.

Rollback: revert HLS code; delete `hls.db`. Sync columns disappear with the schema. Any rows in `pending_delete` are irrecoverable from local state alone (the staging OSS objects are gone) — operator would re-upload from source files. This is the worst rollback case; mitigated by the fact that the rollback decision is made before launch.

## Open Questions

- Should `POST /sync/dramas` and `POST /sync/episodes` be `PUT` instead, since they're idempotent upserts? Either works; sticking with POST for body-required RESTful upsert convention.
- Should the business server's `/api/*` (SDK-facing) be specced in this change too, or in a follow-up? **Yes, here**: a small "business server SDK API mirrors HLS server's `/api/*` shape with `?lang=` resolution" requirement, since locale resolution is the missing feature. Detailed in specs.
- Should sync logs be persisted (last 100 sync events, failures) for an audit trail? Useful but additional table + UI. Out of scope for v1; structured logs only.
