import asyncio
import logging
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import settings
from ..ffmpeg_utils import (
    FfmpegError,
    extract_first_frame,
    probe_duration_ms,
    probe_video_dimensions,
)
from ..queue import Job, enqueue

router = APIRouter()
log = logging.getLogger("hls.admin")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    """Drama cards homepage. Server-rendered using `db.list_dramas_for_homepage()`."""
    rows = db.list_dramas_for_homepage()
    return _TEMPLATES.TemplateResponse(
        request,
        "home.html",
        {"dramas": rows, "nav_active": "home"},
    )


@router.get("/admin/dramas/new", response_class=HTMLResponse)
async def admin_drama_new_page(request: Request) -> HTMLResponse:
    """Create-drama page (form). Languages / tags / actors are populated
    client-side from /api/* endpoints.
    """
    return _TEMPLATES.TemplateResponse(
        request,
        "drama_new.html",
        {"nav_active": "home"},
    )


@router.get("/admin/dramas/{drama_slug}", response_class=HTMLResponse)
async def admin_drama_detail_page(
    request: Request,
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> HTMLResponse:
    """Drama detail page. Server-renders the page with `db.get_drama_full`."""
    full = db.get_drama_full(drama_slug)
    if full is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "drama_detail.html",
        {"drama": full, "nav_active": "home"},
    )


