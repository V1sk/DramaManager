import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import settings
from .pipeline import run_pipeline
from .publish import PublishError, publish_ladder

log = logging.getLogger("hls.worker")


@dataclass
class Job:
    episode_id: str
    drama_slug: str
    ep_number: int
    tmp_path: Path
    # reupload-versioning: captured at enqueue time (NOT re-read from the DB
    # in the worker). Routers compute the version when they call
    # `upsert_pending` and plumb it here; that way a second re-upload that
    # arrives while this job is still queued can't bump the row's version
    # and silently retarget this job's output to the wrong path.
    upload_version: int = 1


_queue: asyncio.Queue[Job] | None = None


def get_queue() -> asyncio.Queue[Job]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def enqueue(job: Job) -> None:
    await get_queue().put(job)


# Per-episode locks: with PIPELINE_CONCURRENCY > 1 several workers run jobs in
# parallel, but two jobs for the SAME episode_id must never run together — they
# write the same `ep-{n}/` output dir and `keys/` files and would clobber each
# other. Different episodes get different locks and run concurrently. Entries
# are intentionally never removed: one tiny Lock per unique episode_id, bounded
# by catalog size, and cleanup would race with a freshly-enqueued same-episode
# job grabbing a stale lock object.
_episode_locks: dict[str, asyncio.Lock] = {}


def _get_episode_lock(episode_id: str) -> asyncio.Lock:
    lock = _episode_locks.get(episode_id)
    if lock is None:
        lock = asyncio.Lock()
        _episode_locks[episode_id] = lock
    return lock


def is_tmp_source(path) -> bool:
    """True iff `path` lives under `UPLOAD_TMP_DIR` — i.e. a streamed-upload temp
    file this service owns and should delete once the encode succeeds. A NAS (or
    any other) source returns False and is KEPT: it's operator-owned
    source-of-truth read in place (nas-source-ingest), not our scratch copy.
    The whole keep/delete policy keys off location, so retries and re-uploads
    inherit it without threading a flag through every call site."""
    try:
        Path(path).resolve().relative_to(settings.upload_tmp_dir)
        return True
    except (ValueError, OSError):
        return False


def _cleanup_tmp(tmp_path: Path) -> None:
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("failed to remove tmp upload %s: %s", tmp_path, e)


def _ladder_encode_complete(rung_dir: Path, ladder: str) -> bool:
    """True iff this rung's clear→encrypted artifacts are fully on disk: the
    media playlist exists and carries the `#EXT-X-KEY` line (so the encrypt
    stage finished), the init segment exists, and every segment the playlist
    references is present. A truncated encode (some segments missing, or the
    key line never injected) returns False so the caller re-runs the full
    pipeline rather than reusing a half-baked rung."""
    m3u8 = rung_dir / f"media-{ladder}.m3u8"
    init = rung_dir / f"init-{ladder}.mp4"
    if not m3u8.is_file() or not init.is_file():
        return False
    try:
        text = m3u8.read_text()
    except OSError:
        return False
    if "#EXT-X-KEY" not in text:
        return False  # encrypt-segments.sh injects this last; absence = unfinished
    seg_count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        seg_count += 1
        # Local m3u8 keeps relative filenames; take the basename defensively.
        if not (rung_dir / Path(line).name).is_file():
            return False
    return seg_count > 0


def _encode_artifacts_complete(out_dir: Path, ep_dir: str) -> bool:
    """True iff a prior pipeline run already produced every artifact this job
    would otherwise re-encode: the DRM key material (`.key.b64` + `.iv`) plus
    all three ladder rungs, each with all of its segments on disk. Lets the
    worker skip an expensive full re-encode on a retry whose encode succeeded
    and only the OSS publish failed.

    Safe-by-construction: the DRM key is generated only inside `run_pipeline`,
    so a *reused* (skipped) encode means the key — and therefore the ciphertext
    of every already-uploaded segment — is unchanged. That's exactly the
    precondition for `publish_ladder(skip_existing=True)` to safely keep the
    bucket objects from the earlier partial publish."""
    keys_dir = out_dir / "keys"
    if not (keys_dir / f"{ep_dir}.key.b64").is_file():
        return False
    if not (keys_dir / f"{ep_dir}.iv").is_file():
        return False
    for ladder in ("540p", "720p", "1080p"):
        if not _ladder_encode_complete(out_dir / ep_dir / ladder, ladder):
            return False
    return True


