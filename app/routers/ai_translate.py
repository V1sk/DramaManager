"""AI 短文本翻译路由。

把剧名 + 简介、标签 label、演员 name 从各自的 `default_lang` 一次性翻译到其余
全部已注册语言，**直接覆盖**已有译文（产品决策：不区分人工 / AI，不留 origin 标记）。
落库走现有 `upsert_*_translation` + `mark_*_dirty` 路径，天然进入 staging → 手动
同步流程；sync 链路无需改动。

仅当 `settings.ai_translate_enabled`（即 `AI_TRANSLATE_API_KEY` 已设）时这些端点
可用，否则返回 503。鉴权沿用 `/admin` 路由的 `require_user`（与手工编辑翻译同级，
不额外要求 can_sync —— 翻译只写 staging，不推 prod）。
"""
import logging

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse

from .. import ai_translate_client, db
from ..ai_translate_client import AITranslateError
from ..config import settings

router = APIRouter()
log = logging.getLogger("hls.ai_translate_router")

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


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