@router.get("/admin/dramas/{drama_slug}/full")
async def admin_drama_full(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> JSONResponse:
    """Aggregate read endpoint for the drama detail page. Same shape used by
    server-side rendering and any client-side post-edit refresh.
    """
    full = db.get_drama_full(drama_slug)
    if full is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    return JSONResponse(full)


@router.get("/admin/dramas/{drama_slug}/episodes/{ep}", response_class=HTMLResponse)
async def admin_episode_detail_page(
    request: Request,
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
) -> HTMLResponse:
    """Episode detail page with embedded hls.js player + subtitle / cover /
    re-upload / delete affordances.
    """
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    episode = db.get_by_slug_ep(drama_slug, ep_number)
    if episode is None:
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")
    drama = db.get_drama(drama_slug)
    subtitles = db.list_subtitles_for_slug_ep(drama_slug, ep_number)
    return _TEMPLATES.TemplateResponse(
        request,
        "episode_detail.html",
        {
            "drama": drama,
            "episode": episode,
            "subtitles": subtitles,
            "nav_active": "home",
        },
    )


@router.get("/admin/episodes")
async def admin_episodes() -> JSONResponse:
    rows = db.list_all()
    payload = [
        {
            "drama_slug": r["drama_slug"],
            "drama_name": r["drama_name"],
            "ep_number": r["ep_number"],
            "episode_id": r["episode_id"],
            "status": r["status"],
            "duration_ms": r["duration_ms"],
            "play_url": r["play_url"],
            "cover_url": r["cover_url"],
            "error_message": r["error_message"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    return JSONResponse(payload)


@router.get("/admin/dramas")
async def admin_list_dramas() -> JSONResponse:
    rows = db.list_dramas()
    return JSONResponse([
        {
            "slug": r["slug"],
            "name": r["name"],
            "default_lang": r["default_lang"],
            "ep_count": r["ep_count"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ])


@router.post("/admin/dramas")
async def admin_create_drama(
    drama_slug: str = Form(...),
    drama_name: str = Form(...),
    default_lang: str = Form(...),
) -> RedirectResponse:
    try:
        db.create_drama(slug=drama_slug, name=drama_name, default_lang=default_lang)
    except db.DramaValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.DramaExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        # Schema-mismatch fallback raised by db.create_drama when an old
        # hls.db with the legacy `dramas.name` column is still around.
        log.error("schema mismatch on drama insert: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    log.info("created drama slug=%s name=%s default_lang=%s",
             drama_slug, drama_name.strip(), default_lang)
    return RedirectResponse(url="/admin", status_code=302)


@router.delete("/admin/dramas/{drama_slug}")
async def admin_delete_drama(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> JSONResponse:
    row = db.get_drama_with_sync(drama_slug)
    if row is None:
        raise HTTPException(status_code=404, detail="drama not found")

    # Refuse to delete a drama that still has episodes attached.
    with __import__("sqlite3").connect(settings.db_path) as raw:
        ep_count = int(raw.execute(
            "SELECT COUNT(*) FROM episodes WHERE drama_slug=?",
            (drama_slug,),
        ).fetchone()[0])
    if ep_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"drama has {ep_count} episode(s); delete them first",
        )

    drama_dir = settings.out_dir / drama_slug
    warnings: list[str] = []
    if drama_dir.exists():
        try:
            shutil.rmtree(drama_dir)
        except OSError as e:
            log.warning("failed to remove drama dir %s: %s", drama_dir, e)
            warnings.append(str(drama_dir))

    if settings.oss_enabled:
        from .. import publish
        try:
            await asyncio.to_thread(publish.unpublish_drama_from_staging, drama_slug)
        except Exception as e:  # noqa: BLE001 — OSS 失败不阻塞本地删除
            log.warning("failed to unpublish drama %s from staging OSS: %s", drama_slug, e)
            warnings.append(f"oss-staging:{drama_slug}")

    pending_sync = False
    if row["last_synced_at"] is not None:
        # Two-phase delete: keep the row in pending_delete state. The sync
        # worker will call DELETE /sync/dramas/{slug} on the business server,
        # then call physical_delete_drama once the business server confirms.
        db.set_drama_sync_status(drama_slug, "pending_delete")
        pending_sync = True
        log.info(
            "drama marked pending_delete slug=%s warnings=%d (awaiting sync)",
            drama_slug, len(warnings),
        )
    else:
        # Never synced: physical delete is safe.
        deleted, _ = db.delete_drama(drama_slug)
        if not deleted:
            log.warning("delete_drama returned False for slug=%s after row check", drama_slug)
        log.info("deleted drama slug=%s warnings=%d", drama_slug, len(warnings))

    return JSONResponse({"ok": True, "warnings": warnings, "pending_sync": pending_sync})


def _process_episode_upload(
    drama_slug: str,
    ep_number: int,
    video: UploadFile,
) -> Path:
    """Shared upload pipeline used by both the auto-increment and the
    re-upload endpoints. Streams the upload to UPLOAD_TMP_DIR, runs ffprobe
    (duration + dimensions), extracts the cover, and persists the row via
    `upsert_pending`. Returns the temp path for the queue job.

    Raises HTTPException on validation/IO failure (with the temp file already
    cleaned up).
    """
    episode_id = f"{drama_slug}-ep-{ep_number}"
    ep_dir_name = f"ep-{ep_number}"
    episode_dir = settings.out_dir / drama_slug / ep_dir_name

    tmp_path = settings.upload_tmp_dir / f"upload-{uuid.uuid4().hex}.mp4"
    try:
        with tmp_path.open("wb") as out_f:
            shutil.copyfileobj(video.file, out_f, length=1024 * 1024)
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"failed to persist upload: {e}")

    try:
        duration_ms = probe_duration_ms(tmp_path)
        width, height = probe_video_dimensions(tmp_path)
    except FfmpegError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"ffprobe failed: {e}")

    cover_path = episode_dir / "cover.jpg"
    try:
        extract_first_frame(tmp_path, cover_path)
    except FfmpegError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"cover extraction failed: {e}")

    # Mirror cover to OSS staging (assets-to-oss). Failure unwinds the just-
    # extracted cover and the temp upload before raising 500 so we don't
    # leave a half-published asset.
    if settings.oss_enabled:
        from .. import publish
        try:
            publish.upload_cover_to_staging(drama_slug, ep_dir_name, cover_path)
        except publish.PublishError as e:
            log.error("OSS staging upload failed for cover %s/%s: %s", drama_slug, ep_dir_name, e)
            cover_path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"failed to mirror cover to OSS staging: {e}",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("OSS unexpected error for cover %s/%s", drama_slug, ep_dir_name)
            cover_path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"unexpected OSS error mirroring cover: {e}",
            )

    cover_url = f"/videos/{drama_slug}/{ep_dir_name}/cover.jpg"

    db.upsert_pending(
        drama_slug=drama_slug,
        ep_number=ep_number,
        episode_id=episode_id,
        duration_ms=duration_ms,
        cover_url=cover_url,
        source_filename=video.filename or "",
        width=width,
        height=height,
    )
    return tmp_path


def _next_ep_number(drama_slug: str) -> int:
    """Compute MAX(ep_number)+1 for a drama. Connection-fresh each call so it
    reflects committed concurrent inserts."""
    with sqlite3.connect(settings.db_path) as raw:
        row = raw.execute(
            "SELECT COALESCE(MAX(ep_number), 0) + 1 FROM episodes WHERE drama_slug=?",
            (drama_slug,),
        ).fetchone()
    return int(row[0])


@router.post("/admin/dramas/{drama_slug}/episodes")
async def admin_upload_next_episode(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    video: UploadFile = File(...),
) -> RedirectResponse:
    """Upload a new episode with auto-incremented `ep_number`. Concurrent
    uploads colliding on the same number retry up to 3 times; persistent
    collision → 503.
    """
    if db.get_drama(drama_slug) is None:
        await video.close()
        raise HTTPException(
            status_code=404,
            detail=f"drama '{drama_slug}' not found; create it via POST /admin/dramas first",
        )
    if video is None or not video.filename:
        raise HTTPException(status_code=400, detail="video file is required")

    # Read the upload bytes ONCE so each retry can re-stream to a fresh temp file.
    try:
        body = await video.read()
    finally:
        await video.close()
    if not body:
        raise HTTPException(status_code=400, detail="video file is empty")

    last_err: Exception | None = None
    for _ in range(3):
        next_ep = _next_ep_number(drama_slug)
        ep_dir_name = f"ep-{next_ep}"
        episode_id = f"{drama_slug}-ep-{next_ep}"
        episode_dir = settings.out_dir / drama_slug / ep_dir_name

        tmp_path = settings.upload_tmp_dir / f"upload-{uuid.uuid4().hex}.mp4"
        try:
            tmp_path.write_bytes(body)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to persist upload: {e}")

        try:
            duration_ms = probe_duration_ms(tmp_path)
            width, height = probe_video_dimensions(tmp_path)
        except FfmpegError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"ffprobe failed: {e}")

        cover_path = episode_dir / "cover.jpg"
        try:
            extract_first_frame(tmp_path, cover_path)
        except FfmpegError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"cover extraction failed: {e}")

        if settings.oss_enabled:
            from .. import publish
            try:
                publish.upload_cover_to_staging(drama_slug, ep_dir_name, cover_path)
            except Exception as e:  # noqa: BLE001 — PublishError or unexpected
                log.error(
                    "OSS staging upload failed for cover %s/%s: %s",
                    drama_slug, ep_dir_name, e,
                )
                cover_path.unlink(missing_ok=True)
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=500,
                    detail=f"failed to mirror cover to OSS staging: {e}",
                )

        cover_url = f"/videos/{drama_slug}/{ep_dir_name}/cover.jpg"
        try:
            db.upsert_pending(
                drama_slug=drama_slug,
                ep_number=next_ep,
                episode_id=episode_id,
                duration_ms=duration_ms,
                cover_url=cover_url,
                source_filename=video.filename or "",
                width=width,
                height=height,
            )
        except sqlite3.IntegrityError as e:
            tmp_path.unlink(missing_ok=True)
            last_err = e
            continue  # retry with a freshly-computed next_ep

        await enqueue(Job(
            episode_id=episode_id,
            drama_slug=drama_slug,
            ep_number=next_ep,
            tmp_path=tmp_path,
        ))
        log.info("enqueued auto-increment slug=%s ep=%s", drama_slug, episode_id)
        return RedirectResponse(url=f"/admin/dramas/{drama_slug}", status_code=302)

    raise HTTPException(
        status_code=503,
        detail=f"concurrent upload collision after 3 attempts; last error: {last_err}",
    )


