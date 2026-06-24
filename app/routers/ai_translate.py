"""AI 翻译入队路由（ai-translation-queue）。

端点只**插 job 行 + 入队**并返回 202——重活由 `app/ai_jobs.py` 的后台 worker 池跑，
不再绑在 HTTP 请求 / 浏览器生命周期上。"翻译到全部语言"扇出每个目标语言一行 job
（每语言一 job）。落库仍走现有 `upsert_*_translation` / 字幕写盘 + `mark_*_dirty`，
天然进入 staging → 手动同步流程。

粒度：每目标语言一个 job。一键翻译是**覆盖式**——每次都重翻全部目标语言，**不按 job
历史跳过**（历史会在译文被手动删改后过期，跳过会漏翻）。入队前清掉这些语言的旧
terminal job 记录；dedupe 唯一索引保证同一单元不会有重复 in-flight job（双击→skipped）。

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
    # Overwrite run: drop prior terminal (done/failed) records for these langs so
    # per-entity progress reflects this run and the table stays bounded. In-flight
    # rows survive → still deduped below.
    db.clear_terminal_translation_jobs(kind, entity_ref, target_codes, ep_number=ep_number)
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


def _other_lang_codes(default_lang: str) -> list[str]:
    """Every registered language except the source. One-click translate is
    overwrite-by-design, so we ALWAYS (re)translate all of them — never skip
    based on prior `done` jobs: that job history goes stale the moment a
    translation is edited or deleted out of band, which would wrongly skip
    languages that actually need (re)translating. In-flight duplicates are still
    deduped by the unique index in `enqueue_translation_job` (reported as
    `skipped`), so double-clicking won't double-enqueue."""
    return [c for c in _lang_codes() if c != default_lang]


_MODE_PATTERN = r"^(overwrite|missing)$"


def _apply_mode(targets: list[str], mode: str, have_langs: set[str]) -> list[str]:
    """`overwrite` → all targets (re)translated. `missing` → only languages that
    currently have NO translation content, so existing translations are never
    touched. "Missing" is decided from the ACTUAL stored content (`have_langs`),
    never from job history (which goes stale on manual edits/deletes)."""
    if mode == "missing":
        return [c for c in targets if c not in have_langs]
    return targets


