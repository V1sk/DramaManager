import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Path as PathParam, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import settings
from ..ffmpeg_utils import FfmpegError, extract_first_frame, probe_duration_ms
from ..queue import Job, enqueue

router = APIRouter()
log = logging.getLogger("hls.admin")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "admin.html", {})


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


@router.post("/admin/upload")
async def admin_upload(
    video: UploadFile = File(...),
    drama_slug: str = Form(...),
    drama_name: str = Form(...),
    ep_number: int = Form(...),
) -> RedirectResponse:
    drama_name = drama_name.strip()
    if not _SLUG_RE.match(drama_slug):
        raise HTTPException(
            status_code=400,
            detail="drama_slug must match ^[a-z0-9][a-z0-9-]*$",
        )
    if ep_number < 1:
        raise HTTPException(status_code=400, detail="ep_number must be >= 1")
    if not drama_name:
        raise HTTPException(status_code=400, detail="drama_name must not be empty")
    if video is None or not video.filename:
        raise HTTPException(status_code=400, detail="video file is required")

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
    finally:
        await video.close()

    try:
        duration_ms = probe_duration_ms(tmp_path)
    except FfmpegError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"ffprobe failed: {e}")

    cover_path = episode_dir / "cover.jpg"
    try:
        extract_first_frame(tmp_path, cover_path)
    except FfmpegError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"cover extraction failed: {e}")

    cover_url = f"/videos/{drama_slug}/{ep_dir_name}/cover.jpg"

    db.upsert_pending(
        drama_slug=drama_slug,
        drama_name=drama_name,
        ep_number=ep_number,
        episode_id=episode_id,
        duration_ms=duration_ms,
        cover_url=cover_url,
        source_filename=video.filename,
    )

    await enqueue(
        Job(
            episode_id=episode_id,
            drama_slug=drama_slug,
            drama_name=drama_name,
            ep_number=ep_number,
            tmp_path=tmp_path,
        )
    )
    log.info("enqueued slug=%s name=%s ep=%s", drama_slug, drama_name, episode_id)

    return RedirectResponse(url="/admin", status_code=302)


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

    db.delete_by_slug_ep(drama_slug, ep_number)

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

    if db.count_by_slug(drama_slug) == 0:
        drama_dir = settings.out_dir / drama_slug
        if drama_dir.exists():
            try:
                shutil.rmtree(drama_dir)
            except OSError as e:
                log.warning("failed to remove empty drama dir %s: %s", drama_dir, e)
                warnings.append(str(drama_dir))

    log.info(
        "deleted slug=%s name=%s ep=%s warnings=%d",
        drama_slug, row["drama_name"], row["episode_id"], len(warnings),
    )
    return JSONResponse({"ok": True, "warnings": warnings})
