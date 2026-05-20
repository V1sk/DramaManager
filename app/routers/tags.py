"""Tag library: admin CRUD + per-tag translation upserts + drama–tag assignment.

All translation rows live in the generic `translations` table under
`entity_type='tag'`, `field='label'`. The drama–tag many-to-many lives in
`drama_tags` with FK CASCADE on both sides.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..auth import require_can_delete

router = APIRouter()
log = logging.getLogger("hls.tags")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_LANG_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"


@router.get("/admin/tags", response_class=HTMLResponse)
async def tags_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "tags.html", {"nav_active": "tags"})


@router.get("/admin/tags.json")
async def tags_list_json() -> JSONResponse:
    rows = db.list_tags()
    return JSONResponse(rows)


@router.post("/admin/tags")
async def tags_create(
    slug: str = Form(...),
    default_lang: str = Form(...),
    label: str = Form(...),
) -> RedirectResponse:
    try:
        db.create_tag(slug=slug, default_lang=default_lang, label=label)
    except db.TagValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.TagExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info("created tag slug=%s default_lang=%s", slug, default_lang)
    return RedirectResponse(url="/admin/tags", status_code=302)


@router.patch("/admin/tags/{slug}")
async def tags_patch(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    extra = set(payload.keys()) - {"default_lang"}
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"unknown / immutable fields: {sorted(extra)}",
        )
    new_default = payload.get("default_lang")
    if not new_default:
        raise HTTPException(status_code=400, detail="default_lang is required")
    try:
        row = db.update_tag_default_lang(slug, new_default)
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.TagDefaultLangNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail=f"tag '{slug}' not found")
    db.cascade_dirty_dramas_via_tag(slug)
    log.info("updated tag default_lang slug=%s default_lang=%s", slug, new_default)
    return JSONResponse(row)


@router.delete("/admin/tags/{slug}", dependencies=[Depends(require_can_delete)])
async def tags_delete(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> Response:
    deleted = db.delete_tag(slug)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"tag '{slug}' not found")
    log.info("deleted tag slug=%s", slug)
    return Response(status_code=204)


@router.put("/admin/tags/{slug}/translations/{lang_code}")
async def tags_translation_upsert(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    label = payload.get("label") if isinstance(payload, dict) else None
    if not isinstance(label, str):
        raise HTTPException(status_code=400, detail="body must contain `label` string")
    try:
        result = db.upsert_tag_translation(slug, lang_code, label)
    except db.TagNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.TagValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"lang_code: {e}")
    except db.LanguageInactiveError as e:
        raise HTTPException(status_code=400, detail=f"lang_code: {e}")
    db.cascade_dirty_dramas_via_tag(slug)
    log.info("upserted tag translation slug=%s lang=%s", slug, lang_code)
    return JSONResponse(result)


@router.delete("/admin/tags/{slug}/translations/{lang_code}")
async def tags_translation_delete(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
) -> Response:
    try:
        deleted = db.delete_tag_translation(slug, lang_code)
    except db.TagNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.TagDefaultTranslationProtectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        # tag exists, no row for this lang
        raise HTTPException(
            status_code=404,
            detail=f"tag '{slug}' has no translation in '{lang_code}'",
        )
    db.cascade_dirty_dramas_via_tag(slug)
    log.info("deleted tag translation slug=%s lang=%s", slug, lang_code)
    return Response(status_code=204)


@router.put("/admin/dramas/{drama_slug}/tags")
async def drama_tags_replace(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    payload: list = Body(...),
) -> JSONResponse:
    if not isinstance(payload, list) or not all(isinstance(s, str) for s in payload):
        raise HTTPException(status_code=400, detail="body must be a JSON array of strings")
    try:
        db.replace_drama_tags(drama_slug, payload)
    except db.DramaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.TagNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.mark_drama_dirty(drama_slug)
    log.info("replaced drama tags drama=%s tags=%s", drama_slug, payload)
    rows = db.list_drama_tags(drama_slug)
    return JSONResponse(rows)


@router.get("/admin/dramas/{drama_slug}/tags")
async def drama_tags_list(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    if db.get_drama(drama_slug) is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    return JSONResponse(db.list_drama_tags(drama_slug))
