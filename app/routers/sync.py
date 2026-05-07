"""Admin-facing sync endpoints. Three buckets:

- Action: `POST /admin/dramas/{slug}/sync`, `POST /admin/episodes/{slug}/{ep}/sync`.
  Validate, transition the row to `syncing` (or preserve `pending_delete`),
  enqueue a job, return 202 with the row.

- Overview: `GET /admin/sync` (HTML page) and `GET /admin/sync/summary`
  (JSON `{non_clean_count: N}`) so the nav-bar can poll.

When `BUSINESS_SYNC_BASE_URL` is unset, action endpoints return 503 and the
overview page renders a "sync disabled" notice. The summary endpoint still
returns 0 so polling JS doesn't error.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import db, sync as sync_module
from ..config import settings

router = APIRouter()
log = logging.getLogger("hls.sync_router")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _sync_disabled_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "business sync is disabled (BUSINESS_SYNC_BASE_URL not set); "
            "action unavailable in this deployment"
        ),
    )


@router.post("/admin/dramas/{drama_slug}/sync")
async def sync_drama(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> JSONResponse:
    if not settings.business_sync_base_url:
        raise _sync_disabled_503()
    drama = db.get_drama_with_sync(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")

    children_pending = db.list_episodes_needing_sync(drama_slug)
    if drama["sync_status"] == "clean" and not children_pending:
        # No-op: nothing to push.
        return JSONResponse(
            {
                "ok": True,
                "noop": True,
                "drama": drama,
            },
            status_code=200,
        )

    if drama["sync_status"] == "pending_delete":
        # Preserve pending_delete intent; the worker branches on it.
        pass
    else:
        db.set_drama_sync_status(drama_slug, "syncing")

    await sync_module.enqueue_drama(drama_slug)
    log.info(
        "enqueued drama sync slug=%s prior_status=%s children=%d",
        drama_slug, drama["sync_status"], len(children_pending),
    )
    return JSONResponse(
        {
            "ok": True,
            "noop": False,
            "drama": db.get_drama_with_sync(drama_slug),
            "child_episodes_to_sync": children_pending,
        },
        status_code=202,
    )


@router.post("/admin/episodes/{drama_slug}/{ep}/sync")
async def sync_episode(
    drama_slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    ep: str = PathParam(..., pattern=r"^[0-9]+$"),
) -> JSONResponse:
    if not settings.business_sync_base_url:
        raise _sync_disabled_503()
    ep_number = int(ep)
    drama = db.get_drama_with_sync(drama_slug)
    if drama is None:
        raise HTTPException(status_code=404, detail=f"drama '{drama_slug}' not found")
    row = db.get_by_slug_ep(drama_slug, ep_number)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"episode '{drama_slug}/{ep_number}' not found",
        )
    if drama["last_synced_at"] is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "drama has never been synced; sync the whole drama first via "
                f"POST /admin/dramas/{drama_slug}/sync"
            ),
        )

    if row["sync_status"] == "clean":
        return JSONResponse(
            {"ok": True, "noop": True, "episode": row}, status_code=200,
        )

    if row["sync_status"] != "pending_delete":
        db.set_episode_sync_status(drama_slug, ep_number, "syncing")

    await sync_module.enqueue_episode(drama_slug, ep_number)
    log.info(
        "enqueued episode sync slug=%s ep=%s prior_status=%s",
        drama_slug, ep_number, row["sync_status"],
    )
    return JSONResponse(
        {
            "ok": True,
            "noop": False,
            "episode": db.get_by_slug_ep(drama_slug, ep_number),
        },
        status_code=202,
    )


@router.get("/admin/sync", response_class=HTMLResponse)
async def sync_overview(request: Request) -> HTMLResponse:
    """Overview page listing every non-clean drama / episode."""
    dramas = db.list_dramas_needing_sync()
    episodes = db.list_episodes_needing_sync_all()
    return _TEMPLATES.TemplateResponse(
        request,
        "sync.html",
        {
            "dramas": dramas,
            "episodes": episodes,
            "sync_enabled": bool(settings.business_sync_base_url),
            "nav_active": "sync",
        },
    )


@router.get("/admin/sync/summary")
async def sync_summary() -> JSONResponse:
    """Lightweight JSON polled by the nav bar's sync zone."""
    if not settings.business_sync_base_url:
        return JSONResponse(
            {"enabled": False, "non_clean_count": 0},
        )
    return JSONResponse(
        {
            "enabled": True,
            "non_clean_count": db.count_non_clean_sync_rows(),
        },
    )
