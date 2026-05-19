"""Sync worker: pushes drama / episode state from this staging server to the
business server.

State machine (per drama and per episode row):
    dirty   ──► syncing ──► clean
                    │
                    └──► sync_failed
    pending_delete ──► syncing ──► (row physically gone)
                            │
                            └──► sync_failed (still pending_delete intent)

The worker is a single asyncio.Queue consumer: one job at a time, FIFO. Drama
jobs internally enqueue child episode jobs after the drama's own POST succeeds.

Lifespan integration:
  - `app.main` creates `asyncio.create_task(sync_worker_loop())` at startup,
    after `sync_client.startup()` initializes the HTTP client.
  - On shutdown the task is cancelled and `sync_client.shutdown()` closes the
    HTTP client.

`pending_delete` rows: the operator's `DELETE /admin/...` already cleaned up
local files + staging OSS. The worker's job is to call the business server's
DELETE endpoint, then delete prod OSS objects, then physically remove the row.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, publish, sync_client
from .config import settings
from .oss_upload import oss_staging_public_base_url
from .sync_client import SyncError

log = logging.getLogger("hls.sync")


@dataclass
class SyncDramaJob:
    slug: str


@dataclass
class SyncEpisodeJob:
    slug: str
    ep_number: int


_queue: asyncio.Queue | None = None


def get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def enqueue_drama(slug: str) -> None:
    await get_queue().put(SyncDramaJob(slug=slug))


async def enqueue_episode(slug: str, ep_number: int) -> None:
    await get_queue().put(SyncEpisodeJob(slug=slug, ep_number=ep_number))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _abs_staging_url(relative_path: str) -> str:
    """Convert a `/videos/...` path into an absolute URL the business server
    can pull from. Uses the OSS staging base when OSS is enabled; falls back
    to a generic placeholder when OSS is off (the business server pulling from
    `/videos/...` directly is a deployment-time concern documented in CLAUDE.md).
    """
    return relative_path  # business server resolves against staging host


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_drama_payload(
    slug: str,
    *,
    poster_prod_urls: dict[str, str] | None = None,
) -> dict:
    """Assemble the `POST /sync/dramas` body for this drama.

    `client_updated_at` is the drama row's `updated_at` at build time — the
    business server uses it for ordering / idempotency.

    `poster_prod_urls` is `{lang_code: prod_oss_url}` (assets-to-oss). When OSS
    sync is in effect, `handle_drama_sync` calls `publish_poster_to_prod` for
    each lang first and passes the result here; the payload's
    `translations[lang].poster_url` field is the prod OSS URL. Languages
    without a prod URL (e.g. no poster file uploaded) keep `poster_url=null`.
    OSS-disabled deploys pass `None`, in which case we fall back to shipping
    the relative `/videos/...` path stored on the translation row (legacy
    "URL pull" semantics that the business server is expected to honor).
    """
    drama = db.get_drama_with_sync(slug)
    if drama is None:
        raise RuntimeError(f"build_drama_payload: drama '{slug}' not found")

    poster_prod_urls = poster_prod_urls or {}

    # Per-language: name / synopsis / poster_url.
    # poster_url is the prod OSS URL when assets-to-oss is in effect; otherwise
    # falls back to the relative `/videos/...` path stored locally.
    translations: dict[str, dict] = {}
    for lang_code, fields in db.list_drama_translations(slug).items():
        if lang_code in poster_prod_urls:
            poster_url = poster_prod_urls[lang_code]
        else:
            poster_url = fields.get("poster")  # null or relative /videos/... path
        translations[lang_code] = {
            "name": fields.get("name"),
            "synopsis": fields.get("synopsis"),
            "poster_url": poster_url,
        }

    # Tags (with all their translation rows)
    tags_payload: list[dict] = []
    drama_tag_rows = db.list_drama_tags(slug)  # [{slug, label}] (default-lang only)
    for tag_row in drama_tag_rows:
        tag_slug = tag_row["slug"]
        # Use list_tags() to find this tag's default_lang; alternative is a
        # direct query, but reusing the helper keeps one source of truth.
        tag_meta = next(
            (t for t in db.list_tags() if t["slug"] == tag_slug), None,
        )
        if tag_meta is None:
            continue  # defensive — junction row outlived its tag (CASCADE prevents this)
        tags_payload.append({
            "slug": tag_slug,
            "default_lang": tag_meta["default_lang"],
            "translations": db.list_translations_for_entity("tag", tag_slug, "label"),
        })

    actors_payload: list[dict] = []
    drama_actor_rows = db.list_drama_actors(slug)
    for actor_row in drama_actor_rows:
        actor_slug = actor_row["slug"]
        actor_meta = next(
            (a for a in db.list_actors() if a["slug"] == actor_slug), None,
        )
        if actor_meta is None:
            continue
        actors_payload.append({
            "slug": actor_slug,
            "default_lang": actor_meta["default_lang"],
            "translations": db.list_translations_for_entity("actor", actor_slug, "name"),
        })

    # Languages: union of every code referenced (transitively) by the drama
    used_codes = db.list_languages_used_by_drama(slug)
    languages_payload: list[dict] = []
    for code in used_codes:
        lang = db.get_language(code)
        if lang is not None:
            languages_payload.append({
                "code": code,
                "display_label": lang["display_label"],
            })

    return {
        "slug": slug,
        "default_lang": drama["default_lang"],
        # 业务字段：免费集数 (0 = 全部付费; N = 前 N 集免费, 第 N+1 集起收费)。
        # 业务服务器决定付费墙时直接读这个字段。
        "free_episodes": drama.get("free_episodes", 3),
        "client_updated_at": drama["updated_at"],
        "translations": translations,
        "tags": tags_payload,
        "actors": actors_payload,
        "languages": languages_payload,
    }


def build_episode_payload(
    slug: str,
    ep_number: int,
    playlists: dict[str, str],
    *,
    cover_prod_url: str | None = None,
    subtitles_prod: list[dict] | None = None,
) -> dict:
    """Assemble the `POST /sync/episodes` body. `playlists` is
    `{ladder: prod_m3u8_text}` produced by `publish.publish_ladder_to_prod`.

    `cover_prod_url` and `subtitles_prod` (assets-to-oss): when OSS sync is in
    effect, `handle_episode_sync` calls `publish_cover_to_prod` /
    `publish_subtitle_to_prod` first and passes the resulting absolute prod
    URLs here. Falls back to the local `/videos/...` paths when None
    (OSS-disabled deploys; legacy URL-pull semantics).
    """
    row = db.get_by_slug_ep(slug, ep_number)
    if row is None:
        raise RuntimeError(
            f"build_episode_payload: episode {slug}/{ep_number} not found"
        )
    if row["status"] != "ready":
        raise RuntimeError(
            f"build_episode_payload: episode {slug}/{ep_number} status={row['status']!r}; "
            f"only 'ready' rows can be synced"
        )

    if subtitles_prod is not None:
        subtitles_payload = subtitles_prod
    else:
        sub_rows = db.list_subtitles_for_slug_ep(slug, ep_number)
        subtitles_payload = [
            {
                "lang_code": s["lang_code"],
                "label": s["label"],
                "url": s["file_url"],
            }
            for s in sub_rows
        ]

    cover_url = cover_prod_url if cover_prod_url is not None else row["cover_url"]

    return {
        "drama_slug": slug,
        "ep_number": ep_number,
        "episode_id": row["episode_id"],
        "client_updated_at": row["updated_at"],
        "duration_ms": row["duration_ms"],
        "width": row.get("width"),
        "height": row.get("height"),
        "drm": {
            "key_uri": row["key_uri"],
            "key_base64": row["key_b64"],
            "iv_hex": row["iv_hex"],
        },
        "playlists": playlists,
        "cover_url": cover_url,
        "subtitles": subtitles_payload,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _execute_drama_delete_sync(slug: str) -> None:
    """Push DELETE /sync/dramas/{slug} to the business server, then unpublish
    prod OSS objects, then physically remove the local drama row.
    """
    db.set_drama_sync_status(slug, "syncing")
    try:
        await sync_client.call_business("DELETE", f"/sync/dramas/{slug}")
    except SyncError as e:
        log.warning("drama delete-sync failed slug=%s: %s", slug, e)
        db.set_drama_sync_status(slug, "sync_failed", error=str(e))
        # Keep state as pending_delete? No — the row stays in DB; sync_status
        # is sync_failed; operator retries via the sync UI. We do NOT revert
        # to pending_delete because the worker just attempted the call.
        # However the *intent* is delete; mark with a clear error and leave
        # sync_status='pending_delete' so the next sync click retries delete.
        db.set_drama_sync_status(slug, "pending_delete", error=str(e))
        return
    except Exception as e:  # noqa: BLE001 — network / unexpected
        log.exception("drama delete-sync unexpected slug=%s", slug)
        db.set_drama_sync_status(slug, "pending_delete", error=f"unexpected: {e}")
        return

    if settings.oss_enabled:
        try:
            await asyncio.to_thread(publish.unpublish_drama_from_prod, slug)
        except Exception as e:  # noqa: BLE001
            log.warning("unpublish_drama_from_prod failed slug=%s: %s", slug, e)
            # Don't block — orphan prod objects are recoverable; surface as warn.
            db.set_drama_sync_status(
                slug, "sync_failed",
                error=f"business DELETE ok but prod OSS cleanup failed: {e}",
            )
            return

    try:
        db.physical_delete_drama(slug)
    except Exception:
        log.exception("physical_delete_drama failed slug=%s", slug)
        db.set_drama_sync_status(
            slug, "sync_failed",
            error="business DELETE + OSS cleanup ok but DB row delete failed",
        )
        return
    log.info("drama delete-sync complete slug=%s", slug)


async def _execute_episode_delete_sync(slug: str, ep_number: int) -> None:
    db.set_episode_sync_status(slug, ep_number, "syncing")
    try:
        await sync_client.call_business(
            "DELETE", f"/sync/episodes/{slug}/{ep_number}",
        )
    except SyncError as e:
        log.warning(
            "episode delete-sync failed slug=%s ep=%s: %s", slug, ep_number, e,
        )
        # Reset to pending_delete with the error so the operator can retry.
        db.set_episode_sync_status(slug, ep_number, "pending_delete", error=str(e))
        return
    except Exception as e:  # noqa: BLE001
        log.exception("episode delete-sync unexpected slug=%s ep=%s", slug, ep_number)
        db.set_episode_sync_status(
            slug, ep_number, "pending_delete", error=f"unexpected: {e}",
        )
        return

    if settings.oss_enabled:
        ep_dir = f"ep-{ep_number}"
        # assets-to-oss: single prefix sweep covers all ladders + cover + subtitles.
        try:
            await asyncio.to_thread(
                publish.unpublish_episode_from_prod, slug, ep_dir,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "unpublish_episode_from_prod failed slug=%s ep=%s: %s",
                slug, ep_number, e,
            )
            db.set_episode_sync_status(
                slug, ep_number, "sync_failed",
                error=f"business DELETE ok but prod OSS cleanup failed: {e}",
            )
            return

    try:
        db.physical_delete_episode(slug, ep_number)
    except Exception:
        log.exception("physical_delete_episode failed slug=%s ep=%s", slug, ep_number)
        db.set_episode_sync_status(
            slug, ep_number, "sync_failed",
            error="business DELETE + OSS cleanup ok but DB row delete failed",
        )
        return
    log.info("episode delete-sync complete slug=%s ep=%s", slug, ep_number)


async def handle_drama_sync(slug: str) -> None:
    drama = db.get_drama_with_sync(slug)
    if drama is None:
        log.warning("handle_drama_sync: drama %s vanished before processing", slug)
        return

    if drama["sync_status"] == "pending_delete":
        await _execute_drama_delete_sync(slug)
        return
    if drama["sync_status"] == "clean":
        # Idempotency defense-in-depth: maybe the job was enqueued before the
        # endpoint short-circuit, or another job cleaned the row first.
        log.debug("handle_drama_sync: %s already clean; skipping", slug)
        return

    db.set_drama_sync_status(slug, "syncing")
    try:
        # assets-to-oss: copy each per-language poster staging→prod first; the
        # returned absolute prod URLs go into the payload. PublishError here
        # (e.g. staging object missing because never uploaded) → sync_failed
        # without calling the business server.
        poster_prod_urls: dict[str, str] = {}
        if settings.oss_enabled:
            translations_view = db.list_drama_translations(slug)
            for lang_code, fields in translations_view.items():
                rel_url = fields.get("poster")
                if not rel_url:
                    continue
                # rel_url is like "/videos/{slug}/poster/{lang}.{ext}";
                # extract the extension off the tail.
                tail = rel_url.rsplit("/", 1)[-1]   # "{lang}.{ext}"
                if "." not in tail:
                    log.warning(
                        "skipping malformed poster rel_url for sync: %s", rel_url,
                    )
                    continue
                ext = tail.rsplit(".", 1)[-1]
                prod_url = await asyncio.to_thread(
                    publish.publish_poster_to_prod, slug, lang_code, ext,
                )
                poster_prod_urls[lang_code] = prod_url

        payload = build_drama_payload(slug, poster_prod_urls=poster_prod_urls)
        await sync_client.call_business("POST", "/sync/dramas", json=payload)
        db.set_drama_sync_status(slug, "clean", last_synced_at=_now_iso())
        log.info("drama sync ok slug=%s", slug)
    except publish.PublishError as e:
        log.warning("drama sync poster publish failed slug=%s: %s", slug, e)
        db.set_drama_sync_status(slug, "sync_failed", error=f"publish_poster_to_prod: {e}")
        return
    except SyncError as e:
        log.warning("drama sync failed slug=%s: %s", slug, e)
        db.set_drama_sync_status(slug, "sync_failed", error=str(e))
        return
    except Exception as e:  # noqa: BLE001
        log.exception("drama sync unexpected slug=%s", slug)
        db.set_drama_sync_status(slug, "sync_failed", error=f"unexpected: {e}")
        return

    # Drama POST succeeded → enqueue child episode jobs.
    children = db.list_episodes_needing_sync(slug)
    for ep_n in children:
        await get_queue().put(SyncEpisodeJob(slug=slug, ep_number=ep_n))
    if children:
        log.info(
            "drama sync slug=%s queued %d child episode job(s)", slug, len(children),
        )


async def handle_episode_sync(slug: str, ep_number: int) -> None:
    drama = db.get_drama_with_sync(slug)
    if drama is None:
        log.warning(
            "handle_episode_sync: drama %s vanished before processing ep=%s",
            slug, ep_number,
        )
        return
    if drama["last_synced_at"] is None:
        # Drama never synced → episode sync is invalid. Mark sync_failed.
        db.set_episode_sync_status(
            slug, ep_number, "sync_failed",
            error="drama not synced; sync drama first",
        )
        return

    row = db.get_by_slug_ep(slug, ep_number)
    if row is None:
        log.warning(
            "handle_episode_sync: episode %s/%s vanished before processing",
            slug, ep_number,
        )
        return
    if row["sync_status"] == "pending_delete":
        await _execute_episode_delete_sync(slug, ep_number)
        return
    if row["sync_status"] == "clean":
        log.debug(
            "handle_episode_sync: %s/%s already clean; skipping", slug, ep_number,
        )
        return
    if row["status"] != "ready":
        db.set_episode_sync_status(
            slug, ep_number, "sync_failed",
            error=f"episode pipeline status={row['status']!r}; expected 'ready'",
        )
        return

    db.set_episode_sync_status(slug, ep_number, "syncing")
    try:
        ep_dir = f"ep-{ep_number}"
        playlists: dict[str, str] = {}
        cover_prod_url: str | None = None
        subtitles_prod: list[dict] | None = None
        if settings.oss_enabled:
            # Ladders first.
            for ladder in ("540p", "720p", "1080p"):
                playlists[ladder] = await asyncio.to_thread(
                    publish.publish_ladder_to_prod, slug, ep_dir, ladder,
                )
            # assets-to-oss: cover + subtitles staging→prod.
            cover_prod_url = await asyncio.to_thread(
                publish.publish_cover_to_prod, slug, ep_dir,
            )
            sub_rows = db.list_subtitles_for_slug_ep(slug, ep_number)
            subtitles_prod = []
            for s in sub_rows:
                prod_url = await asyncio.to_thread(
                    publish.publish_subtitle_to_prod, slug, ep_dir, s["lang_code"],
                )
                subtitles_prod.append({
                    "lang_code": s["lang_code"],
                    "label": s["label"],
                    "url": prod_url,
                })
        else:
            # OSS disabled: the local m3u8 is the source of truth; ship it as-is.
            # Cover + subtitle URLs fall back to relative `/videos/...` paths
            # via build_episode_payload's defaults.
            from pathlib import Path
            for ladder in ("540p", "720p", "1080p"):
                pl = (
                    settings.out_dir / slug / ep_dir / ladder
                    / f"media-{ladder}.m3u8"
                )
                if not pl.is_file():
                    raise publish.PublishError(f"missing local playlist: {pl}")
                playlists[ladder] = pl.read_text()

        payload = build_episode_payload(
            slug, ep_number,
            playlists=playlists,
            cover_prod_url=cover_prod_url,
            subtitles_prod=subtitles_prod,
        )
        await sync_client.call_business("POST", "/sync/episodes", json=payload)
        db.set_episode_sync_status(
            slug, ep_number, "clean", last_synced_at=_now_iso(),
        )
        log.info("episode sync ok slug=%s ep=%s", slug, ep_number)
    except SyncError as e:
        log.warning("episode sync failed slug=%s ep=%s: %s", slug, ep_number, e)
        db.set_episode_sync_status(slug, ep_number, "sync_failed", error=str(e))
    except publish.PublishError as e:
        log.warning("episode sync publish failed slug=%s ep=%s: %s", slug, ep_number, e)
        db.set_episode_sync_status(
            slug, ep_number, "sync_failed", error=f"publish_to_prod: {e}",
        )
    except Exception as e:  # noqa: BLE001 — network / SDK / FS unexpected
        log.exception("episode sync unexpected slug=%s ep=%s", slug, ep_number)
        db.set_episode_sync_status(
            slug, ep_number, "sync_failed", error=f"unexpected: {e}",
        )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


async def sync_worker_loop() -> None:
    q = get_queue()
    log.info("sync worker started")
    while True:
        job = await q.get()
        try:
            if isinstance(job, SyncDramaJob):
                await handle_drama_sync(job.slug)
            elif isinstance(job, SyncEpisodeJob):
                await handle_episode_sync(job.slug, job.ep_number)
            else:  # pragma: no cover — defensive
                log.error("sync worker: unknown job type %r", job)
        except Exception:  # noqa: BLE001 — keep worker alive across job-level bugs
            log.exception("unhandled sync worker error on job=%r", job)
        finally:
            q.task_done()
