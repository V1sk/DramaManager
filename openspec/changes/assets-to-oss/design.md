## Context

After step 5 (`oss-staging-prod-separation`) and step 6 (`business-server-sync`), the OSS bucket is the source of truth for **video** assets — staging is what this server writes, prod is what the business server reads, and the sync flow `publish_ladder_to_prod` does an OSS server-side copy to bridge them.

But three other asset families never made the jump:

- `cover.jpg` (per episode, jpeg)
- `poster/{lang}.{ext}` (per drama × language, jpeg / png / webp)
- `subtitles/{lang}.vtt` (per episode × language, WebVTT)

These three still live only on local disk under `OUT_DIR/{slug}/...`, served via the `/videos/` static mount. The `business-server-sync` change papered over this with a "URL pull" contract: HLS sends the relative `/videos/...` path in the sync payload, and the business server fetches it over HTTP from the HLS host.

That contract has known weaknesses (network coupling, 502 failure path in protocol, CDN unfriendly, role smearing). This change unifies all asset families under the same staging→prod-copy pattern that videos already use.

The decision is largely **mechanical**: extend the existing helpers (`publish_ladder_to_prod`, `unpublish_*_from_*`) to additional prefixes, and wire upload-time OSS PUT into the existing handlers. No new external dependency, no new architectural pattern, just consistent application of the established one.

## Goals / Non-Goals

**Goals:**
- Single source of truth for all video-pipeline assets: OSS bucket. Local disk under `OUT_DIR/` becomes a write-through cache (still useful for debugging / `/videos/` static serve / reupload).
- Sync protocol simplifies: payload's `cover_url` / `poster_url` / `subtitles[].url` are absolute prod OSS URLs; business server records them verbatim with no fetch.
- Removes the `502 (failed to pull URL)` branch from the `/sync/*` protocol — the only protocol-level "fetch this thing" instruction is gone.
- Symmetry with video segments — operators / future contributors don't need to remember which assets are local-only and which are OSS.
- Business server can run on a public-internet box without inbound network access to the HLS server (HLS server stays VPN-only).
- All four `unpublish_*_from_*` helpers continue to work without modification — they delete by **prefix**, so `Drama/staging/{slug}/poster/` etc. is naturally swept along.

**Non-Goals:**
- HLS-side `/api/*` endpoint URL form: keeps current relative paths. These endpoints are debug / preview only; not on the SDK hot path.
- DRM key files (`.key` / `.iv` / `.key.b64`): MUST stay local. Exposing the AES key bytes via a public OSS URL is a hard security violation. The `key_base64` in the `/sync/episodes` payload is fine because that payload is HTTPS + API-key-gated.
- m3u8 files: do NOT live on OSS at all. The staging copy is local (rendered by `publish_ladder` after segment upload); the prod copy is shipped inline as a string in the `/sync/episodes` payload and stored on the business server's filesystem.
- CDN config / lifecycle policies: out of scope. Existing CORS rule covers the new asset types since they're all GET-from-bucket.
- Bucket / credential split between environments: still single bucket, single AK pair, prefix-only separation.

## Decisions

### Decision: extend `publish.py` helpers per-asset-type instead of a generic `publish_path` API

We add per-asset helpers that each know their on-disk source path, OSS staging key, and OSS prod key:

```python
def upload_poster_to_staging(slug: str, lang: str, local_path: Path) -> str:
    """Upload OUT_DIR/{slug}/poster/{lang}.{ext} → Drama/staging/{slug}/poster/{lang}.{ext}.
    Returns the public staging URL. Called from POST /admin/dramas/{slug}/poster.
    """

def publish_poster_to_prod(slug: str, lang: str, ext: str) -> str:
    """OSS server-side copy: Drama/staging/{slug}/poster/{lang}.{ext}
    → Drama/prod/{slug}/poster/{lang}.{ext}. Returns prod URL. Called from sync."""

def unpublish_poster_from_staging(slug: str, lang: str) -> None:
    """Delete every Drama/staging/{slug}/poster/{lang}.* (any extension).
    Used by DELETE /admin/dramas/{slug}/poster?lang=... to keep staging clean."""

def unpublish_poster_from_prod(slug: str, lang: str) -> None:
    """Same shape, prod side. Used by sync delete flow."""
```

…and analogous helpers for cover and subtitle.

