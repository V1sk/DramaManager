import shutil

from fastapi import APIRouter, File, HTTPException, Path, UploadFile
from fastapi.responses import JSONResponse

from .. import db
from ..config import settings
from ..models import DramaSummary, DrmInfo, EpisodeInfo, FallbackPlaylists
from ..oss_upload import oss_public_base_url

router = APIRouter(prefix="/api")

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_EP_PATTERN = r"^[0-9]+$"


def _row_to_episode_info(row: dict) -> EpisodeInfo:
    """把 DB row 映射为对外的 EpisodeInfo 对象。单集端点和按剧集数列表端点共用此函数，
    保证 SDK 侧看到的同一集 payload 永远一致。

    initUrl / firstSegUrl / fallback 的 URL 由 `drama_slug` + `episode_id` + 固定 ladder
    约定（540 / 720 / 1080）线性拼出，依赖 `pipeline.sh` 产物布局不变——未来 pipeline
    支持可变 rung 组合时需要在 DB 里记录实际产出的 rung 集合。

    URL 双形态（与 OSS 模式联动）：
      - playUrl / fallback / coverUrl / drm.keyUri：永远走业务 host 相对路径。
      - initUrl / firstSegUrl：OSS 启用 → 绝对 OSS URL；OSS 未启用 → 业务 host 相对路径。
    """
    slug = row["drama_slug"]
    ep_id = row["episode_id"]                 # SDK 契约字段："{slug}-ep-{n}"
    ep_dir = f"ep-{row['ep_number']}"         # 磁盘目录 / URL 段（必须对齐 admin.py + /drm router）
    base = f"/videos/{slug}/{ep_dir}"
    if settings.oss_enabled:
        media_base = f"{oss_public_base_url}/{slug}/{ep_dir}"
    else:
        media_base = base

    drm = None
    if row["key_uri"] and row["key_b64"]:
        drm = DrmInfo(
            keyUri=row["key_uri"],
            keyBase64=row["key_b64"],
            ivHex=row["iv_hex"],
        )
    return EpisodeInfo(
        episodeId=ep_id,
        playUrl=row["play_url"],
        durationMs=row["duration_ms"],
        coverUrl=row["cover_url"],
        width=row.get("width"),
        height=row.get("height"),
        initUrl=f"{media_base}/720p/init-720p.mp4",
        firstSegUrl=f"{media_base}/720p/seg-720p-0.m4s",
        drm=drm,
        fallback=FallbackPlaylists(
            low=f"{base}/540p/media-540p.m3u8",
            high=f"{base}/1080p/media-1080p.m3u8",
        ),
    )


def _row_to_drama_summary(row: dict) -> DramaSummary:
    return DramaSummary(
        dramaSlug=row["drama_slug"],
        dramaName=row["drama_name"],
        epCount=row["ep_count"],
        latestEpNumber=row["latest_ep_number"],
        posterUrl=row["poster_url"],
        lastUpdatedAt=row["last_updated_at"],
    )


@router.get("/episodes/{drama_slug}/{ep}")
async def get_episode(
    drama_slug: str = Path(..., pattern=_SLUG_PATTERN),
    ep: str = Path(..., pattern=_EP_PATTERN),
) -> JSONResponse:
    ep_number = int(ep)
    row = db.get_by_slug_ep(drama_slug, ep_number)
    if row is None or row["status"] != "ready":
        raise HTTPException(status_code=404, detail="episode not found")
    return JSONResponse(_row_to_episode_info(row).model_dump(exclude_none=False))


@router.post("/episodes/{drama_slug}/{ep}/cover")
async def replace_cover(
    drama_slug: str = Path(..., pattern=_SLUG_PATTERN),
    ep: str = Path(..., pattern=_EP_PATTERN),
    cover: UploadFile = File(...),
) -> JSONResponse:
    ep_number = int(ep)
    row = db.get_by_slug_ep(drama_slug, ep_number)
    if row is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if not cover.content_type or not cover.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="cover must be an image/* upload")

    cover_path = settings.out_dir / drama_slug / f"ep-{ep_number}" / "cover.jpg"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with cover_path.open("wb") as out_f:
            shutil.copyfileobj(cover.file, out_f, length=1024 * 1024)
    finally:
        await cover.close()

    db.bump_updated_at(drama_slug, ep_number)
    return JSONResponse({"ok": True})


@router.get("/dramas")
async def list_dramas() -> JSONResponse:
    rows = db.list_ready_dramas()
    summaries = [_row_to_drama_summary(r) for r in rows]
    return JSONResponse([s.model_dump() for s in summaries])


@router.get("/dramas/{drama_slug}/episodes")
async def list_drama_episodes(
    drama_slug: str = Path(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    rows = db.list_ready_by_slug(drama_slug)
    infos = [_row_to_episode_info(r) for r in rows]
    return JSONResponse([i.model_dump(exclude_none=False) for i in infos])
