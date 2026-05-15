## Context

The current OSS layout is single-tenant: encoder writes, SDK reads, same path. With manual review-before-sync as the new operator UX, the staging server's writes must not mutate what the prod SDK serves. The narrowest fix that preserves the existing single-bucket / single-credentials posture is a path-prefix split inside the bucket.

OSS server-side copy within the same bucket is free, instantaneous, and atomic per-object. Listing under a prefix is paginated; bulk delete supports up to 1000 keys per call. These primitives are sufficient to implement publish/unpublish without holding bytes locally.

The m3u8 written by `publish_ladder` after re-encoding ends up with absolute staging URLs. Generating the prod-flavored m3u8 at sync time is then a localized string substitution — no re-rewriting from scratch, no risk of drift.

## Goals / Non-Goals

**Goals:**
- All encoder output lands under `Drama/staging/{slug}/...` in OSS.
- Operators reviewing on staging see staging-flavored URLs in m3u8 and `EpisodeInfo`.
- A primitive (`publish_ladder_to_prod`) that copies staging→prod and returns a prod-flavored m3u8 — the only piece step 6 needs to publish a single ladder.
- Symmetric primitives for delete sync (`unpublish_ladder_from_prod`, `unpublish_drama_from_prod`).
- Hygiene: deleting an episode/drama on staging cleans up its staging OSS objects, eliminating the orphan-accumulation gap noted in CLAUDE.md.
- A migration helper for any operator who has test data under the legacy single-tenant layout.

**Non-Goals:**
- No bucket split. Staging and prod share the same bucket; only the path prefix differs.
- No per-environment credentials. The hardcoded `accessKeyId` / `accessKeySecret` in `oss_upload.py` cover both prefixes.
- No business-server hand-off. That's step 6. This change exposes primitives; step 6 calls them.
- No CORS changes. Existing rules apply uniformly.
- No CDN, no signed URLs. OSS objects remain publicly readable per the existing CORS rule.
- No "republish unchanged content" optimization (skipping staging→prod copy when the staging object is identical). Adds complexity for marginal benefit; OSS server-side copy is already cheap.

## Decisions

### Decision: prefix layout

```
Bucket: photobundle
  Drama/staging/
    {slug}/
      {ep_dir}/
        {ladder}/
          init-{ladder}.mp4
          seg-{ladder}-*.m4s
  Drama/prod/
    {slug}/
      {ep_dir}/
        {ladder}/
          init-{ladder}.mp4
          seg-{ladder}-*.m4s
```

Both subtrees are mirror-shape under the same bucket. Sync = copy within the bucket. Independent CORS / lifecycle policies are not introduced (could be added later if business asks for shorter staging retention).

### Decision: constants in `app/oss_upload.py`

```python
ossBaseDir = "Drama"                                              # unchanged
oss_public_base_url = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"  # unchanged: bucket-level base

OSS_STAGING_PREFIX = f"{ossBaseDir}/staging"                      # NEW: "Drama/staging"
OSS_PROD_PREFIX    = f"{ossBaseDir}/prod"                         # NEW: "Drama/prod"

oss_staging_public_base_url = f"{oss_public_base_url}/staging"    # NEW: full URL
oss_prod_public_base_url    = f"{oss_public_base_url}/prod"       # NEW: full URL
```

`oss_public_base_url` is preserved (no rename) so the existing `oss-segment-hosting` requirement "MUST 暴露 `oss_public_base_url`" still holds. Its role narrows: it's the bucket-level base, used only inside `oss_upload.py` to derive staging/prod children. **External callers MUST use `oss_staging_public_base_url` or `oss_prod_public_base_url` directly**, never the bucket-level constant; this is enforced by review, not by code.

### Decision: `publish_ladder` writes to staging prefix and rewrites m3u8 with staging URL

```python
remote_dir = f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/{ladder}"     # "Drama/staging/{slug}/..."
# ... uploads ...
rewrite_playlist(text, f"{oss_staging_public_base_url}/{slug}/{ep_dir}/{ladder}")
```

The function signature is unchanged. Its OSS path output and m3u8 content change.

### Decision: `publish_ladder_to_prod` is a server-side copy + m3u8 string-swap