_EP_PREFIX_RE = re.compile(r"^EP(\d+)", re.IGNORECASE)


@router.post("/admin/dramas/{drama_slug}/episodes/batch")
async def admin_batch_upload_episodes(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    videos: list[UploadFile] = File(...),
) -> JSONResponse:
    """Batch-upload episodes. Each file's `ep_number` is derived from its
    filename `EP<n>` prefix (case-insensitive). Existing episodes are
    overwritten (re-upload semantics); episodes currently encoding are
    skipped. Returns a per-file result list — partial failure is normal.

    NOTE: declared before the `episodes/{ep}` route — FastAPI matches in
    order, and the literal `batch` segment would otherwise be captured by
    `{ep}` and 422 on the `^[0-9]+$` pattern.
    """
    if db.get_drama(drama_slug) is None:
        for v in videos:
            await v.close()
        raise HTTPException(
            status_code=404,
            detail=f"drama '{drama_slug}' not found; create it via POST /admin/dramas first",
        )

    results: list[dict] = []
    seen_eps: dict[int, str] = {}  # ep_number -> filename, dedupe within the batch

    for video in videos:
        filename = video.filename or ""
        m = _EP_PREFIX_RE.match(filename.strip())
        if not m:
            await video.close()
            results.append({
                "filename": filename, "ep_number": None, "ok": False,
                "detail": "文件名未以 EP<数字> 开头",
            })
            continue
        ep_number = int(m.group(1))
        if ep_number < 1:
            await video.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "ok": False,
                "detail": "集号必须 >= 1",
            })
            continue
        if ep_number in seen_eps:
            await video.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "ok": False,
                "detail": f"集号与本批次文件 '{seen_eps[ep_number]}' 重复",
            })
            continue
        seen_eps[ep_number] = filename

        row = db.get_by_slug_ep(drama_slug, ep_number)
        if row is not None and row["status"] == "encoding":
            await video.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "ok": False,
                "detail": "该集正在编码中，跳过",
            })
            continue

        try:
            tmp_path = _process_episode_upload(drama_slug, ep_number, video)
        except HTTPException as e:
            await video.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "ok": False,
                "detail": str(e.detail),
            })
            continue
        await video.close()

        episode_id = f"{drama_slug}-ep-{ep_number}"
        await enqueue(Job(
            episode_id=episode_id,
            drama_slug=drama_slug,
            ep_number=ep_number,
            tmp_path=tmp_path,
        ))
        log.info("enqueued batch slug=%s ep=%s file=%s", drama_slug, episode_id, filename)
        results.append({
            "filename": filename, "ep_number": ep_number, "ok": True,
            "detail": "已入队",
        })

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({
        "ok_count": ok_count,
        "error_count": len(results) - ok_count,
        "results": results,
    })


