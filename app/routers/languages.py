"""Languages registry: admin CRUD + minimal HTML page.

Underpins step 3a/3b/3c/3d (tags, actors, drama metadata, subtitles) which
all reference `languages.code` via the generic `translations` table.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..auth import require_can_delete

router = APIRouter()
log = logging.getLogger("hls.languages")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/admin/languages", response_class=HTMLResponse)
async def languages_page(request: Request) -> HTMLResponse:
    """HTML page — accessed via `Accept: text/html`. JSON callers should use
    the same path; FastAPI dispatches based on Accept header negotiation.

    To keep things simple we serve HTML from this exact path and the JSON
    version from `/admin/languages.json`. Browsers GET the page; admin JS
    fetches `/admin/languages.json` to populate the table.
    """
    return _TEMPLATES.TemplateResponse(request, "languages.html", {"nav_active": "languages"})


@router.get("/admin/languages.json")
async def languages_list_json() -> JSONResponse:
    rows = db.list_languages(active_only=False)
    return JSONResponse([
        {
            "code": r["code"],
            "display_label": r["display_label"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ])


@router.post("/admin/languages")
async def languages_create(
    code: str = Form(...),
    display_label: str = Form(...),
) -> RedirectResponse:
    try:
        db.create_language(code=code, display_label=display_label)
    except db.LanguageValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LanguageExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info("created language code=%s label=%s", code, display_label.strip())
    return RedirectResponse(url="/admin/languages", status_code=302)


_ALLOWED_PATCH_FIELDS = {"display_label", "is_active"}


@router.patch("/admin/languages/{code}")
async def languages_patch(
    code: str = PathParam(..., pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    unknown = set(payload.keys()) - _ALLOWED_PATCH_FIELDS
    if unknown:
        # Specifically, `code` is in this set so attempts to rename via PATCH
        # are rejected with a clear message.
        raise HTTPException(
            status_code=400,
            detail=f"unknown / immutable fields: {sorted(unknown)}",
        )

    try:
        row = db.update_language(
            code,
            display_label=payload.get("display_label"),
            is_active=payload.get("is_active"),
        )
    except db.LanguageValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"language '{code}' not found")
    # display_label change → cascade dirty (subtitle pickers show this label).
    # is_active toggle → no cascade (existing payloads are unchanged; the
    # business server keeps serving the same label).
    if "display_label" in payload:
        db.cascade_dirty_dramas_via_language(code)
    log.info("updated language code=%s payload=%s", code, payload)
    return JSONResponse({
        "code": row["code"],
        "display_label": row["display_label"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })


@router.delete("/admin/languages/{code}", dependencies=[Depends(require_can_delete)])
async def languages_delete(
    code: str = PathParam(..., pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"),
) -> JSONResponse:
    deleted, refs = db.delete_language(code)
    if not deleted and refs["dramas"] == 0 and refs["translations"] == 0:
        # Pre-check found no row at all
        raise HTTPException(status_code=404, detail=f"language '{code}' not found")
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "language is referenced",
                "dramas": refs["dramas"],
                "translations": refs["translations"],
            },
        )
    log.info("deleted language code=%s", code)
    return Response(status_code=204)