```python
def publish_ladder_to_prod(slug: str, ep_dir: str, ladder: str) -> str:
    """Copy Drama/staging/{slug}/{ep_dir}/{ladder}/ → Drama/prod/{slug}/{ep_dir}/{ladder}/
    via OSS server-side copy. Returns the prod-flavored m3u8 text (caller ships it 
    to the business server). Idempotent — re-runs overwrite prod objects.
    """
    src_dir = f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/{ladder}"
    dst_dir = f"{OSS_PROD_PREFIX}/{slug}/{ep_dir}/{ladder}"

    # 1. enumerate every staging key under the ladder dir, copy each
    src_keys = oss_upload.list_with_prefix(src_dir + "/")
    if not src_keys:
        raise PublishError(f"no staging objects under {src_dir}/; was publish_ladder ever called?")
    for src_key in src_keys:
        if not src_key.endswith((".mp4", ".m4s")):
            continue                                              # defensive — only media
        filename = src_key.rsplit("/", 1)[-1]
        oss_upload.copy_object(src_key, f"{dst_dir}/{filename}")

    # 2. read local m3u8, swap base URL
    local_m3u8 = settings.out_dir / slug / ep_dir / ladder / f"media-{ladder}.m3u8"
    if not local_m3u8.is_file():
        raise PublishError(f"missing local playlist: {local_m3u8}")
    text = local_m3u8.read_text()
    return text.replace(oss_staging_public_base_url + "/", oss_prod_public_base_url + "/")
```

The string-replace is unambiguous: `oss_staging_public_base_url` is `https://photobundle.../Drama/staging`, which only appears in init/segment lines after `publish_ladder`. `#EXT-X-KEY:URI="/drm/..."` is relative and never matches the search string.