@router.post("/admin/dramas/{drama_slug}/episodes/{ep}")
async def admin_reupload_episode(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
    video: UploadFile = File(...),
) -> RedirectResponse:
    """Re-encode an existing episode with a new source file. Requires the
    episode to exist; rejects 409 if currently `status=encoding`.
    """
    ep_number = int(ep)
    if ep_number < 1:
        await video.close()
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    row = db.get_by_slug_ep(drama_slug, ep_number)
    if row is None:
        await video.close()
        raise HTTPException(
            status_code=404,
            detail=f"episode '{drama_slug}/{ep_number}' not found",
        )
    if row["status"] == "encoding":
        await video.close()
        raise HTTPException(
            status_code=409,
            detail="episode is currently encoding; wait for it to finish before re-uploading",
        )
    if video is None or not video.filename:
        raise HTTPException(status_code=400, detail="video file is required")

    tmp_path = _process_episode_upload(drama_slug, ep_number, video)
    await video.close()

    episode_id = f"{drama_slug}-ep-{ep_number}"
    await enqueue(Job(
        episode_id=episode_id,
        drama_slug=drama_slug,
        ep_number=ep_number,
        tmp_path=tmp_path,
    ))
    log.info("enqueued re-upload slug=%s ep=%s", drama_slug, episode_id)
    return RedirectResponse(
        url=f"/admin/dramas/{drama_slug}/episodes/{ep_number}",
        status_code=302,
    )


