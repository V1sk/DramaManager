import asyncio
import shutil

from fastapi import APIRouter, File, HTTPException, Path, UploadFile
from fastapi.responses import JSONResponse

from .. import db
from ..config import settings
from ..models import DramaSummary, DrmInfo, EpisodeInfo, Subtitle, VideoTrack

router = APIRouter(prefix="/api")

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_EP_PATTERN = r"^[0-9]+$"


# Ladder rungs (must mirror LADDERS in pipeline.sh) mapped to the SDK
# `videoTracks` id. Ordered high → low so the SDK gets descending quality.
_LADDER_TRACKS = (
    ("high", "1080p", 1080),
    ("mid", "720p", 720),
    ("low", "540p", 540),
)


def _rung_dimensions(src_w, src_h, rung_height):
    """Encoded (width, height) of one ladder rung. `encode-clear.sh` runs
    `scale=-2:HEIGHT`, so the rung height is fixed and the width follows the
    source aspect ratio rounded to an even integer. Returns (None, None) when
    the source dimensions are unknown (legacy rows)."""
    if not src_w or not src_h:
        return None, None
    width = int(src_w * rung_height / src_h / 2 + 0.5) * 2
    return max(width, 2), rung_height


def _row_to_episode_info(row: dict) -> EpisodeInfo:
    """把 DB row 映射为对外的 EpisodeInfo 对象。单集端点和按剧集数列表端点共用此函数，
    保证 SDK 侧看到的同一集 payload 永远一致。

    所有 URL（videoTracks[].url / coverUrl / drm.keyUri）都是业务 host 相对路径
    —— HLS 服务器是本地审片用，预览页通过 `/videos/` 静态挂载直接读 OUT_DIR 下的
    本地切片，零 CORS、零 TOS 公网出站。生产端（业务服务器）从 sync payload 拿到
    path-only 形态后自行拼 `MEDIA_BASE_URL`。

    `videoTracks` 始终带全三档 rung（high / mid / low）；SDK 客户端自行选档。
    每档 width / height 由源视频 codec 尺寸按 `scale=-2:HEIGHT` 推导（老行源尺寸
    缺失时为 null）。
    """
    slug = row["drama_slug"]
    ep_id = row["episode_id"]                 # SDK 契约字段："{slug}-ep-{n}"
    ep_dir = f"ep-{row['ep_number']}"         # 磁盘目录 / URL 段（必须对齐 admin.py + /drm router）
    base = f"/videos/{slug}/{ep_dir}"

    drm = None
    if row["key_uri"] and row["key_b64"]:
        drm = DrmInfo(
            keyUri=row["key_uri"],
            keyBase64=row["key_b64"],
            ivHex=row["iv_hex"],
        )

    src_w = row.get("width")
    src_h = row.get("height")
    video_tracks = []
    for track_id, ladder, rung_height in _LADDER_TRACKS:
        w, h = _rung_dimensions(src_w, src_h, rung_height)
        video_tracks.append(VideoTrack(
            id=track_id,
            url=f"{base}/{ladder}/media-{ladder}.m3u8",
            width=w,
            height=h,
        ))

    # subtitles: side-loaded; null when none exist (matches the cover/drm
    # convention). Always sourced from the same db helper so the per-drama list
    # endpoint and the single-episode endpoint stay byte-identical.
    sub_rows = db.list_subtitles_for_slug_ep(slug, row["ep_number"])
    subtitles = (
        [
            Subtitle(langCode=s["lang_code"], label=s["label"], url=s["file_url"])
            for s in sub_rows
        ]
        if sub_rows
        else None
    )

    return EpisodeInfo(
        episodeId=ep_id,
        durationMs=row["duration_ms"],
        coverUrl=row["cover_url"],
        videoTracks=video_tracks,
        drm=drm,
        subtitles=subtitles,
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

    ep_dir = f"ep-{ep_number}"
    cover_path = settings.out_dir / drama_slug / ep_dir / "cover.jpg"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    # Snapshot prior cover so we can restore on OSS failure (assets-to-oss).
    backup_path = cover_path.with_suffix(".jpg.bak")
    backup_made = False
    if cover_path.exists():
        try:
            shutil.copy2(cover_path, backup_path)
            backup_made = True
        except OSError:
            # Snapshot failure isn't fatal; proceed without rollback safety net.
            backup_made = False
    try:
        with cover_path.open("wb") as out_f:
            shutil.copyfileobj(cover.file, out_f, length=1024 * 1024)
    finally:
        await cover.close()

    if settings.storage_enabled:
        from .. import publish
        try:
            await asyncio.to_thread(
                publish.upload_cover_to_staging, drama_slug, ep_dir, cover_path,
            )
        except publish.PublishError as e:
            # Restore prior cover so the local + DB state stays consistent.
            if backup_made:
                try:
                    shutil.move(backup_path, cover_path)
                except OSError:
                    cover_path.unlink(missing_ok=True)
            else:
                cover_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"failed to mirror cover to OSS staging: {e}",
            )
        except Exception as e:  # noqa: BLE001
            if backup_made:
                try:
                    shutil.move(backup_path, cover_path)
                except OSError:
                    cover_path.unlink(missing_ok=True)
            else:
                cover_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail=f"unexpected OSS error mirroring cover: {e}",
            )

    # Success path: drop the backup if we made one.
    if backup_made:
        backup_path.unlink(missing_ok=True)

    db.bump_updated_at(drama_slug, ep_number)
    db.mark_episode_dirty(drama_slug, ep_number)
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


@router.get("/actors")
async def list_actors_for_sdk() -> JSONResponse:
    """SDK-facing list of every actor with name resolved to its `default_lang`.
    Ordering: `slug ASC`. Empty registry → `[]`. `?lang=` resolution lands in
    `sdk-search-and-localization`.
    """
    rows = db.list_actors()
    out = sorted(
        ({"slug": r["slug"], "name": r["default_name"]} for r in rows),
        key=lambda x: x["slug"],
    )
    return JSONResponse(out)


@router.get("/tags")
async def list_tags_for_sdk() -> JSONResponse:
    """SDK-facing list of every tag with its label resolved to the tag's
    default_lang. Ordering: `slug ASC`. Empty registry → `[]`.

    Per-request `?lang=` resolution lands in `sdk-search-and-localization`;
    this version always returns the default-lang label.
    """
    rows = db.list_tags()
    out = sorted(
        ({"slug": r["slug"], "label": r["default_label"]} for r in rows),
        key=lambda x: x["slug"],
    )
    return JSONResponse(out)


@router.get("/languages")
async def list_active_languages() -> JSONResponse:
    """SDK-facing list of active languages. Inactive rows excluded; ordering is
    `code ASC`. Empty registry → `[]`. Used by SDK to enumerate available
    locales for subtitle pickers / drama-name resolution.
    """
    rows = db.list_languages(active_only=True)
    return JSONResponse([
        {"code": r["code"], "display_label": r["display_label"]}
        for r in rows
    ])