@router.post("/admin/dramas/{drama_slug}/translate")
async def ai_translate_drama(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    """Enqueue per-language jobs translating the drama's name (+ synopsis) from
    its default_lang. `mode=overwrite` (re)does all other languages; `mode=missing`
    only fills languages that have no name translation yet."""
    _require_enabled()
    drama = db.get_drama(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    default_lang = drama["default_lang"]
    trans = db.list_drama_translations(drama_slug) or {}
    if not ((trans.get(default_lang) or {}).get("name") or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有剧名，无法翻译")
    have = {lang for lang, t in trans.items() if (t.get("name") or "").strip()}
    targets = _apply_mode(_other_lang_codes(default_lang), mode, have)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "mode": mode, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("drama", drama_slug, targets, source_lang=default_lang)
    log.info("enqueued drama translate slug=%s mode=%s enqueued=%d skipped=%d", drama_slug, mode, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "mode": mode, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/tags/translate")
async def ai_translate_all_tags(
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    """Library-level tag translation: for EVERY tag that has a default-language
    label, enqueue per-language jobs translating the label. Saves clicking each
    tag one by one. `overwrite` re-does all other languages per tag; `missing`
    only fills languages with no label yet. Tags lacking a default label are
    skipped (nothing to translate from). Declared BEFORE `/{slug}/translate` so
    the literal `translate` segment isn't captured as a slug."""
    _require_enabled()
    total_enqueued = 0
    total_skipped = 0
    tags_translated = 0      # tags that contributed >=1 freshly enqueued job
    tags_without_source = 0  # tags lacking a default-language label
    for tag in db.list_tags():
        slug = tag["slug"]
        default_lang = tag["default_lang"]
        labels = tag.get("translations") or {}  # {lang: label}, includes default
        if not (labels.get(default_lang) or "").strip():
            tags_without_source += 1
            continue
        have = {lang for lang, v in labels.items() if (v or "").strip()}
        targets = _apply_mode(_other_lang_codes(default_lang), mode, have)
        if not targets:
            continue
        enqueued, skipped = await _fanout("tag", slug, targets, source_lang=default_lang)
        total_enqueued += len(enqueued)
        total_skipped += len(skipped)
        if enqueued:
            tags_translated += 1
    log.info("enqueued all-tags translate mode=%s tags=%d no_src=%d enqueued=%d skipped=%d",
             mode, tags_translated, tags_without_source, total_enqueued, total_skipped)
    noop = total_enqueued == 0
    return JSONResponse({
        "ok": True, "mode": mode, "noop": noop,
        "tags_translated": tags_translated,
        "tags_without_source": tags_without_source,
        "enqueued": total_enqueued, "skipped": total_skipped,
    }, status_code=(200 if noop else 202))


@router.post("/admin/tags/{slug}/translate")
async def ai_translate_tag(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    _require_enabled()
    tag = db.get_tag(slug)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"tag '{slug}' not found")
    default_lang = tag["default_lang"]
    labels = db.list_translations_for_entity("tag", slug, "label")
    if not (labels.get(default_lang) or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有 label，无法翻译")
    have = {lang for lang, v in labels.items() if (v or "").strip()}
    targets = _apply_mode(_other_lang_codes(default_lang), mode, have)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "mode": mode, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("tag", slug, targets, source_lang=default_lang)
    log.info("enqueued tag translate slug=%s mode=%s enqueued=%d skipped=%d", slug, mode, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "mode": mode, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/actors/translate")
async def ai_translate_all_actors(
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    """Library-level actor translation: for EVERY actor that has a default-language
    name, enqueue per-language jobs translating the name. Saves clicking each actor
    one by one. `overwrite` re-does all other languages per actor; `missing` only
    fills languages with no name yet. Actors lacking a default name are skipped.
    Declared BEFORE `/{slug}/translate` so the literal `translate` segment isn't
    captured as a slug."""
    _require_enabled()
    total_enqueued = 0
    total_skipped = 0
    actors_translated = 0      # actors that contributed >=1 freshly enqueued job
    actors_without_source = 0  # actors lacking a default-language name
    for actor in db.list_actors():
        slug = actor["slug"]
        default_lang = actor["default_lang"]
        names = actor.get("translations") or {}  # {lang: name}, includes default
        if not (names.get(default_lang) or "").strip():
            actors_without_source += 1
            continue
        have = {lang for lang, v in names.items() if (v or "").strip()}
        targets = _apply_mode(_other_lang_codes(default_lang), mode, have)
        if not targets:
            continue
        enqueued, skipped = await _fanout("actor", slug, targets, source_lang=default_lang)
        total_enqueued += len(enqueued)
        total_skipped += len(skipped)
        if enqueued:
            actors_translated += 1
    log.info("enqueued all-actors translate mode=%s actors=%d no_src=%d enqueued=%d skipped=%d",
             mode, actors_translated, actors_without_source, total_enqueued, total_skipped)
    noop = total_enqueued == 0
    return JSONResponse({
        "ok": True, "mode": mode, "noop": noop,
        "actors_translated": actors_translated,
        "actors_without_source": actors_without_source,
        "enqueued": total_enqueued, "skipped": total_skipped,
    }, status_code=(200 if noop else 202))


@router.post("/admin/actors/{slug}/translate")
async def ai_translate_actor(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    _require_enabled()
    actor = db.get_actor(slug)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor '{slug}' not found")
    default_lang = actor["default_lang"]
    names = db.list_translations_for_entity("actor", slug, "name")
    if not (names.get(default_lang) or "").strip():
        raise HTTPException(status_code=400, detail=f"默认语言 '{default_lang}' 还没有 name，无法翻译")
    have = {lang for lang, v in names.items() if (v or "").strip()}
    targets = _apply_mode(_other_lang_codes(default_lang), mode, have)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "mode": mode, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout("actor", slug, targets, source_lang=default_lang)
    log.info("enqueued actor translate slug=%s mode=%s enqueued=%d skipped=%d", slug, mode, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "mode": mode, "enqueued": enqueued, "skipped": skipped}, status_code=202)


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
    mode = (payload.get("mode") or "overwrite").strip()
    if mode not in ("overwrite", "missing"):
        raise HTTPException(status_code=400, detail="mode 必须是 overwrite 或 missing")
    requested = payload.get("targets")
    if requested is not None:
        if not isinstance(requested, list) or not all(isinstance(c, str) for c in requested):
            raise HTTPException(status_code=400, detail="targets 必须是语言代码字符串数组")
        bad = [c for c in requested if c not in all_codes]
        if bad:
            raise HTTPException(status_code=400, detail=f"未注册的目标语言: {bad}")
        targets = [c for c in requested if c != source_lang]
    else:
        targets = [c for c in all_codes if c != source_lang]
    # `missing` → only languages that have no subtitle yet (existing subtitles
    # untouched); `overwrite` → all requested targets. "Have" comes from the
    # actual subtitle rows, not job history.
    have = {r["lang_code"] for r in db.list_subtitles_for_slug_ep(drama_slug, ep_number)}
    targets = _apply_mode(targets, mode, have)
    if not targets:
        return JSONResponse({"ok": True, "noop": True, "mode": mode, "enqueued": [], "skipped": []})
    enqueued, skipped = await _fanout(
        "subtitle", drama_slug, targets, ep_number=ep_number, source_lang=source_lang,
    )
    log.info("enqueued subtitle translate slug=%s ep=%s src=%s mode=%s enqueued=%d skipped=%d",
             drama_slug, ep_number, source_lang, mode, len(enqueued), len(skipped))
    return JSONResponse({"ok": True, "mode": mode, "enqueued": enqueued, "skipped": skipped}, status_code=202)


@router.post("/admin/dramas/{drama_slug}/subtitles/translate")
async def ai_translate_drama_subtitles(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    mode: str = Query("overwrite", pattern=_MODE_PATTERN),
) -> JSONResponse:
    """Drama-level subtitle translation: for EVERY episode that has a
    default-language source subtitle, enqueue per-language subtitle jobs (source =
    drama `default_lang`). Saves opening each episode one by one. `overwrite`
    re-does all other languages per episode; `missing` only fills languages that
    have no subtitle yet for that episode. Episodes with no default-language
    subtitle are skipped (nothing to translate from)."""
    _require_enabled()
    drama = db.get_drama(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    source_lang = drama["default_lang"]
    other = _other_lang_codes(source_lang)

    total_enqueued = 0
    total_skipped = 0
    eps_translated = 0      # episodes that contributed >=1 freshly enqueued job
    eps_without_source = 0  # episodes lacking a default-language subtitle to translate from
    for ep_number in db.list_episode_numbers(drama_slug):
        if not ai_jobs._subtitle_path(drama_slug, ep_number, source_lang).is_file():
            eps_without_source += 1
            continue
        have = {r["lang_code"] for r in db.list_subtitles_for_slug_ep(drama_slug, ep_number)}
        targets = _apply_mode(other, mode, have)
        if not targets:
            continue
        enqueued, skipped = await _fanout(
            "subtitle", drama_slug, targets, ep_number=ep_number, source_lang=source_lang,
        )
        total_enqueued += len(enqueued)
        total_skipped += len(skipped)
        if enqueued:
            eps_translated += 1
    log.info(
        "enqueued drama subtitle translate slug=%s mode=%s eps_translated=%d "
        "eps_no_source=%d enqueued=%d skipped=%d",
        drama_slug, mode, eps_translated, eps_without_source, total_enqueued, total_skipped,
    )
    noop = total_enqueued == 0
    return JSONResponse({
        "ok": True, "mode": mode, "noop": noop,
        "episodes_translated": eps_translated,
        "episodes_without_source": eps_without_source,
        "enqueued": total_enqueued, "skipped": total_skipped,
    }, status_code=(200 if noop else 202))


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


@router.get("/admin/translations/progress")
async def translation_progress(
    kind: str = Query(..., pattern=r"^(drama|tag|actor|subtitle)$"),
    entity_ref: str | None = Query(None, pattern=_SLUG_PATTERN),
    ep_number: int | None = Query(None),
) -> JSONResponse:
    """Job counts `{queued, running, done, failed}` — polled after enqueue to
    refresh as jobs complete. `active = queued + running == 0` means the batch
    has settled. With `entity_ref` → that entity (optionally scoped to one
    `ep_number`); WITHOUT `entity_ref` → aggregate across ALL entities of `kind`
    (drives library-wide "translate all" progress, e.g. all tags)."""
    if entity_ref is None:
        return JSONResponse(db.kind_translation_progress(kind))
    return JSONResponse(db.entity_translation_progress(kind, entity_ref, ep_number))


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
    dedupe-protected) and drop the old `failed` rows. Without the delete the
    stale failure would linger in the overview / nav badge even after the retry
    succeeds — the retry produces a separate `done` row, never clearing the old
    `failed` one."""
    _require_enabled()
    requeued = 0
    for j in db.list_translation_jobs(statuses=("failed",), limit=1000):
        job_id = db.enqueue_translation_job(
            j["kind"], j["entity_ref"], j["target_lang"],
            ep_number=j["ep_number"], source_lang=j["source_lang"],
        )
        # Drop the old failed row whether the enqueue created a fresh job or was
        # suppressed by an in-flight dup — either way a live job now covers this
        # unit, so the failed record is stale.
        db.delete_translation_job(j["id"])
        if job_id is not None:
            await ai_jobs.enqueue(job_id)
            requeued += 1
    log.info("re-enqueued %d failed translation job(s)", requeued)
    return JSONResponse({"ok": True, "requeued": requeued})