@router.delete("/admin/episodes/{drama_slug}/{ep}")
async def admin_delete_episode(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
) -> JSONResponse:
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")

    row = db.get_by_slug_ep(drama_slug, ep_number)
    if row is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if row["status"] == "encoding":
        raise HTTPException(
            status_code=409,
            detail="can't delete while encoding; wait until status is ready/failed",
        )

    ep_dir_name = f"ep-{ep_number}"
    ep_dir_path = settings.out_dir / drama_slug / ep_dir_name
    keys_dir_path = settings.out_dir / drama_slug / "keys"
    warnings: list[str] = []

    try:
        shutil.rmtree(ep_dir_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("failed to remove ep dir %s: %s", ep_dir_path, e)
        warnings.append(str(ep_dir_path))

    for ext in ("key", "iv", "key.b64"):
        key_file = keys_dir_path / f"{ep_dir_name}.{ext}"
        try:
            key_file.unlink(missing_ok=True)
        except OSError as e:
            log.warning("failed to remove key file %s: %s", key_file, e)
            warnings.append(str(key_file))

    # Drama directory cleanup is no longer triggered here. Drama row outlives
    # its last episode; cleanup of OUT_DIR/{slug}/ happens only when the drama
    # itself is deleted via DELETE /admin/dramas/{slug}.

    if settings.oss_enabled:
        from .. import publish
        try:
            await asyncio.to_thread(
                publish.unpublish_episode_from_staging, drama_slug, ep_dir_name,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "failed to unpublish episode %s/%s from staging OSS: %s",
                drama_slug, ep_dir_name, e,
            )
            warnings.append(f"oss-staging:{drama_slug}/{ep_dir_name}")

    pending_sync = False
    if row["last_synced_at"] is not None:
        # Two-phase delete: keep the row, mark pending_delete; the sync worker
        # propagates the DELETE to the business server and then physically
        # removes the row.
        db.set_episode_sync_status(drama_slug, ep_number, "pending_delete")
        pending_sync = True
        log.info(
            "episode marked pending_delete slug=%s ep=%s warnings=%d (awaiting sync)",
            drama_slug, row["episode_id"], len(warnings),
        )
    else:
        # Never synced: physical delete is safe.
        db.delete_by_slug_ep(drama_slug, ep_number)
        log.info(
            "deleted slug=%s ep=%s warnings=%d",
            drama_slug, row["episode_id"], len(warnings),
        )

    return JSONResponse(
        {"ok": True, "warnings": warnings, "pending_sync": pending_sync},
    )


# ---------------------------------------------------------------------------
# drama-meta-translations: PATCH default_lang, translation upsert/delete,
# poster upload/delete, full per-lang content listing.
# ---------------------------------------------------------------------------

_LANG_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"

# MIME type → file extension. Keep these in sync with the spec.
_POSTER_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _poster_dir(drama_slug: str) -> Path:
    return settings.out_dir / drama_slug / "poster"


def _poster_url(drama_slug: str, lang_code: str, ext: str) -> str:
    return f"/videos/{drama_slug}/poster/{lang_code}.{ext}"


def _remove_existing_poster_files(drama_slug: str, lang_code: str) -> list[str]:
    """Remove any `OUT_DIR/{drama_slug}/poster/{lang_code}.*` file regardless
    of extension. Returns a list of paths that failed to delete (warnings).
    Tolerates missing files.
    """
    warnings: list[str] = []
    poster_dir = _poster_dir(drama_slug)
    if not poster_dir.is_dir():
        return warnings
    prefix = f"{lang_code}."
    for p in poster_dir.glob(f"{lang_code}.*"):
        if not p.name.startswith(prefix):
            continue  # defensive — glob could match {lang_code}.bar via wildcard
        try:
            p.unlink()
        except OSError as e:
            log.warning("failed to remove poster file %s: %s", p, e)
            warnings.append(str(p))
    return warnings


@router.patch("/admin/dramas/{drama_slug}")
async def admin_patch_drama(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    extra = set(payload.keys()) - {"default_lang"}
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"unknown / immutable fields: {sorted(extra)}",
        )
    new_default = payload.get("default_lang")
    if not new_default:
        raise HTTPException(status_code=400, detail="default_lang is required")
    try:
        row = db.update_drama_default_lang(drama_slug, new_default)
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.DramaDefaultLangNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    db.mark_drama_dirty(drama_slug)
    log.info("patched drama default_lang slug=%s default_lang=%s", drama_slug, new_default)
    return JSONResponse(row)


@router.get("/admin/dramas/{drama_slug}/translations")
async def admin_list_drama_translations(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> JSONResponse:
    try:
        out = db.list_drama_translations(drama_slug)
    except db.DramaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return JSONResponse(out)


@router.put("/admin/dramas/{drama_slug}/translations/{lang_code}")
async def admin_upsert_drama_translation(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    extra = set(payload.keys()) - {"name", "synopsis"}
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"unknown fields: {sorted(extra)} (allowed: name, synopsis)",
        )
    name = payload.get("name")
    synopsis = payload.get("synopsis")
    if name is not None and not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name must be a string")
    if synopsis is not None and not isinstance(synopsis, str):
        raise HTTPException(status_code=400, detail="synopsis must be a string")
    try:
        result = db.upsert_drama_translation(
            drama_slug, lang_code, name=name, synopsis=synopsis,
        )
    except db.DramaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.DramaValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.DramaTranslationFreshNameRequiredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"lang_code: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"lang_code: {e}")
    db.mark_drama_dirty(drama_slug)
    log.info("upserted drama translation slug=%s lang=%s fields=%s",
             drama_slug, lang_code,
             [f for f, v in (("name", name), ("synopsis", synopsis)) if v is not None])
    return JSONResponse(result)


@router.delete("/admin/dramas/{drama_slug}/translations/{lang_code}")
async def admin_delete_drama_translation(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
) -> JSONResponse:
    try:
        db.delete_drama_translation(drama_slug, lang_code)
    except db.DramaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.DramaDefaultTranslationProtectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Translation rows are gone; now remove the on-disk poster file (any ext).
    warnings = _remove_existing_poster_files(drama_slug, lang_code)
    db.mark_drama_dirty(drama_slug)
    log.info("deleted drama translation slug=%s lang=%s warnings=%d",
             drama_slug, lang_code, len(warnings))
    return JSONResponse({"ok": True, "warnings": warnings})


@router.post("/admin/dramas/{drama_slug}/poster")
async def admin_upload_drama_poster(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    lang: str = Query(..., pattern=_LANG_PATTERN),
    file: UploadFile = File(...),
) -> JSONResponse:
    # Drama must exist
    if db.get_drama(drama_slug) is None:
        await file.close()
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    # Lang must reference an active language
    lang_row = db.get_language(lang)
    if lang_row is None:
        await file.close()
        raise HTTPException(status_code=400, detail=f"lang '{lang}' is not a registered language")
    if not lang_row["is_active"]:
        await file.close()
        raise HTTPException(status_code=400, detail=f"lang '{lang}' is inactive")
    # Drama must already have a name translation in this lang
    if db.get_drama_name_translation(drama_slug, lang) is None:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail=f"drama '{drama_slug}' has no name translation in '{lang}'; "
                   f"upsert it first via PUT /admin/dramas/{drama_slug}/translations/{lang}",
        )
    # Validate MIME
    content_type = file.content_type or ""
    if content_type not in _POSTER_MIME_EXT:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content type {content_type!r}; "
                   f"accepted: {sorted(_POSTER_MIME_EXT.keys())}",
        )
    new_ext = _POSTER_MIME_EXT[content_type]

    # Order: remove any existing files for this (slug, lang) → write new file → upsert row.
    poster_dir = _poster_dir(drama_slug)
    poster_dir.mkdir(parents=True, exist_ok=True)
    _remove_existing_poster_files(drama_slug, lang)
    target_path = poster_dir / f"{lang}.{new_ext}"
    try:
        with target_path.open("wb") as out_f:
            shutil.copyfileobj(file.file, out_f, length=1024 * 1024)
    except OSError as e:
        await file.close()
        # best-effort cleanup
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"failed to write poster file: {e}")
    finally:
        await file.close()

    # Mirror to OSS staging if enabled. Order: clear stale OSS object for any
    # prior extension under this (slug, lang) → upload new bytes. On OSS
    # failure unlink the local file we just wrote and respond 500 so the DB
    # row is not left pointing at a half-published asset.
    if settings.oss_enabled:
        from .. import publish
        try:
            await asyncio.to_thread(publish.unpublish_poster_from_staging, drama_slug, lang)
            await asyncio.to_thread(
                publish.upload_poster_to_staging, drama_slug, lang, target_path,
            )
        except publish.PublishError as e:
            log.error("OSS staging upload failed for poster %s/%s: %s", drama_slug, lang, e)
            target_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"failed to mirror poster to OSS staging: {e}",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("OSS unexpected error for poster %s/%s", drama_slug, lang)
            target_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"unexpected OSS error mirroring poster: {e}",
            )

    url = _poster_url(drama_slug, lang, new_ext)
    db.upsert_drama_poster(drama_slug, lang, url)
    db.mark_drama_dirty(drama_slug)
    log.info("uploaded drama poster slug=%s lang=%s ext=%s", drama_slug, lang, new_ext)
    return JSONResponse({"slug": drama_slug, "lang_code": lang, "poster_url": url})


