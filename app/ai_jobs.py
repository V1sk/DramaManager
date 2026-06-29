"""AI 翻译任务队列 worker（ai-translation-queue）。

把重的 AI 翻译从 HTTP 请求里解耦出来：端点只插 `translation_jobs` 行 + 入队 job id，
后台 worker 池（`AI_TRANSLATE_CONCURRENCY` 个协程）拉 job 执行。DB 是真相源——
in-memory `asyncio.Queue` 只是调度通道，启动期从 DB 重新 seed，崩溃重启不丢活。

对标 `app/work_queue.py`（worker 池 + per-entity 锁）与 `app/sync.py`（DB 状态机 +
启动 reap）。翻译完成只写 editor 状态（translations 行 / 本地字幕 + prod 对象）
并标 dirty；推 prod 仍由既有 business-sync 队列负责（两队列串联）。
"""
import asyncio
import logging
from pathlib import Path

from . import ai_translate_client, db, vtt
from .ai_translate_client import AITranslateError
from .config import settings

log = logging.getLogger("hls.ai_jobs")

_SUBTITLE_CHUNK = 40          # cues per AI call (equal-length contract easier; bounds size)
_MAX_ATTEMPTS = 3            # per-job attempts (1 try + 2 retries) for transient errors
_BACKOFF_SECONDS = (2, 8, 32)  # capped exponential backoff between retries


# ---------------------------------------------------------------------------
# In-memory scheduling queue (job ids) + per-entity locks
# ---------------------------------------------------------------------------

_queue: "asyncio.Queue[int] | None" = None


def get_queue() -> "asyncio.Queue[int]":
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def enqueue(job_id: int) -> None:
    await get_queue().put(job_id)


# Per-entity locks: subtitle jobs for the SAME episode are serialized so they
# never interleave reads/writes of one `ep-{n}/subtitles/` dir; different
# episodes (and all short-text jobs) run concurrently up to the worker cap.
# Entries are never removed (one tiny Lock per episode, bounded by catalog).
_entity_locks: dict[str, asyncio.Lock] = {}


def _entity_lock(key: str) -> asyncio.Lock:
    lock = _entity_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _entity_locks[key] = lock
    return lock


def _lang_label_map() -> dict[str, str]:
    return {lang["code"]: lang["display_label"] for lang in db.list_languages()}


def _label(all_langs: dict[str, str], code: str) -> str:
    label = all_langs.get(code)
    return f"{label} ({code})" if label else code


def _local_path_from_video_url(url: str | None) -> Path | None:
    if not url or not url.startswith("/videos/"):
        return None
    return settings.out_dir / url.removeprefix("/videos/")


def _current_ep_dir(slug: str, ep_number: int) -> str:
    row = db.get_by_slug_ep(slug, ep_number)
    version = int(row.get("upload_version") or 1) if row else 1
    return db.episode_ep_dir(ep_number, version)


def _subtitle_path(slug: str, ep_number: int, lang: str) -> Path:
    """Existing subtitle path for source reads; falls back to current version."""
    episode_id = f"{slug}-ep-{ep_number}"
    for row in db.list_subtitles_for_episode(episode_id):
        if row["lang_code"] == lang:
            local = _local_path_from_video_url(row["file_url"])
            if local is not None:
                return local
    return _current_subtitle_path(slug, ep_number, lang)


def _current_subtitle_path(slug: str, ep_number: int, lang: str) -> Path:
    return settings.out_dir / slug / _current_ep_dir(slug, ep_number) / "subtitles" / f"{lang}.vtt"


def _subtitle_url(slug: str, ep_number: int, lang: str) -> str:
    return f"/videos/{slug}/{_current_ep_dir(slug, ep_number)}/subtitles/{lang}.vtt"


class _PermanentJobError(Exception):
    """Non-retryable job failure (missing entity / missing source / validation)."""


# ---------------------------------------------------------------------------
# Per-kind executors. Each performs ONE attempt for ONE target language and
# raises on failure (AITranslateError → maybe retryable; _PermanentJobError /
# db.*Error → permanent).
# ---------------------------------------------------------------------------

