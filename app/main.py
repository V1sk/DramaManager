import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .queue import worker_loop
from .routers import actors, admin, api, drm, languages, sync as sync_router, tags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hls")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    reaped = db.reap_orphaned_encoding()
    if reaped:
        log.warning("startup: flipped %d orphaned encoding row(s) to failed", reaped)
    sync_reaped = db.reap_orphaned_syncing()
    if sync_reaped:
        log.warning(
            "startup: flipped %d orphaned syncing row(s) to sync_failed", sync_reaped,
        )

    worker_task = asyncio.create_task(worker_loop(), name="pipeline-worker")
    sync_task = None
    if settings.business_sync_base_url:
        from . import sync as sync_module, sync_client
        await sync_client.startup()
        sync_task = asyncio.create_task(
            sync_module.sync_worker_loop(), name="sync-worker",
        )
        log.info(
            "lifespan up: business-sync enabled; base=%s timeout=%ds",
            settings.business_sync_base_url, settings.business_sync_timeout,
        )
    else:
        log.info("lifespan up: business-sync disabled (BUSINESS_SYNC_BASE_URL unset)")

    if settings.oss_enabled:
        from .oss_upload import oss_prod_public_base_url, oss_staging_public_base_url
        log.info(
            "lifespan up: out_dir=%s db=%s oss_enabled=True staging=%s prod=%s",
            settings.out_dir, settings.db_path,
            oss_staging_public_base_url, oss_prod_public_base_url,
        )
    else:
        log.info(
            "lifespan up: out_dir=%s db=%s oss_enabled=False",
            settings.out_dir, settings.db_path,
        )
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            from . import sync_client
            await sync_client.shutdown()


app = FastAPI(title="HLS Management Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def block_keys_via_videos(request: Request, call_next):
    """Deny /videos/{slug}/keys/** so the key directory never leaks via the
    static mount. Key material is served only through /drm/... .
    """
    path = request.url.path
    if path.startswith("/videos/"):
        parts = path.split("/")
        # parts = ['', 'videos', slug, rest...]
        if len(parts) >= 4 and parts[3] == "keys":
            return Response(status_code=404)
    return await call_next(request)


app.include_router(admin.router)
app.include_router(api.router)
app.include_router(drm.router)
app.include_router(languages.router)
app.include_router(tags.router)
app.include_router(actors.router)
app.include_router(sync_router.router)

app.mount(
    "/videos",
    StaticFiles(directory=str(settings.out_dir), html=False, check_dir=True),
    name="videos",
)

# Shared admin CSS/JS, vendored hls.js fallback, etc. Read-only.
import pathlib as _pathlib
_STATIC_DIR = _pathlib.Path(__file__).resolve().parent / "static"
app.mount(
    "/static",
    StaticFiles(directory=str(_STATIC_DIR), html=False, check_dir=True),
    name="static",
)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=302)
