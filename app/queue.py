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


async def _handle_job(job: Job) -> None:
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
    log.info("encoding start slug=%s ep=%s", slug, ep_id)

    rc, stderr_tail = await run_pipeline(
        source=job.tmp_path,
        out_dir=out_dir,
        episode_id=ep_dir,
        key_uri=key_uri,
    )

    if rc == 0:
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
            return

        # OSS 启用时：把每档 ladder 的 init + 全部 segment 上传到 OSS，并改写 m3u8。
        # 任一档失败 → episode 置 failed，不进入 ready。本地产物保留供事后排查。
        if settings.oss_enabled:
            for ladder in ("540p", "720p", "1080p"):
                try:
                    await asyncio.to_thread(publish_ladder, slug, ep_dir, ladder)
                except PublishError as e:
                    db.set_status(ep_id, "failed", error_message=str(e))
                    log.error(
                        "publish failed slug=%s ep=%s ladder=%s: %s",
                        slug, ep_id, ladder, e,
                    )
                    return
                except Exception as e:  # noqa: BLE001 — 网络 / SDK / FS 异常一律转 failed
                    db.set_status(
                        ep_id, "failed",
                        error_message=f"publish unexpected error for {ladder}: {e}",
                    )
                    log.exception(
                        "publish unexpected error slug=%s ep=%s ladder=%s",
                        slug, ep_id, ladder,
                    )
                    return

        db.set_status(
            ep_id, "ready",
            play_url=play_url,
            key_uri=key_uri,
            key_b64=key_b64,
            iv_hex=iv_hex,
        )
        log.info("encoding ok slug=%s ep=%s", slug, ep_id)
    else:
        db.set_status(ep_id, "failed", error_message=stderr_tail)
        log.error(
            "encoding failed slug=%s ep=%s rc=%s",
            slug, ep_id, rc,
        )


async def worker_loop(worker_id: int = 0) -> None:
    q = get_queue()
    log.info("pipeline worker %d started", worker_id)
    while True:
        job = await q.get()
        try:
            # Serialize same-episode jobs; different episodes run in parallel.
            async with _get_episode_lock(job.episode_id):
                await _handle_job(job)
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
            _cleanup_tmp(job.tmp_path)
            q.task_done()