**Alternative considered:** parsing the m3u8 with `rewrite_playlist` and a new "swap base" function. Rejected because the string-replace is shorter, exhibits the same idempotency property (running it twice on prod-flavored text is a no-op since the staging string isn't there), and matches the m3u8 structure exactly.

### Decision: unpublish primitives operate per-ladder and per-drama

```python
def unpublish_ladder_from_prod(slug, ep_dir, ladder) -> None:
    keys = oss_upload.list_with_prefix(f"{OSS_PROD_PREFIX}/{slug}/{ep_dir}/{ladder}/")
    oss_upload.batch_delete(keys)        # tolerates empty list

def unpublish_drama_from_prod(slug) -> None:
    keys = oss_upload.list_with_prefix(f"{OSS_PROD_PREFIX}/{slug}/")
    oss_upload.batch_delete(keys)
```

The drama-level helper is convenient for step 6's "delete drama → sync delete" flow: one OSS call sequence, one HTTP DELETE to the business server.

Mirror staging-side helpers:
```python
def unpublish_ladder_from_staging(slug, ep_dir, ladder)
def unpublish_episode_from_staging(slug, ep_dir)              # NEW: convenience for episode delete
def unpublish_drama_from_staging(slug)                         # NEW: convenience for drama delete
```

### Decision: staging cleanup is wired into the existing delete handlers

`DELETE /admin/episodes/{slug}/{ep}` already removes local files. After this change, when `settings.oss_enabled`:
- It additionally calls `unpublish_episode_from_staging(slug, ep_dir)` after the local rmtree.
- Failures are logged + surfaced in the existing `warnings` array, **not** rolled back. (OSS-vs-local consistency is a known eventual-consistency posture; staging objects are non-authoritative.)

`DELETE /admin/dramas/{slug}` (added by `drama-as-entity`) similarly calls `unpublish_drama_from_staging(slug)` after the local rmtree.

`unpublish_*_from_prod` is **not** called from delete handlers. Prod cleanup is gated by step 6's manual sync action — operators delete on staging, then click "同步删除," which triggers the prod-side unpublish + business server DELETE.

### Decision: `_row_to_episode_info` uses `oss_staging_public_base_url`

```python
if settings.oss_enabled:
    media_base = f"{oss_staging_public_base_url}/{slug}/{ep_dir}"   # was oss_public_base_url
else:
    media_base = base
```

This server is staging; SDK clients hitting **this** API see staging URLs. The business server in step 6 will compose `EpisodeInfo` with `oss_prod_public_base_url`, but that's their problem.

### Decision: OSS primitives in `oss_upload.py`

```python
def copy_object(src_key: str, dst_key: str) -> None:
    """Server-side copy within the same bucket. Raises on failure."""
    bucket.copy_object(bucket.bucket_name, src_key, dst_key)

def list_with_prefix(prefix: str) -> list[str]:
    """Paginated list of all object keys under a prefix. Returns full key strings."""
    keys = []
    marker = ""
    while True:
        result = bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
        keys.extend(o.key for o in result.object_list)
        if not result.is_truncated:
            break
        marker = result.next_marker
    return keys

def batch_delete(keys: list[str]) -> None:
    """Delete up to 1000 keys per OSS call; chunks if more. No-op on empty list."""
    if not keys:
        return
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        bucket.batch_delete_objects(chunk)
```

These primitives are private helpers used by `publish.py`. They are not invoked directly from routers.

### Decision: migration helper

`scripts/migrate_to_staging_prefix.py`:
- For each `status=ready` row in DB:
  - For each ladder (540p / 720p / 1080p):
    - Read local m3u8.
    - If it still references `oss_public_base_url + "/{slug}/"` (the legacy single-tenant path), re-run `publish_ladder` (which uploads to staging + rewrites m3u8 with staging URL).
- Idempotent: subsequent runs find no legacy references and exit.

This is for any operator with data from earlier OSS test runs. The fresh-start path is "delete `hls.db` + delete bucket contents under `Drama/`, redeploy" — same as prior changes.

## Risks / Trade-offs

- **Risk: copy step succeeds but local m3u8 read fails after.** → Caller's `PublishError` causes the sync to fail, the prod copies remain orphaned. Acceptable: subsequent retries (or `unpublish_ladder_from_prod`) clean up. Not worth a transactional protocol.
- **Risk: `publish_ladder_to_prod` is called before `publish_ladder` ever ran (no staging objects).** → Function raises `PublishError` with a clear message naming the missing prefix. Step 6's caller surfaces this.
- **Risk: lifecycle policies could differ between staging and prod (e.g. shorter retention for staging).** → Out of scope. Adding a bucket lifecycle rule keyed on the staging prefix is a one-liner if/when storage cost matters.
- **Risk: `replace(staging, prod)` collides if a slug literally contained `/staging/`.** → Slug regex `^[a-z0-9][a-z0-9-]*$` forbids slashes; ladder names (`540p`, `720p`, `1080p`) are slash-free; filenames (`init-720p.mp4`, `seg-720p-N.m4s`) are slash-free. Collision impossible.
- **Risk: staging cleanup on episode delete leaves OSS in an inconsistent state if local cleanup partially fails.** → Already handled: warnings array surfaces both local and OSS issues; operator can re-run delete.
- **Trade-off: prod m3u8 is regenerated from local staging m3u8 every sync.** No prod m3u8 is stored anywhere on this server. Step 6 ships the text via HTTP body to the business server. → Correct: staging is the source of truth; prod m3u8 has no independent existence here.

## Migration Plan

Two paths:

**Fresh deploy (recommended; no data exists):**
1. Stop server.
2. Manually delete any existing OSS objects under `Drama/{slug}/` (legacy layout). The bucket SDK / web console can bulk delete.
3. Delete `hls.db` and `OUT_DIR` contents.
4. Deploy new code.
5. Operators re-create dramas / re-upload episodes through the admin UI; new uploads land under `Drama/staging/`.

**Preserve test data:**
1. Stop server.
2. Deploy new code.
3. Run `OSS_ENABLED=true ./venv/bin/python scripts/migrate_to_staging_prefix.py`.
4. Confirm the migration log: each ready episode is re-uploaded to staging; local m3u8 references `Drama/staging/`.
5. Optionally delete the legacy `Drama/{slug}/` objects manually after spot-checking.

Rollback: revert code; legacy m3u8 references will fail to play (file moved to staging/), so rollback ships the old code AND restores the bucket layout. Practical rollback path is "redeploy old code; run a reverse migration." For this change, the simpler answer is "don't roll back — fix forward."

## Open Questions

- Should `unpublish_*_from_prod` be exposed as helpers from `publish.py` even though no caller invokes them in this change? **Yes** — step 6 needs them, and including them now lets step 5 ship with full primitive coverage and integration tests against an OSS test bucket.
- Should `publish_ladder` stay synchronous or become async? Currently the worker calls `await asyncio.to_thread(publish_ladder, ...)`. Same wrapper applies to `publish_ladder_to_prod`. Async-first refactor is unrelated to this change.
- Should the migration helper auto-delete legacy `Drama/{slug}/` objects after copying? **No** — too destructive without operator review. The helper logs them as "candidates for manual cleanup."