async def _run_drama(job: dict, all_langs: dict[str, str]) -> None:
    slug = job["entity_ref"]
    target = job["target_lang"]
    drama = db.get_drama(slug)
    if drama is None:
        raise _PermanentJobError(f"drama '{slug}' 不存在")
    default_lang = drama["default_lang"]
    src = (db.list_drama_translations(slug) or {}).get(default_lang) or {}
    name = (src.get("name") or "").strip()
    synopsis = (src.get("synopsis") or "").strip()
    if not name:
        raise _PermanentJobError(f"默认语言 '{default_lang}' 无剧名，无法翻译")
    fields = {"name": name}
    if synopsis:
        fields["synopsis"] = synopsis
    result = await ai_translate_client.translate_texts(
        source_lang_label=_label(all_langs, default_lang),
        targets=[{"code": target, "label": all_langs.get(target, target)}],
        fields=fields,
    )
    fmap = result.get(target) or {}
    t_name = (fmap.get("name") or "").strip()
    if not t_name:
        raise AITranslateError(f"模型未返回 '{target}' 的剧名", retryable=True)
    db.upsert_drama_translation(slug, target, name=t_name, synopsis=(fmap.get("synopsis") or "").strip() or None)
    db.mark_drama_dirty(slug)


async def _run_tag(job: dict, all_langs: dict[str, str]) -> None:
    slug = job["entity_ref"]
    target = job["target_lang"]
    tag = db.get_tag(slug)
    if tag is None:
        raise _PermanentJobError(f"tag '{slug}' 不存在")
    default_lang = tag["default_lang"]
    src_label = (db.list_translations_for_entity("tag", slug, "label").get(default_lang) or "").strip()
    if not src_label:
        raise _PermanentJobError(f"默认语言 '{default_lang}' 无 label")
    result = await ai_translate_client.translate_texts(
        source_lang_label=_label(all_langs, default_lang),
        targets=[{"code": target, "label": all_langs.get(target, target)}],
        fields={"label": src_label},
    )
    label = ((result.get(target) or {}).get("label") or "").strip()
    if not label:
        raise AITranslateError(f"模型未返回 '{target}' 的 label", retryable=True)
    db.upsert_tag_translation(slug, target, label)
    db.cascade_dirty_dramas_via_tag(slug)


async def _run_actor(job: dict, all_langs: dict[str, str]) -> None:
    slug = job["entity_ref"]
    target = job["target_lang"]
    actor = db.get_actor(slug)
    if actor is None:
        raise _PermanentJobError(f"actor '{slug}' 不存在")
    default_lang = actor["default_lang"]
    src_name = (db.list_translations_for_entity("actor", slug, "name").get(default_lang) or "").strip()
    if not src_name:
        raise _PermanentJobError(f"默认语言 '{default_lang}' 无 name")
    result = await ai_translate_client.translate_texts(
        source_lang_label=_label(all_langs, default_lang),
        targets=[{"code": target, "label": all_langs.get(target, target)}],
        fields={"name": src_name},
    )
    name = ((result.get(target) or {}).get("name") or "").strip()
    if not name:
        raise AITranslateError(f"模型未返回 '{target}' 的 name", retryable=True)
    db.upsert_actor_translation(slug, target, name)
    db.cascade_dirty_dramas_via_actor(slug)


async def _translate_cue_texts(src_label: str, tgt_label: str, texts: list[str]) -> list[str]:
    """Chunked cue translation with one local retry per chunk on transient error
    (count mismatch / network). A persistent chunk failure propagates."""
    out: list[str] = []
    for i in range(0, len(texts), _SUBTITLE_CHUNK):
        chunk = texts[i : i + _SUBTITLE_CHUNK]
        try:
            res = await ai_translate_client.translate_lines(
                source_lang_label=src_label, target_lang_label=tgt_label, lines=chunk,
            )
        except AITranslateError as e:
            if not e.retryable:
                raise
            res = await ai_translate_client.translate_lines(
                source_lang_label=src_label, target_lang_label=tgt_label, lines=chunk,
            )
        out.extend(res)
    return out


