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


def _cleanup_tmp(tmp_path: Path) -> None:
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("failed to remove tmp upload %s: %s", tmp_path, e)


async def _handle_job(job: Job) -> bool:
    """Run one pipeline job. Returns True iff the episode reached `ready`.

    A False return means the row was set to `failed` somewhere along the way
    and the worker MUST keep `job.tmp_path` on disk so the operator can
    one-click retry from the admin UI without re-uploading the source.
    """
    slug = job.drama_slug
    ep_id = job.episode_id              # DB 里的完整 episode_id："{slug}-ep-{n}"（SDK 契约）
    ep_dir = f"ep-{job.ep_number}"      # 目录名 / URL 段 / key 文件名前缀 —— 必须与 admin.py
                                        # 里 ep_dir_name 一致，且与 /drm router 的 pattern
                                        # `^ep-[0-9]+$` 对齐。
    out_dir = settings.out_dir / slug
    # 相对路径：写进 m3u8 的 #EXT-X-KEY:URI 是同一个字符串，播放器按 playlist 自身的
    # host 补全；SDK 主动调用也基于同一个 host，和 m3u8 里 verbatim 一致。
    key_uri = f"/drm/{slug}/{ep_dir}/key"
    # The persisted play_url is informational; api.py derives the actual playUrl
    # from settings.default_ladder at read time so flipping the env var takes
    # effect without re-encoding. We still write a sensible value here so
    # one-off DB inspections don't show NULL.
    ladder = settings.default_ladder
    play_url = f"/videos/{slug}/{ep_dir}/{ladder}/media-{ladder}.m3u8"

    db.set_status(ep_id, "encoding")
    db.set_episode_progress(ep_id, "准备编码…")
    log.info("encoding start slug=%s ep=%s", slug, ep_id)

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
                await asyncio.to_thread(publish_ladder, slug, ep_dir, ladder_name)
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
            if success:
                _cleanup_tmp(job.tmp_path)
                try:
                    db.clear_episode_source_path(job.episode_id)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "failed to clear source_path for %s", job.episode_id,
                    )
            q.task_done()
