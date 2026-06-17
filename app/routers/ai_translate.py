"""AI 翻译路由（短文本 + 字幕）。

短文本：把剧名 + 简介、标签 label、演员 name 从各自的 `default_lang` 一次性翻译到
其余全部已注册语言，**直接覆盖**已有译文（产品决策：不区分人工 / AI，不留 origin
标记）。落库走现有 `upsert_*_translation` + `mark_*_dirty` 路径。

字幕：集详情页从某条已有字幕（源语言）逐 cue 翻译成一个目标语言，保留时间轴/标识，
分块 + 等长校验后落盘（VTT 解析见 `app/vtt.py`），复用字幕上传的写盘 + staging +
`upsert_subtitle` + `mark_episode_dirty` 路径。

两者都天然进入 staging → 手动同步流程；sync 链路无需改动。

仅当 `settings.ai_translate_enabled`（即 `AI_TRANSLATE_API_KEY` 已设）时这些端点
可用，否则返回 503。鉴权沿用 `/admin` 路由的 `require_user`（与手工编辑翻译同级，
不额外要求 can_sync —— 翻译只写 staging，不推 prod）。
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse

from .. import ai_translate_client, db, vtt
from ..ai_translate_client import AITranslateError
from ..config import settings

router = APIRouter()
log = logging.getLogger("hls.ai_translate_router")

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_EP_PATTERN = r"^[0-9]+$"
# Cues per AI call. Smaller chunks keep the equal-length contract easier for the
# model to honor and bound each request's latency/size; subtitles can be large.
_SUBTITLE_CHUNK = 40


def _require_enabled() -> None:
    if not settings.ai_translate_enabled:
        raise HTTPException(
            status_code=503,
            detail="AI 翻译未启用（未配置 AI_TRANSLATE_API_KEY）",
        )


def _lang_label_map() -> dict[str, str]:
    """`{code: display_label}` over every registered language."""
    return {lang["code"]: lang["display_label"] for lang in db.list_languages()}


def _build_targets(all_langs: dict[str, str], default_lang: str) -> list[dict[str, str]]:
    """Every registered language except the source — the set we translate into."""
    return [
        {"code": code, "label": label}
        for code, label in all_langs.items()
        if code != default_lang
    ]


def _source_label(all_langs: dict[str, str], default_lang: str) -> str:
    label = all_langs.get(default_lang)
    return f"{label} ({default_lang})" if label else default_lang


@router.post("/admin/dramas/{drama_slug}/translate")
async def ai_translate_drama(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    """Translate the drama's default-lang name (+ synopsis if present) into every
    other registered language, overwriting existing rows. Marks the drama dirty.
    """
    _require_enabled()
    drama = db.get_drama(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    default_lang = drama["default_lang"]

    translations = db.list_drama_translations(drama_slug)
    src = translations.get(default_lang) or {}
    name = (src.get("name") or "").strip()
    synopsis = (src.get("synopsis") or "").strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail=f"默认语言 '{default_lang}' 还没有剧名，无法翻译",
        )

    all_langs = _lang_label_map()
    targets = _build_targets(all_langs, default_lang)
    if not targets:
        return JSONResponse(
            {"ok": True, "noop": True, "translated_langs": [], "errors": [],
             "detail": "没有其它已注册语言"}
        )

    fields = {"name": name}
    if synopsis:
        fields["synopsis"] = synopsis
    try:
        result = await ai_translate_client.translate_texts(
            source_lang_label=_source_label(all_langs, default_lang),
            targets=targets,
            fields=fields,
        )
    except AITranslateError as e:
        log.warning("ai-translate failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    translated: list[str] = []
    errors: list[str] = []
    for code, fmap in result.items():
        t_name = (fmap.get("name") or "").strip()
        t_syn = (fmap.get("synopsis") or "").strip() or None
        if not t_name:
            # name is required to (re)populate a language; skip when the model
            # omitted it rather than writing a half row.
            errors.append(f"{code}: 缺少剧名翻译，已跳过")
            continue
        try:
            db.upsert_drama_translation(drama_slug, code, name=t_name, synopsis=t_syn)
            translated.append(code)
        except (
            db.DramaValidationError,
            db.LanguageNotFoundError,
            db.DramaTranslationFreshNameRequiredError,
            db.DramaNotFoundError,
        ) as ex:
            errors.append(f"{code}: {ex}")

    if translated:
        db.mark_drama_dirty(drama_slug)
    log.info(
        "ai-translated drama=%s langs=%s errors=%d",
        drama_slug, translated, len(errors),
    )
    return JSONResponse({"ok": True, "translated_langs": translated, "errors": errors})


@router.post("/admin/tags/{slug}/translate")
async def ai_translate_tag(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    """Translate a tag's default-lang label into every other registered language,
    overwriting. Cascades dirty to dramas referencing the tag."""
    _require_enabled()
    tag = db.get_tag(slug)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"tag '{slug}' not found")
    default_lang = tag["default_lang"]

    labels = db.list_translations_for_entity("tag", slug, "label")
    src_label = (labels.get(default_lang) or "").strip()
    if not src_label:
        raise HTTPException(
            status_code=400,
            detail=f"默认语言 '{default_lang}' 还没有 label，无法翻译",
        )

    all_langs = _lang_label_map()
    targets = _build_targets(all_langs, default_lang)
    if not targets:
        return JSONResponse(
            {"ok": True, "noop": True, "translated_langs": [], "errors": []}
        )
    try:
        result = await ai_translate_client.translate_texts(
            source_lang_label=_source_label(all_langs, default_lang),
            targets=targets,
            fields={"label": src_label},
        )
    except AITranslateError as e:
        log.warning("ai-translate failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    translated: list[str] = []
    errors: list[str] = []
    for code, fmap in result.items():
        label = (fmap.get("label") or "").strip()
        if not label:
            continue
        try:
            db.upsert_tag_translation(slug, code, label)
            translated.append(code)
        except (db.TagValidationError, db.LanguageNotFoundError, db.TagNotFoundError) as ex:
            errors.append(f"{code}: {ex}")

    if translated:
        db.cascade_dirty_dramas_via_tag(slug)
    log.info("ai-translated tag=%s langs=%s errors=%d", slug, translated, len(errors))
    return JSONResponse({"ok": True, "translated_langs": translated, "errors": errors})


@router.post("/admin/actors/{slug}/translate")
async def ai_translate_actor(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    """Translate an actor's default-lang name into every other registered language,
    overwriting. Cascades dirty to dramas referencing the actor."""
    _require_enabled()
    actor = db.get_actor(slug)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor '{slug}' not found")
    default_lang = actor["default_lang"]

    names = db.list_translations_for_entity("actor", slug, "name")
    src_name = (names.get(default_lang) or "").strip()
    if not src_name:
        raise HTTPException(
            status_code=400,
            detail=f"默认语言 '{default_lang}' 还没有 name，无法翻译",
        )

    all_langs = _lang_label_map()
    targets = _build_targets(all_langs, default_lang)
    if not targets:
        return JSONResponse(
            {"ok": True, "noop": True, "translated_langs": [], "errors": []}
        )
    try:
        result = await ai_translate_client.translate_texts(
            source_lang_label=_source_label(all_langs, default_lang),
            targets=targets,
            fields={"name": src_name},
        )
    except AITranslateError as e:
        log.warning("ai-translate failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    translated: list[str] = []
    errors: list[str] = []
    for code, fmap in result.items():
        value = (fmap.get("name") or "").strip()
        if not value:
            continue
        try:
            db.upsert_actor_translation(slug, code, value)
            translated.append(code)
        except (db.ActorValidationError, db.LanguageNotFoundError, db.ActorNotFoundError) as ex:
            errors.append(f"{code}: {ex}")

    if translated:
        db.cascade_dirty_dramas_via_actor(slug)
    log.info("ai-translated actor=%s langs=%s errors=%d", slug, translated, len(errors))
    return JSONResponse({"ok": True, "translated_langs": translated, "errors": errors})


# ---------------------------------------------------------------------------
# 字幕 AI 翻译（集详情页）：从某条已有字幕（源语言）逐 cue 翻译成一个目标语言，
# 保留时间轴/标识，分块 + 等长校验后落盘。写盘 + staging + upsert + mark dirty
# 复用 admin.py 的字幕上传路径形态；字幕固定在 v1 目录 `ep-{n}`（不版本化）。
# 一次请求只翻一个目标语言以约束时延，前端按需循环多语言。
# ---------------------------------------------------------------------------


def _subtitle_path(drama_slug: str, ep_number: int, lang_code: str) -> Path:
    return settings.out_dir / drama_slug / f"ep-{ep_number}" / "subtitles" / f"{lang_code}.vtt"


def _subtitle_url(drama_slug: str, ep_number: int, lang_code: str) -> str:
    return f"/videos/{drama_slug}/ep-{ep_number}/subtitles/{lang_code}.vtt"


async def _translate_cue_texts(
    source_label: str, target_label: str, texts: list[str]
) -> list[str]:
    """Chunked cue translation with one retry per chunk on count mismatch /
    transient error. A persistent failure propagates (route → 502)."""
    out: list[str] = []
    for i in range(0, len(texts), _SUBTITLE_CHUNK):
        chunk = texts[i : i + _SUBTITLE_CHUNK]
        try:
            res = await ai_translate_client.translate_lines(
                source_lang_label=source_label, target_lang_label=target_label, lines=chunk,
            )
        except AITranslateError:
            # one retry; if it fails again the exception propagates
            res = await ai_translate_client.translate_lines(
                source_lang_label=source_label, target_lang_label=target_label, lines=chunk,
            )
        out.extend(res)
    return out


@router.post("/admin/episodes/{drama_slug}/{ep}/subtitles/translate")
async def ai_translate_subtitle(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    ep: str = PathParam(..., pattern=_EP_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    """Translate one episode subtitle (source_lang) into one target_lang, cue by
    cue, preserving timestamps. Overwrites the target-lang subtitle if present.
    """
    _require_enabled()
    ep_number = int(ep)
    if ep_number < 1:
        raise HTTPException(status_code=422, detail="ep must be >= 1")
    ep_row = db.get_by_slug_ep(drama_slug, ep_number)
    if ep_row is None:
        raise HTTPException(status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    source_lang = (payload.get("source_lang") or "").strip()
    target_lang = (payload.get("target_lang") or "").strip()
    if not source_lang or not target_lang:
        raise HTTPException(status_code=400, detail="source_lang 和 target_lang 必填")
    if source_lang == target_lang:
        raise HTTPException(status_code=400, detail="source_lang 与 target_lang 不能相同")
    all_langs = _lang_label_map()
    if target_lang not in all_langs:
        raise HTTPException(status_code=400, detail=f"target_lang '{target_lang}' 不是已注册语言")

    src_path = _subtitle_path(drama_slug, ep_number, source_lang)
    if not src_path.is_file():
        raise HTTPException(status_code=400, detail=f"源语言 '{source_lang}' 的字幕不存在")
    try:
        src_text = src_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取源字幕失败：{e}")

    blocks, texts = vtt.parse_cues(src_text)
    if not texts:
        raise HTTPException(status_code=400, detail="源字幕没有可翻译的 cue")

    try:
        translated = await _translate_cue_texts(
            _source_label(all_langs, source_lang),
            _source_label(all_langs, target_lang),
            texts,
        )
    except AITranslateError as e:
        log.warning("ai-translate subtitle failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    if len(translated) != len(texts):
        raise HTTPException(
            status_code=502,
            detail=f"字幕翻译条数不匹配（{len(translated)} vs {len(texts)}），未写入",
        )

    vtt_bytes = vtt.rebuild(blocks, translated).encode("utf-8")
    if not vtt_bytes.startswith(b"WEBVTT"):
        # Defensive — parse_cues preserves the WEBVTT header block, so this only
        # triggers on a source that wasn't valid VTT to begin with.
        vtt_bytes = b"WEBVTT\n\n" + vtt_bytes

    target_path = _subtitle_path(drama_slug, ep_number, target_lang)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_path.write_bytes(vtt_bytes)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入字幕文件失败：{e}")

    if settings.storage_enabled:
        from .. import publish
        ep_dir = f"ep-{ep_number}"
        try:
            await asyncio.to_thread(
                publish.upload_subtitle_to_staging, drama_slug, ep_dir, target_lang, target_path,
            )
        except Exception as e:  # noqa: BLE001 — unwind local write on publish failure
            target_path.unlink(missing_ok=True)
            log.error("OSS staging upload failed for ai subtitle %s/%s/%s: %s",
                      drama_slug, ep_dir, target_lang, e)
            raise HTTPException(status_code=500, detail=f"字幕上传到 OSS staging 失败：{e}")

    file_url = _subtitle_url(drama_slug, ep_number, target_lang)
    db.upsert_subtitle(ep_row["episode_id"], target_lang, file_url)
    db.mark_episode_dirty(drama_slug, ep_number)
    log.info("ai-translated subtitle slug=%s ep=%s %s->%s cues=%d",
             drama_slug, ep_number, source_lang, target_lang, len(texts))
    return JSONResponse({
        "ok": True,
        "lang_code": target_lang,
        "label": all_langs.get(target_lang, target_lang),
        "url": file_url,
        "cues": len(texts),
    })