@router.delete("/admin/dramas/{drama_slug}/poster")
async def admin_delete_drama_poster(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    lang: str = Query(..., pattern=_LANG_PATTERN),
) -> Response:
    if db.get_drama(drama_slug) is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    if db.get_drama_poster_url(drama_slug, lang) is None:
        raise HTTPException(
            status_code=404,
            detail=f"drama '{drama_slug}' has no poster in '{lang}'",
        )
    db.delete_drama_poster(drama_slug, lang)
    _remove_existing_poster_files(drama_slug, lang)
    if settings.oss_enabled:
        from .. import publish
        try:
            await asyncio.to_thread(publish.unpublish_poster_from_staging, drama_slug, lang)
        except Exception as e:  # noqa: BLE001 — best-effort, don't fail the API
            log.warning("OSS staging cleanup failed for poster %s/%s: %s", drama_slug, lang, e)
    db.mark_drama_dirty(drama_slug)
    log.info("deleted drama poster slug=%s lang=%s", drama_slug, lang)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# episode-subtitles: WebVTT-only upload / list / delete per (episode, lang).
# Files live at OUT_DIR/{slug}/{ep_dir}/subtitles/{lang}.vtt and are served
# by the existing /videos/ static mount (no extra router-level handling).
# ---------------------------------------------------------------------------

_VTT_ALLOWED_MIME = {"text/vtt", "text/plain"}
_VTT_MAGIC = b"WEBVTT"
_UTF8_BOM = b"\xef\xbb\xbf"


def _subtitle_path(drama_slug: str, ep_number: int, lang_code: str) -> Path:
    return settings.out_dir / drama_slug / f"ep-{ep_number}" / "subtitles" / f"{lang_code}.vtt"


def _subtitle_url(drama_slug: str, ep_number: int, lang_code: str) -> str:
    return f"/videos/{drama_slug}/ep-{ep_number}/subtitles/{lang_code}.vtt"