**Alternative considered:** A single generic `publish_object(staging_key, prod_key) -> str`. Rejected because:
- The on-disk → OSS-key mapping is per-asset-type and slightly tricky (poster has variable extension; subtitle has lang code; cover is fixed name). Embedding that logic in 3 typed helpers reads better than 1 generic helper called from 6 different places with 6 different key formulas.
- Prefix-based unpublish is per-asset-type already (the prefix is what makes idempotent delete work) — pairing each `publish` helper with its own `unpublish` keeps the cardinality 1:1.

**Note**: the drama-level / episode-level / ladder-level **prefix** unpublish helpers from step 5 (`unpublish_drama_from_staging`, `unpublish_drama_from_prod`, `unpublish_episode_from_staging`, `unpublish_ladder_from_prod`) **do not need changes** — they list-and-delete by prefix, so any new objects under those prefixes get swept automatically. We add the per-asset helpers only for the *partial-cleanup* paths: deleting one specific poster language, deleting one specific subtitle language, replacing one cover.

### Decision: upload to OSS happens **synchronously** inside the request handler

When the operator POSTs a poster / cover / subtitle, the handler:
1. Validates the upload (MIME, magic bytes for vtt, etc.)
2. Writes to `OUT_DIR/{slug}/...` (existing behavior)
3. Uploads to OSS staging via `bucket.put_object_from_file` (`asyncio.to_thread`-wrapped; `oss2` is sync)
4. On OSS failure: the handler can either (a) roll back the local write and return 500, or (b) keep the local write and return 200 with a warning + dirty marker.

We choose **(a) roll back + return 500** for partial-failure cleanliness. Rationale:
- Operators expect "200 = my upload landed" semantics. Half-landing (local OK, OSS fail) is confusing and leaks into sync-time errors that are harder to diagnose.
- OSS uploads are normally fast (the asset files are small: a few MB for posters at most, KB for subtitles). The retry loop on the operator side ("click upload again") is fine.
- The transactional code is simple: if `bucket.put_object_from_file` raises, `local_path.unlink(missing_ok=True)` and re-raise as HTTP 500.

**Alternative considered:** Asynchronous upload via a background queue (similar to the pipeline worker). Rejected because:
- It would require a new queue, new state column ("upload_pending"), and a new sync precondition ("can't sync until all assets are in OSS"). Significant complexity.
- Operators expect the post-upload page (e.g. /admin/dramas/{slug}) to immediately reflect the new poster. Async upload makes "I uploaded but the OSS copy isn't there yet" a user-visible issue.

For the **pipeline-time cover extraction** (worker grabs first frame after FFmpeg → writes to OUT_DIR), we add the OSS upload in the worker right after `extract_first_frame`, before enqueueing the pipeline job. Failure here marks the episode `failed` with a useful error.

### Decision: sync flow does staging→prod copy of these assets BEFORE the HTTP call

In `handle_drama_sync`:

```python
async def handle_drama_sync(slug):
    ...
    # collect all poster langs that have files
    poster_translations = ...  # [(lang, prod_url)]
    for lang, ext in posters_with_ext:
        prod_url = await asyncio.to_thread(publish.publish_poster_to_prod, slug, lang, ext)
    payload = build_drama_payload(slug, poster_prod_urls=...)  # uses absolute prod URLs
    await call_business('POST', '/sync/dramas', json=payload)
    ...
```

In `handle_episode_sync`:

```python
async def handle_episode_sync(slug, ep):
    ...
    cover_prod_url = await asyncio.to_thread(publish.publish_cover_to_prod, slug, ep_dir)
    subtitle_prod_urls = []
    for lang in ...:
        subtitle_prod_urls.append(await asyncio.to_thread(publish.publish_subtitle_to_prod, slug, ep_dir, lang))
    # ladder copies: as before
    playlists = {...}
    payload = build_episode_payload(..., cover_url=cover_prod_url, subtitles=...)
    await call_business('POST', '/sync/episodes', json=payload)
```

If any `publish_*_to_prod` raises `PublishError` (e.g. staging object missing), the sync row goes `sync_failed` with the error; the call to the business server is **not** made.

**Alternative considered:** Inline in `build_*_payload`. Rejected because `build_*_payload` is currently a pure function (DB read → dict). Mixing OSS I/O into it would make it harder to reason about. Keeping the I/O in `handle_*_sync` and passing the URLs into `build_*_payload` as parameters preserves that separation.

### Decision: payload schema — fields keep their names, semantics change

Renaming `poster_url` → `poster_oss_url` etc. would be cleaner but requires a coordinated business-server rollout. We keep the field names and **change the semantics**: the value is now an absolute `https://photobundle.oss-...` URL instead of a relative `/videos/...` path.