async def _run_subtitle(job: dict, all_langs: dict[str, str]) -> None:
    slug = job["entity_ref"]
    ep_number = job["ep_number"]
    source_lang = job["source_lang"]
    target = job["target_lang"]
    ep_row = db.get_by_slug_ep(slug, ep_number)
    if ep_row is None:
        raise _PermanentJobError(f"episode '{slug}/{ep_number}' 不存在")
    src_path = _subtitle_path(slug, ep_number, source_lang)
    if not src_path.is_file():
        raise _PermanentJobError(f"源语言 '{source_lang}' 字幕不存在")
    src_text = src_path.read_text(encoding="utf-8", errors="replace")
    blocks, texts = vtt.parse_cues(src_text)
    if not texts:
        raise _PermanentJobError("源字幕没有可翻译的 cue")

    translated = await _translate_cue_texts(
        _label(all_langs, source_lang), _label(all_langs, target), texts,
    )
    if len(translated) != len(texts):
        raise AITranslateError(
            f"字幕翻译条数不匹配（{len(translated)} vs {len(texts)}），未写入",
            retryable=True,
        )
    vtt_bytes = vtt.rebuild(blocks, translated).encode("utf-8")
    if not vtt_bytes.startswith(b"WEBVTT"):
        vtt_bytes = b"WEBVTT\n\n" + vtt_bytes

    target_path = _current_subtitle_path(slug, ep_number, target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(vtt_bytes)

    if settings.storage_enabled:
        from . import publish
        ep_dir = db.episode_ep_dir(ep_number, int(ep_row.get("upload_version") or 1))
        try:
            await asyncio.to_thread(
                publish.upload_subtitle_to_staging, slug, ep_dir, target, target_path,
            )
        except Exception as e:  # noqa: BLE001 — unwind local write on publish failure
            target_path.unlink(missing_ok=True)
            raise AITranslateError(f"字幕上传 OSS staging 失败：{e}", retryable=True) from e

    db.upsert_subtitle(f"{slug}-ep-{ep_number}", target, _subtitle_url(slug, ep_number, target))
    db.mark_episode_dirty(slug, ep_number)


_EXECUTORS = {
    "drama": _run_drama,
    "tag": _run_tag,
    "actor": _run_actor,
    "subtitle": _run_subtitle,
}


async def _run_with_retry(job: dict) -> None:
    """Run one job to a terminal state, retrying transient (`retryable`) failures
    with bounded backoff. Persists `done` / `failed` and the last error."""
    job_id = job["id"]
    executor = _EXECUTORS[job["kind"]]
    attempt = 0
    while True:
        attempt += 1
        try:
            all_langs = _lang_label_map()
            await executor(job, all_langs)
            db.set_translation_job_status(job_id, "done", bump_attempts=True)
            return
        except AITranslateError as e:
            if e.retryable and attempt < _MAX_ATTEMPTS:
                db.set_translation_job_status(
                    job_id, "running", error=f"重试 {attempt}/{_MAX_ATTEMPTS - 1}: {e}", bump_attempts=True,
                )
                await asyncio.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])
                continue
            db.set_translation_job_status(job_id, "failed", error=str(e), bump_attempts=True)
            log.warning("translation job %s failed: %s", job_id, e)
            return
        except (_PermanentJobError, db.DramaValidationError, db.TagValidationError,
                db.ActorValidationError, db.LanguageNotFoundError,
                db.DramaNotFoundError, db.TagNotFoundError, db.ActorNotFoundError,
                db.DramaTranslationFreshNameRequiredError, OSError) as e:
            db.set_translation_job_status(job_id, "failed", error=str(e), bump_attempts=True)
            log.warning("translation job %s permanently failed: %s", job_id, e)
            return


async def worker_loop(worker_id: int = 0) -> None:
    q = get_queue()
    log.info("ai-translate worker %d started", worker_id)
    while True:
        job_id = await q.get()
        try:
            job = db.get_translation_job(job_id)
            if job is None or job["status"] != "queued":
                continue  # reaped / already handled / stale queue entry
            if job["kind"] == "subtitle":
                async with _entity_lock(f"{job['entity_ref']}-ep-{job['ep_number']}"):
                    db.set_translation_job_status(job_id, "running")
                    await _run_with_retry(job)
            else:
                db.set_translation_job_status(job_id, "running")
                await _run_with_retry(job)
        except Exception:  # noqa: BLE001 — keep the worker alive across job-level bugs
            log.exception("unhandled ai-translate worker error job=%s", job_id)
            try:
                db.set_translation_job_status(job_id, "failed", error="internal worker error; see server logs")
            except Exception:  # noqa: BLE001
                log.exception("also failed to record failure for job %s", job_id)
        finally:
            q.task_done()


async def seed_from_db() -> int:
    """Push every `queued` job id onto the in-memory queue. Called at startup
    after the reap so jobs left over from before a restart resume."""
    ids = db.list_queued_translation_job_ids()
    for jid in ids:
        await get_queue().put(jid)
    return len(ids)