@router.post("/admin/episodes/{drama_slug}/{ep}/subtitles")
async def admin_upload_subtitle(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
    lang: str = Query(..., pattern=_LANG_PATTERN),
    file: UploadFile = File(...),
) -> JSONResponse:
    ep_number = int(ep)
    if ep_number < 1:
        await file.close()
        raise HTTPException(status_code=422, detail="ep must be >= 1")

    # Episode exists (any status — subtitle can be uploaded before pipeline reaches ready)
    ep_row = db.get_by_slug_ep(drama_slug, ep_number)
    if ep_row is None:
        await file.close()
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")

    # Lang must be active
    lang_row = db.get_language(lang)
    if lang_row is None:
        await file.close()
        raise HTTPException(status_code=400, detail=f"lang '{lang}' is not a registered language")
    if not lang_row["is_active"]:
        await file.close()
        raise HTTPException(status_code=400, detail=f"lang '{lang}' is inactive")

    # MIME gate
    content_type = (file.content_type or "").lower()
    if content_type not in _VTT_ALLOWED_MIME:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content type {content_type!r}; accepted: {sorted(_VTT_ALLOWED_MIME)}",
        )

    # Read body, strip BOM if present, verify WEBVTT magic
    try:
        body = await file.read()
    finally:
        await file.close()

    if body.startswith(_UTF8_BOM):
        body = body[len(_UTF8_BOM):]
    if not body.startswith(_VTT_MAGIC):
        raise HTTPException(
            status_code=400,
            detail="file does not start with the WEBVTT magic bytes; expected a WebVTT (.vtt) file",
        )

    target_path = _subtitle_path(drama_slug, ep_number, lang)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_path.write_bytes(body)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to write subtitle file: {e}")

    # Mirror to OSS staging. Failure unwinds the local write so DB doesn't get
    # a row pointing at half-published content.
    if settings.oss_enabled:
        from .. import publish
        ep_dir = f"ep-{ep_number}"
        try:
            await asyncio.to_thread(
                publish.upload_subtitle_to_staging,
                drama_slug, ep_dir, lang, target_path,
            )
        except publish.PublishError as e:
            log.error(
                "OSS staging upload failed for subtitle %s/%s/%s: %s",
                drama_slug, ep_dir, lang, e,
            )
            target_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"failed to mirror subtitle to OSS staging: {e}",
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "OSS unexpected error for subtitle %s/%s/%s",
                drama_slug, ep_dir, lang,
            )
            target_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"unexpected OSS error mirroring subtitle: {e}",
            )

    episode_id = ep_row["episode_id"]
    file_url = _subtitle_url(drama_slug, ep_number, lang)
    upserted = db.upsert_subtitle(episode_id, lang, file_url)
    db.mark_episode_dirty(drama_slug, ep_number)
    log.info("uploaded subtitle slug=%s ep=%s lang=%s bytes=%d",
             drama_slug, ep_number, lang, len(body))

    return JSONResponse({
        "lang_code": lang,
        "label": lang_row["display_label"],
        "url": file_url,
        "uploaded_at": upserted["uploaded_at"],
    })


