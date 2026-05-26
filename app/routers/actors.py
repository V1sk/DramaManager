"""Actor library: structural mirror of `tags.py`. See that file's docstring
for the rationale; only `entity_type='actor'`, `field='name'`, and the
junction table differ.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..auth import require_can_delete

router = APIRouter()
log = logging.getLogger("hls.actors")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_LANG_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"


@router.get("/admin/actors", response_class=HTMLResponse)
async def actors_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "actors.html", {"nav_active": "actors"})


@router.get("/admin/actors.json")
async def actors_list_json() -> JSONResponse:
    rows = db.list_actors()
    return JSONResponse(rows)


@router.post("/admin/actors")
async def actors_create(
    slug: str = Form(...),
    default_lang: str = Form(...),
    name: str = Form(...),
) -> RedirectResponse:
    try:
        db.create_actor(slug=slug, default_lang=default_lang, name=name)
    except db.ActorValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.ActorExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info("created actor slug=%s default_lang=%s", slug, default_lang)
    return RedirectResponse(url="/admin/actors", status_code=302)


@router.patch("/admin/actors/{slug}")
async def actors_patch(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    extra = set(payload.keys()) - {"default_lang"}
    if extra:
        raise HTTPException(status_code=400, detail=f"unknown / immutable fields: {sorted(extra)}")
    new_default = payload.get("default_lang")
    if not new_default:
        raise HTTPException(status_code=400, detail="default_lang is required")
    try:
        row = db.update_actor_default_lang(slug, new_default)
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"default_lang: {e}")
    except db.ActorDefaultLangNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail=f"actor '{slug}' not found")
    db.cascade_dirty_dramas_via_actor(slug)
    log.info("updated actor default_lang slug=%s default_lang=%s", slug, new_default)
    return JSONResponse(row)


@router.delete("/admin/actors/{slug}", dependencies=[Depends(require_can_delete)])
async def actors_delete(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> Response:
    deleted = db.delete_actor(slug)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"actor '{slug}' not found")
    log.info("deleted actor slug=%s", slug)
    return Response(status_code=204)


@router.put("/admin/actors/{slug}/translations/{lang_code}")
async def actors_translation_upsert(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
    payload: dict = Body(...),
) -> JSONResponse:
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="body must contain `name` string")
    try:
        result = db.upsert_actor_translation(slug, lang_code, name)
    except db.ActorNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.ActorValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"lang_code: {e}")
    db.cascade_dirty_dramas_via_actor(slug)
    log.info("upserted actor translation slug=%s lang=%s", slug, lang_code)
    return JSONResponse(result)


@router.delete("/admin/actors/{slug}/translations/{lang_code}")
async def actors_translation_delete(
    slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    lang_code: str = PathParam(..., pattern=_LANG_PATTERN),
) -> Response:
    try:
        deleted = db.delete_actor_translation(slug, lang_code)
    except db.ActorNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.ActorDefaultTranslationProtectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"actor '{slug}' has no translation in '{lang_code}'",
        )
    db.cascade_dirty_dramas_via_actor(slug)
    log.info("deleted actor translation slug=%s lang=%s", slug, lang_code)
    return Response(status_code=204)


@router.put("/admin/dramas/{drama_slug}/actors")
async def drama_actors_replace(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
    payload: list = Body(...),
) -> JSONResponse:
    if not isinstance(payload, list) or not all(isinstance(s, str) for s in payload):
        raise HTTPException(status_code=400, detail="body must be a JSON array of strings")
    try:
        db.replace_drama_actors(drama_slug, payload)
    except db.DramaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except db.ActorNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.mark_drama_dirty(drama_slug)
    log.info("replaced drama actors drama=%s actors=%s", drama_slug, payload)
    rows = db.list_drama_actors(drama_slug)
    return JSONResponse(rows)


@router.get("/admin/dramas/{drama_slug}/actors")
async def drama_actors_list(
    drama_slug: str = PathParam(..., pattern=_SLUG_PATTERN),
) -> JSONResponse:
    if db.get_drama(drama_slug) is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    return JSONResponse(db.list_drama_actors(drama_slug))
