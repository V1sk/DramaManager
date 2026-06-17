"""AI 翻译入队路由（ai-translation-queue）。

端点只**插 job 行 + 入队**并返回 202——重活由 `app/ai_jobs.py` 的后台 worker 池跑，
不再绑在 HTTP 请求 / 浏览器生命周期上。"翻译到全部语言"扇出每个目标语言一行 job
（每语言一 job）。落库仍走现有 `upsert_*_translation` / 字幕写盘 + `mark_*_dirty`，
天然进入 staging → 手动同步流程。

粒度：每目标语言一个 job。默认**跳过已 done 的语言**（可续传）；`force` 忽略 done
重翻。dedupe 索引保证同一单元不会有重复 in-flight job。

仅当 `settings.ai_translate_enabled` 时可用，否则 503。鉴权沿用 `/admin` 的
`require_user`（与手工编辑翻译同级）。
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import ai_jobs, db
from ..config import settings

router = APIRouter()
log = logging.getLogger("hls.ai_translate_router")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_EP_PATTERN = r"^[0-9]+$"


def _require_enabled() -> None:
    if not settings.ai_translate_enabled:
        raise HTTPException(status_code=503, detail="AI 翻译未启用（未配置 AI_TRANSLATE_API_KEY）")


def _lang_codes() -> list[str]:
    return [lang["code"] for lang in db.list_languages()]


async def _fanout(
    kind: str,
    entity_ref: str,
    target_codes: list[str],
    *,
    ep_number: int | None = None,
    source_lang: str | None = None,
) -> tuple[list[str], list[str]]:
    """Insert one queued job per target language and push it onto the worker
    queue. Returns `(enqueued, skipped)` where `skipped` were deduped against an
    already in-flight job."""
    enqueued: list[str] = []
    skipped: list[str] = []
    for code in target_codes:
        job_id = db.enqueue_translation_job(
            kind, entity_ref, code, ep_number=ep_number, source_lang=source_lang,
        )
        if job_id is not None:
            await ai_jobs.enqueue(job_id)
            enqueued.append(code)
        else:
            skipped.append(code)  # an in-flight job for this unit already exists
    return enqueued, skipped


def _targets_for(default_lang: str, *, force: bool, kind: str, entity_ref: str,
                 ep_number: int | None = None) -> list[str]:
    """Registered languages minus the source, minus already-`done` (unless force)."""
    done = set() if force else db.done_translation_target_langs(kind, entity_ref, ep_number)
    return [c for c in _lang_codes() if c != default_lang and c not in done]


@router.post("/admin/dramas/{drama_slug}/translate")
async def ai_translate_drama(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    force: bool = Query(False),
) -> JSONResponse:
    """Enqueue per-language jobs to translate the drama's name (+ synopsis) from
    its default_lang into every other registered language."""
    _require_enabled()
    drama = db.get_drama(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    default_lang = drama["default_lang"]
    src = (db.list_drama_translations(drama_slug) or {}).get(default_lang) or {}
    if not (src.get("name") or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有剧名，无法翻译")
    targets = _targets_for(default_lang, force=force, kind="drama", entity_ref=drama_slug)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("drama", drama_slug, targets, source_lang=default_lang)
    log.info("enqueued drama translate slug=%s enqueued=%d skipped=%d", drama_slug, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/tags/{slug}/translate")
async def ai_translate_tag(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    force: bool = Query(False),
) -> JSONResponse:
    _require_enabled()
    tag = db.get_tag(slug)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"tag '{slug}' not found")
    default_lang = tag["default_lang"]
    if not (db.list_translations_for_entity("tag", slug, "label").get(default_lang) or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有 label，无法翻译")
    targets = _targets_for(default_lang, force=force, kind="tag", entity_ref=slug)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("tag", slug, targets, source_lang=default_lang)
    log.info("enqueued tag translate slug=%s enqueued=%d skipped=%d", slug, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/actors/{slug}/translate")
async def ai_translate_actor(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    force: bool = Query(False),
) -> JSONResponse:
    _require_enabled()
    actor = db.get_actor(slug)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor '{slug}' not found")
    default_lang = actor["default_lang"]
    if not (db.list_translations_for_entity("actor", slug, "name").get(default_lang) or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有 name，无法翻译")
    targets = _targets_for(default_lang, force=force, kind="actor", entity_ref=slug)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("actor", slug, targets, source_lang=default_lang)
    log.info("enqueued actor translate slug=%s enqueued=%d skipped=%d", slug, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/episodes/{drama_slug}/{ep}/subtitles/translate")
async def ai_translate_subtitle(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    ep: str = PathParam(..., pattern=_EP_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    """Enqueue per-language subtitle-translation jobs from one existing subtitle
    (`source_lang`) into `targets` (default: all other registered languages)."""
    _require_enabled()
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    if db.get_by_slug_ep(drama_slug, ep_number) is None:
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    source_lang = (payload.get("source_lang") or "").strip()
    if not source_lang:
        raise HTTPException(status_code=400, detail="source_lang 必填")
    if not ai_jobs._subtitle_path(drama_slug, ep_number, source_lang).is_file():
        raise HTTPException(status_code=400, detail=f"源语言 '{source_lang}' 的字幕不存在")

    all_codes = _lang_codes()
    force = bool(payload.get("force"))
    requested = payload.get("targets")
    if requested is not None:
        if not isinstance(requested, list) or not all(isinstance(c, str) for c in requested):
            raise HTTPException(status_code=400, detail="targets 必须是语言代码字符串数组")
        bad = [c for c in requested if c not in all_codes]
        if bad:
            raise HTTPException(status_code=400, detail=f"未注册的目标语言: {bad}")
        candidates = [c for c in requested if c != source_lang]
    else:
        candidates = [c for c in all_codes if c != source_lang]

    done = set() if force else db.done_translation_target_langs("subtitle", drama_slug, ep_number)
    targets = [c for c in candidates if c not in done]
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout(
        "subtitle", drama_slug, targets, ep_number=ep_number, source_lang=source_lang,
    )
    log.info("enqueued subtitle translate slug=%s ep=%s src=%s enqueued=%d skipped=%d",
             drama_slug, ep_number, source_lang, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "enqueued": enqueued, "skipped": skipped}, status_code=202)


# ---------------------------------------------------------------------------
# Operator surfaces: jobs overview page, nav-badge summary, retry-failed.
# ---------------------------------------------------------------------------


@router.get("/admin/translations", response_class=HTMLResponse)
async def translations_overview(request: Request) -> HTMLResponse:
    """Overview of non-`done` translation jobs (queued / running / failed)."""
    return _TEMPLATES.TemplateResponse(
        request,
        "translations.html",
        {
            "jobs": db.list_translation_jobs(),
            "counts": db.count_translation_jobs_by_status(),
            "ai_enabled": settings.ai_translate_enabled,
            "nav_active": "translations",
        },
    )


@router.get("/admin/translations/summary")
async def translations_summary() -> JSONResponse:
    """Lightweight JSON polled by the nav bar. `outstanding` = non-`done` jobs
    (queued + running + failed) so failures keep the badge visible until cleared."""
    if not settings.ai_translate_enabled:
        return JSONResponse({"enabled": False, "outstanding": 0})
    c = db.count_translation_jobs_by_status()
    return JSONResponse({
        "enabled": True,
        "outstanding": c["queued"] + c["running"] + c["failed"],
        "queued": c["queued"],
        "running": c["running"],
        "failed": c["failed"],
    })


@router.post("/admin/translations/retry")
async def translations_retry_failed() -> JSONResponse:
    """Re-enqueue every `failed` job (fresh queued jobs for the same units;
    dedupe-protected). Failed rows stay as history."""
    _require_enabled()
    requeued = 0
    for j in db.list_translation_jobs(statuses=("failed",), limit=1000):
        job_id = db.enqueue_translation_job(
            j["kind"], j["entity_ref"], j["target_lang"],
            ep_number=j["ep_number"], source_lang=j["source_lang"],
        )
        if job_id is not None:
            await ai_jobs.enqueue(job_id)
            requeued += 1
    log.info("re-enqueued %d failed translation job(s)", requeued)
    return JSONResponse({"ok": True, "requeued": requeued})
