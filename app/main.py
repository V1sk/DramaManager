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
from .routers import admin, api, drm

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

    worker_task = asyncio.create_task(worker_loop(), name="pipeline-worker")
    if settings.oss_enabled:
        from .oss_upload import oss_public_base_url
        log.info(
            "lifespan up: out_dir=%s db=%s oss_enabled=True oss_public_base_url=%s",
            settings.out_dir, settings.db_path, oss_public_base_url,
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

app.mount(
    "/videos",
    StaticFiles(directory=str(settings.out_dir), html=False, check_dir=True),
    name="videos",
)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=302)