@router.get("/admin/episodes/{drama_slug}/{ep}/subtitles")
async def admin_list_subtitles(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
) -> JSONResponse:
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    ep_row = db.get_by_slug_ep(drama_slug, ep_number)
    if ep_row is None:
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")
    rows = db.list_subtitles_for_slug_ep(drama_slug, ep_number)
    return JSONResponse([
        {
            "lang_code": r["lang_code"],
            "label": r["label"],
            "url": r["file_url"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@router.delete("/admin/episodes/{drama_slug}/{ep}/subtitles")
async def admin_delete_subtitle(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
    lang: str = Query(..., pattern=_LANG_PATTERN),
) -> JSONResponse:
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    ep_row = db.get_by_slug_ep(drama_slug, ep_number)
    if ep_row is None:
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")

    deleted, _file_url = db.delete_subtitle(ep_row["episode_id"], lang)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"episode '{drama_slug}/{ep_number}' has no subtitle in '{lang}'",
        )

    warnings: list[str] = []
    target_path = _subtitle_path(drama_slug, ep_number, lang)
    try:
        target_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("failed to remove subtitle file %s: %s", target_path, e)
        warnings.append(str(target_path))

    if settings.oss_enabled:
        from .. import publish
        ep_dir = f"ep-{ep_number}"
        try:
            await asyncio.to_thread(
                publish.unpublish_subtitle_from_staging, drama_slug, ep_dir, lang,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning(
                "OSS staging cleanup failed for subtitle %s/%s/%s: %s",
                drama_slug, ep_dir, lang, e,
            )
            warnings.append(f"oss-staging:{drama_slug}/{ep_dir}/subtitles/{lang}.vtt")

    db.mark_episode_dirty(drama_slug, ep_number)
    log.info("deleted subtitle slug=%s ep=%s lang=%s warnings=%d",
             drama_slug, ep_number, lang, len(warnings))
    return JSONResponse({"ok": True, "warnings": warnings})


_SUBTITLE_BATCH_RE = re.compile(r"^EP(\d+)-(.+)$", re.IGNORECASE)


def _subtitle_to_vtt_bytes(filename: str, body: bytes) -> bytes:
    """Normalize an uploaded .vtt or .srt subtitle to WebVTT bytes. SRT is
    converted in place (comma→period in timecodes + WEBVTT header) since
    everything downstream — local path, OSS, DB — assumes `.vtt`. Raises
    ValueError with a human-readable message on bad extension / content."""
    ext = Path(filename).suffix.lower()
    if body.startswith(_UTF8_BOM):
        body = body[len(_UTF8_BOM):]
    if ext == ".vtt":
        if not body.startswith(_VTT_MAGIC):
            raise ValueError("扩展名是 .vtt 但内容不以 WEBVTT 开头")
        return body
    if ext == ".srt":
        text = body.decode("utf-8", errors="replace")
        if "-->" not in text:
            raise ValueError("扩展名是 .srt 但内容不含时间轴 '-->'")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
        return ("WEBVTT\n\n" + text.lstrip()).encode("utf-8")
    raise ValueError(f"不支持的字幕扩展名 {ext!r}；仅支持 .vtt / .srt")


@router.post("/admin/dramas/{drama_slug}/subtitles/batch")
async def admin_batch_upload_subtitles(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    """Batch-upload subtitles. Each filename must be `EP<n>-<lang>-...vtt|srt`
    (EP prefix case-insensitive); `<lang>` is resolved against the active
    language registry by longest match, so hyphenated codes like `zh-rCN`
    work even though the filename also uses `-` as separator. SRT files are
    converted to WebVTT. Existing (episode, lang) subtitles are overwritten.
    Returns a per-file result list — partial failure is normal.
    """
    if db.get_drama(drama_slug) is None:
        for f in files:
            await f.close()
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")

    active_langs = [r["code"] for r in db.list_languages(active_only=True)]
    results: list[dict] = []
    seen: dict[tuple[int, str], str] = {}  # (ep, lang) -> filename, dedupe within the batch

    for file in files:
        filename = file.filename or ""
        m = _SUBTITLE_BATCH_RE.match(Path(filename).stem.strip())
        if not m:
            await file.close()
            results.append({
                "filename": filename, "ep_number": None, "lang_code": None,
                "ok": False, "detail": "文件名须形如 EP<集号>-<语言>-说明.vtt/srt",
            })
            continue
        ep_number = int(m.group(1))
        if ep_number < 1:
            await file.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": None,
                "ok": False, "detail": "集号必须 >= 1",
            })
            continue
        remainder = m.group(2)
        # Resolve language: longest active code that `remainder` equals or
        # starts with (followed by '-'). Longest-match disambiguates hyphenated
        # codes (e.g. `zh-rCN-foo` → `zh-rCN`, not a bare `zh`).
        lang: str | None = None
        for code in active_langs:
            if remainder == code or remainder.startswith(code + "-"):
                if lang is None or len(code) > len(lang):
                    lang = code
        if lang is None:
            await file.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": None,
                "ok": False, "detail": "文件名中的语言段未匹配到任何已启用语言",
            })
            continue
        key = (ep_number, lang)
        if key in seen:
            await file.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": lang,
                "ok": False, "detail": f"与本批次文件 '{seen[key]}' 的集号+语言重复",
            })
            continue
        seen[key] = filename

        ep_row = db.get_by_slug_ep(drama_slug, ep_number)
        if ep_row is None:
            await file.close()
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": lang,
                "ok": False, "detail": f"该剧下不存在第 {ep_number} 集",
            })
            continue

        try:
            body = await file.read()
        finally:
            await file.close()
        try:
            vtt_bytes = _subtitle_to_vtt_bytes(filename, body)
        except ValueError as e:
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": lang,
                "ok": False, "detail": str(e),
            })
            continue

        target_path = _subtitle_path(drama_slug, ep_number, lang)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            target_path.write_bytes(vtt_bytes)
        except OSError as e:
            results.append({
                "filename": filename, "ep_number": ep_number, "lang_code": lang,
                "ok": False, "detail": f"写入字幕文件失败: {e}",
            })
            continue

        if settings.oss_enabled:
            from .. import publish
            ep_dir = f"ep-{ep_number}"
            try:
                await asyncio.to_thread(
                    publish.upload_subtitle_to_staging,
                    drama_slug, ep_dir, lang, target_path,
                )
            except Exception as e:  # noqa: BLE001 — PublishError or unexpected
                log.exception(
                    "OSS staging upload failed for subtitle %s/%s/%s",
                    drama_slug, ep_dir, lang,
                )
                target_path.unlink(missing_ok=True)
                results.append({
                    "filename": filename, "ep_number": ep_number, "lang_code": lang,
                    "ok": False, "detail": f"OSS staging 上传失败: {e}",
                })
                continue

        file_url = _subtitle_url(drama_slug, ep_number, lang)
        db.upsert_subtitle(ep_row["episode_id"], lang, file_url)
        db.mark_episode_dirty(drama_slug, ep_number)
        log.info("batch subtitle slug=%s ep=%s lang=%s bytes=%d file=%s",
                 drama_slug, ep_number, lang, len(vtt_bytes), filename)
        results.append({
            "filename": filename, "ep_number": ep_number, "lang_code": lang,
            "ok": True, "detail": "已上传",
        })

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({
        "ok_count": ok_count,
        "error_count": len(results) - ok_count,
        "results": results,
    })