Business server impact: previously they parsed the path, joined with `HLS_STAGING_HOST`, and HTTP GET'd. Now they treat the value as opaque (record verbatim, return verbatim to SDK clients). Strictly simpler.

The integration doc (`docs/business-server-integration.md`) MUST be updated to reflect this. The §5 "URL 拉取 contract" section is deleted; the §4 wire-protocol tables get the field semantic change called out.

### Decision: drama / episode delete still cascades correctly

Existing `unpublish_drama_from_staging(slug)` / `unpublish_episode_from_staging(slug, ep_dir)` / `unpublish_drama_from_prod(slug)` / `unpublish_ladder_from_prod(slug, ep_dir, ladder)` work by listing keys under a prefix and batch-deleting. New asset prefixes (`Drama/staging/{slug}/poster/`, `Drama/staging/{slug}/{ep_dir}/cover.jpg`, `Drama/staging/{slug}/{ep_dir}/subtitles/`) all sit under the existing drama / episode prefix tree, so the existing helpers naturally sweep them on whole-entity delete. No code change to those helpers.

For **partial** delete (one poster language, one subtitle language, one cover replace) we add typed helpers as listed above.

### Decision: ladder-level vs episode-level prod cleanup on delete

`unpublish_ladder_from_prod(slug, ep_dir, ladder)` is currently called by the sync worker on episode delete-sync, three times (one per ladder). It does NOT touch the `cover.jpg` or `subtitles/` siblings under `Drama/prod/{slug}/{ep_dir}/`. We need a fix — but which?

**Option A**: Extend `unpublish_ladder_from_prod` to also clean cover and subtitles "the third time it's called". Brittle.

**Option B**: Add `unpublish_episode_from_prod(slug, ep_dir)` (mirror of staging) that prefix-deletes everything under `Drama/prod/{slug}/{ep_dir}/`. Sync worker calls this **once** per delete, replacing the 3× ladder calls. Cleaner.

We choose **Option B**. Worker delete loop becomes:

```python
async def _execute_episode_delete_sync(slug, ep):
    # business server DELETE first
    await sync_client.call_business('DELETE', f'/sync/episodes/{slug}/{ep}')
    # then prod OSS, single sweep
    await asyncio.to_thread(publish.unpublish_episode_from_prod, slug, f"ep-{ep}")
    db.physical_delete_episode(slug, ep)
```

`unpublish_drama_from_prod` already does the right thing for drama-level delete (prefix `Drama/prod/{slug}/`).

### Decision: backfill script, no data migration

Existing dramas / episodes that were uploaded before this change have posters / covers / subtitles only locally. We provide `scripts/backfill_assets_to_oss.py`:

```python
# walk OUT_DIR, upload every cover.jpg / poster/*.* / subtitles/*.vtt to staging OSS
# idempotent (overwrites).
# DOES NOT touch DB.
# Operator runs once after deploy.
```

After backfill, the next sync of each drama / episode will server-side-copy the assets from staging to prod and update the business server.

We do not gate "must run backfill before deploy" — operators can also re-upload through the admin UI for a fresh start. The script is a convenience.

### Decision: poster file extension preservation

Currently `_remove_existing_poster_files(drama_slug, lang_code)` glob-deletes any extension before writing the new one. The OSS object key needs to preserve the same extension (so the URL matches the local file path). Decision flow:

1. POST `/admin/dramas/{slug}/poster?lang=xx` with `image/jpeg` body
2. Local: write `OUT_DIR/{slug}/poster/xx.jpg`, glob-delete `xx.png` / `xx.webp` if any
3. OSS staging: `unpublish_poster_from_staging(slug, "xx")` (deletes any extension under prefix `Drama/staging/{slug}/poster/xx.*`)
4. OSS staging: `bucket.put_object_from_file("Drama/staging/{slug}/poster/xx.jpg", local_path)`

The "delete-by-prefix-glob" pattern keeps OSS in sync with local: only one `xx.{ext}` lives per (slug, lang) at a time. The translation row in DB still stores the relative URL (`/videos/{slug}/poster/xx.jpg`) as before.

## Risks / Trade-offs

- **Risk: OSS upload during `POST /admin/.../poster` is slow → operator UX regression.**
  → Mitigation: posters are typically <1 MB. OSS PUT latency from HLS server (Singapore region) is ~200ms p50. The handler was already not instant (image MIME check + filesystem write); adding ~200ms is acceptable. If this becomes an issue, we can move to async background upload (see "alternative considered" above).
