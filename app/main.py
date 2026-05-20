import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .auth import require_admin, require_user
from .config import settings
from .work_queue import worker_loop
from .routers import (
    accounts,
    actors,
    admin,
    api,
    auth as auth_router,
    drm,
    languages,
    sync as sync_router,
    tags,
)

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

    worker_tasks = [
        asyncio.create_task(worker_loop(i), name=f"pipeline-worker-{i}")
        for i in range(settings.pipeline_concurrency)
    ]
    log.info("lifespan up: %d pipeline worker(s)", settings.pipeline_concurrency)
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

    if settings.storage_enabled:
        from . import storage
        prov = storage.provider
        log.info(
            "lifespan up: out_dir=%s db=%s storage_provider=%s staging=%s prod=%s",
            settings.out_dir, settings.db_path, settings.storage_provider,
            prov.staging_base_url, prov.prod_base_url,
        )
    else:
        log.info(
            "lifespan up: out_dir=%s db=%s storage_provider=none",
            settings.out_dir, settings.db_path,
        )
    try:
        yield
    finally:
        for t in worker_tasks:
            t.cancel()
        for t in worker_tasks:
            try:
                await t
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


# admin-accounts-auth: map a well-known mutating /admin route to a semantic
# (action, target) for the audit log. Anything unmatched falls back to the bare
# HTTP method as the action (the `path` column still records the full path).
_AUDIT_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("DELETE", re.compile(r"^/admin/dramas/([^/]+)$"), "delete_drama"),
    ("DELETE", re.compile(r"^/admin/episodes/([^/]+)/([^/]+)$"), "delete_episode"),
    ("DELETE", re.compile(r"^/admin/languages/([^/]+)$"), "delete_language"),
    ("DELETE", re.compile(r"^/admin/tags/([^/]+)$"), "delete_tag"),
    ("DELETE", re.compile(r"^/admin/actors/([^/]+)$"), "delete_actor"),
    ("DELETE", re.compile(r"^/admin/accounts/([^/]+)$"), "delete_account"),
    ("POST", re.compile(r"^/admin/dramas/([^/]+)/sync$"), "sync_drama"),
    ("POST", re.compile(r"^/admin/episodes/([^/]+)/([^/]+)/sync$"), "sync_episode"),
    ("POST", re.compile(r"^/admin/accounts$"), "create_account"),
    ("PATCH", re.compile(r"^/admin/accounts/([^/]+)$"), "update_account"),
    ("POST", re.compile(r"^/admin/accounts/([^/]+)/password$"), "reset_account_password"),
]


def _classify_audit(method: str, path: str) -> tuple[str, str | None]:
    for m, rx, action in _AUDIT_PATTERNS:
        if m != method:
            continue
        mt = rx.match(path)
        if mt:
            groups = mt.groups()
            return action, ("/".join(groups) if groups else None)
    return method, None


@app.middleware("http")
async def audit_admin_mutations(request: Request, call_next):
    """Record every mutating `/admin` request in the audit log. Runs after the
    response so the status code is known. Best-effort — a logging failure must
    never break the underlying request.
    """
    response = await call_next(request)
    try:
        path = request.url.path
        method = request.method
        if path.startswith("/admin/") and method in ("POST", "PATCH", "PUT", "DELETE"):
            action, target = _classify_audit(method, path)
            db.insert_audit_entry(
                username=request.session.get("username"),
                action=action,
                target=target,
                method=method,
                path=path,
                status_code=response.status_code,
                ip=request.client.host if request.client else None,
            )
    except Exception:  # noqa: BLE001 — audit is best-effort
        log.exception("audit middleware failed")
    return response


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


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SessionMiddleware MUST be the last middleware registered so it is the
# outermost layer — that way `request.session` is populated before the audit
# middleware and the auth dependencies run.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="hls_admin_session",
    max_age=12 * 3600,
    same_site="lax",
    https_only=False,
)


# Ungated routers: the login page (the way in) and the SDK contract
# (`/api`, `/drm`) — the latter stays open behind the VPN, unchanged.
app.include_router(auth_router.router)
app.include_router(api.router)
app.include_router(drm.router)

# admin-accounts-auth: every `/admin` route requires an authenticated session.
# `require_user` is attached at include time so no route can be left ungated by
# omission. Per-route `require_can_delete` / `require_can_sync` / `require_admin`
# dependencies live inside the routers themselves.
_admin_gate = [Depends(require_user)]
app.include_router(admin.router, dependencies=_admin_gate)
app.include_router(languages.router, dependencies=_admin_gate)
app.include_router(tags.router, dependencies=_admin_gate)
app.include_router(actors.router, dependencies=_admin_gate)
app.include_router(sync_router.router, dependencies=_admin_gate)
# Account management + audit + self-service password change. Router-level gate
# is `require_user`; the admin-only routes additionally carry `require_admin`.
app.include_router(accounts.router, dependencies=_admin_gate)

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