async def _handle_job(job: Job) -> bool:
    """Run one pipeline job. Returns True iff the episode reached `ready`.

    A False return means the row was set to `failed` somewhere along the way
    and the worker MUST keep `job.tmp_path` on disk so the operator can
    one-click retry from the admin UI without re-uploading the source.
    """
    slug = job.drama_slug
    ep_id = job.episode_id              # DB 里的完整 episode_id："{slug}-ep-{n}"（SDK 契约）
    # 目录名 / URL 段 / key 文件名前缀。reupload-versioning：v1 仍是 `ep-{n}`，
    # v2+ 变成 `ep-{n}-v{V}`，让客户端缓存（按 URL 命中）跟着 m3u8 的新路径走
    # ——避免新 key + 旧 segments 静默乱码。/drm router 的 pattern
    # `^[a-z0-9][a-z0-9-]*$` 已能匹配两种形态。
    ep_dir = db.episode_ep_dir(job.ep_number, job.upload_version)
    out_dir = settings.out_dir / slug
    # 相对路径：写进 m3u8 的 #EXT-X-KEY:URI 是同一个字符串，播放器按 playlist 自身的
    # host 补全；SDK 主动调用也基于同一个 host，和 m3u8 里 verbatim 一致。
    key_uri = f"/drm/{slug}/{ep_dir}/key"
    # The persisted play_url is informational; db._apply_default_ladder rewrites
    # it from settings.default_ladder at read time (admin preview only — the SDK
    # API returns all rungs via videoTracks). We still write a sensible value
    # here so one-off DB inspections don't show NULL.
    ladder = settings.default_ladder
    play_url = f"/videos/{slug}/{ep_dir}/{ladder}/media-{ladder}.m3u8"

    db.set_status(ep_id, "encoding")
    log.info("encoding start slug=%s ep=%s", slug, ep_id)

    # Resume optimization (retry after a publish failure): if a prior run already
    # produced a complete set of encode artifacts for this ep_dir, skip the
    # expensive re-encode and go straight to (resumable) publish. Re-encoding all
    # three rungs from 540p just because one 1080p segment failed to upload is
    # pure waste. The DRM key is regenerated only inside run_pipeline, so reusing
    # the encode keeps the key stable — which is what makes the partial-publish
    # resume below safe (already-uploaded segments stay valid).
    encode_reused = _encode_artifacts_complete(out_dir, ep_dir)
    if encode_reused:
        db.set_episode_progress(ep_id, "复用已编码切片…")
        log.info(
            "encode artifacts complete slug=%s ep=%s; skipping re-encode",
            slug, ep_id,
        )
    else:
        db.set_episode_progress(ep_id, "准备编码…")

        def _on_stage(label: str) -> None:
            # Invoked from inside run_pipeline's stdout drain. Cheap sync write.
            db.set_episode_progress(ep_id, label)

        rc, stderr_tail = await run_pipeline(
            source=job.tmp_path,
            out_dir=out_dir,
            episode_id=ep_dir,
            key_uri=key_uri,
            on_progress=_on_stage,
        )

        if rc != 0:
            db.set_status(ep_id, "failed", error_message=stderr_tail)
            log.error(
                "encoding failed slug=%s ep=%s rc=%s",
                slug, ep_id, rc,
            )
            return False

    key_b64_path = out_dir / "keys" / f"{ep_dir}.key.b64"
    iv_path = out_dir / "keys" / f"{ep_dir}.iv"
    try:
        key_b64 = key_b64_path.read_text().strip()
        iv_hex = iv_path.read_text().strip()
    except OSError as e:
        db.set_status(
            ep_id, "failed",
            error_message=f"pipeline ok but key/iv missing: {e}",
        )
        log.error("key/iv read failed slug=%s ep=%s: %s", slug, ep_id, e)
        return False

    # OSS 启用时：把每档 ladder 的 init + 全部 segment 上传到 OSS，并改写 m3u8。
    # 任一档失败 → episode 置 failed，不进入 ready。本地产物保留供事后排查。
    if settings.storage_enabled:
        for ladder_name in ("540p", "720p", "1080p"):
            db.set_episode_progress(ep_id, f"上传 OSS · {ladder_name}")
            try:
                # skip_existing=encode_reused: on a retry that reused the encode,
                # only re-send the segments that never made it to the bucket; on a
                # fresh encode, upload everything (overwrite).
                uploaded, skipped = await asyncio.to_thread(
                    publish_ladder, slug, ep_dir, ladder_name,
                    skip_existing=encode_reused,
                )
            except PublishError as e:
                db.set_status(ep_id, "failed", error_message=str(e))
                log.error(
                    "publish failed slug=%s ep=%s ladder=%s: %s",
                    slug, ep_id, ladder_name, e,
                )
                return False
            except Exception as e:  # noqa: BLE001 — 网络 / SDK / FS 异常一律转 failed
                db.set_status(
                    ep_id, "failed",
                    error_message=f"publish unexpected error for {ladder_name}: {e}",
                )
                log.exception(
                    "publish unexpected error slug=%s ep=%s ladder=%s",
                    slug, ep_id, ladder_name,
                )
                return False
            if skipped:
                log.info(
                    "publish slug=%s ep=%s ladder=%s resumed: uploaded=%d skipped=%d",
                    slug, ep_id, ladder_name, uploaded, skipped,
                )

    db.set_status(
        ep_id, "ready",
        play_url=play_url,
        key_uri=key_uri,
        key_b64=key_b64,
        iv_hex=iv_hex,
    )
    log.info("encoding ok slug=%s ep=%s", slug, ep_id)
    return True


async def worker_loop(worker_id: int = 0) -> None:
    q = get_queue()
    log.info("pipeline worker %d started", worker_id)
    while True:
        job = await q.get()
        success = False
        try:
            # Serialize same-episode jobs; different episodes run in parallel.
            async with _get_episode_lock(job.episode_id):
                success = await _handle_job(job)
        except Exception:  # noqa: BLE001 — keep the worker alive across job-level bugs
            log.exception("unhandled worker error on ep=%s", job.episode_id)
            try:
                db.set_status(
                    job.episode_id, "failed",
                    error_message="internal worker error; see server logs",
                )
            except Exception:  # noqa: BLE001
                log.exception("also failed to record failure for %s", job.episode_id)
        finally:
            # Only drop the temp source on success. A failed episode keeps its
            # source on disk + `source_path` column so the admin UI's "重试"
            # button can re-enqueue without a re-upload. The DB column is
            # cleared in the same step so a stale path can't survive cleanup.
            # NAS sources (read in place, NOT under UPLOAD_TMP_DIR) are never
            # deleted — they're operator-owned source-of-truth; only the column
            # is cleared so the row looks identical to a finished upload.
            if success:
                if is_tmp_source(job.tmp_path):
                    _cleanup_tmp(job.tmp_path)
                try:
                    db.clear_episode_source_path(job.episode_id)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "failed to clear source_path for %s", job.episode_id,
                    )
            q.task_done()