- **Risk: Local file written but OSS PUT fails → user retries and gets duplicate behavior.**
  → Mitigation: handler unwinds the local write on OSS failure. Retry from the operator goes through the same code path; fresh attempt.
- **Risk: backfill script uploads stale files (e.g. orphan cover files from deleted episodes).**
  → Mitigation: script is documented to walk only paths that have a corresponding DB row. It SHALL filter `OUT_DIR/{slug}/{ep_dir}/cover.jpg` against `episodes (drama_slug, ep_number)` rows; same for posters against `translations` rows; same for subtitles against `subtitles` rows.
- **Risk: Mid-sync interruption between asset publish-to-prod and `/sync/episodes` POST → orphan prod objects.**
  → Mitigation: subsequent sync re-runs `publish_*_to_prod` (idempotent — server-side copy overwrites). Failed sync row stays `sync_failed`; operator retries; orphan objects are eventually overwritten on success or cleaned by full-drama delete.
- **Risk: Business server already deployed with URL-pull logic — existing deploys break on first sync after this change.**
  → Mitigation: BREAKING change called out in proposal. Business server team must update their `/sync/*` handlers to **not** fetch (treat URLs as opaque). Coordinate deploy. As a transition aid, the integration doc gets an explicit "version bump" note.
- **Trade-off: extra OSS PUT per poster/cover/subtitle save.** Edit-heavy flows now do (local write + OSS PUT) instead of (local write only). Each save is a few hundred ms slower. Acceptable for correctness gain.
- **Trade-off: `EpisodeInfo`-style URLs from the business server are now public OSS URLs.** Anyone with the URL can hit OSS directly. Mitigated by the fact that the URLs are unguessable + bucket has CORS but no auth: same posture as the existing video segments. If this is unacceptable, a follow-up change can move to signed-URL distribution.

## Migration Plan

1. **Stop HLS server.**
2. Pull this change. `git pull` etc.
3. Restart HLS server. Existing local files still serve via `/videos/` mount; no immediate breakage.
4. Run `OSS_ENABLED=true ./venv/bin/python scripts/backfill_assets_to_oss.py`. This walks every `cover.jpg`, every `poster/{lang}.{ext}`, every `subtitles/{lang}.vtt` under `OUT_DIR/{slug}/...`, filters by DB-row existence, and uploads to staging. Idempotent.
5. Coordinate with business-server team:
   - Deploy their updated `/sync/*` handlers (no longer fetch URLs; treat as opaque).
   - They must clear their existing local poster / cover / subtitle files? No — they'll be overwritten on next sync. Or they can keep them; the `EpisodeInfo` URL just changes to point at OSS.
6. From admin UI, click "[同步整部剧]" on each drama. Sync worker uses the new `publish_*_to_prod` flow; business server records the OSS URLs.
7. Verify in OSS console: `Drama/prod/{slug}/poster/...`, `Drama/prod/{slug}/{ep_dir}/cover.jpg`, `Drama/prod/{slug}/{ep_dir}/subtitles/...` exist.
8. Verify SDK client can fetch the new prod URLs (CORS, network).

**Rollback:** revert HLS code; the business server will keep its old URL-pull logic. Local files in `OUT_DIR/...` are untouched. OSS staging objects from this change can be left (operator can clean by hand) or removed by a separate `unpublish_drama_from_staging` call. No data loss either direction; only the URL form on the business server's `EpisodeInfo` differs.

## Open Questions

- **Should we also publish posters / covers to prod when operators edit them, even before they click "sync"?** Answer (default): no. Stay symmetric with video segments — staging is what this server writes; prod is purely a sync output. Editing a poster sets the drama row dirty; the next sync click does the staging→prod copy. This keeps the "manual review before publish" UX promise intact.
- **`EpisodeInfo` from the HLS-side `/api/*` endpoints (debug / local-preview) — does it switch to OSS staging URLs for poster / cover / subtitle?** Tentative answer: yes, eventually, via a follow-up "everything goes through OSS staging URL on this server too" change. For this change, leave HLS-side `/api/*` returning relative paths (debug). The cost of updating it now is small but it's not on any user's critical path.
- **Does this change touch the `episode-info-schema.json`?** No. The schema's URL fields are already `format: uri-reference`, accepting both relative and absolute. No schema change needed; only the URL form changes for clients hitting the business server.
