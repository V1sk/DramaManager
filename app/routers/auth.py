"""Login / logout routes (admin-accounts-auth).

This router is registered WITHOUT the `require_user` gate — it is the way in.
Login attempts (success and failure) are written to the audit log here; the
audit middleware deliberately does not cover `/login`.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, db

router = APIRouter()
log = logging.getLogger("hls.auth")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _safe_next(nxt: str | None) -> str:
    """Only allow same-site relative redirect targets — blocks open-redirect
    via a crafted `next` value."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/admin"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _audit_login(
    request: Request, username: str, *, action: str, status_code: int,
) -> None:
    try:
        db.insert_audit_entry(
            username=username or None,
            action=action,
            target=username or None,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            ip=_client_ip(request),
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        log.exception("failed to write login audit entry")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin") -> HTMLResponse:
    if auth.resolve_current_user(request) is not None:
        return RedirectResponse("/admin", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
):
    target = _safe_next(next)
    username = username.strip()

    def _fail(message: str, action: str = "login_failed") -> HTMLResponse:
        _audit_login(request, username, action=action, status_code=401)
        return _TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": message},
            status_code=401,
        )

    locked = auth.lockout_remaining(username)
    if locked > 0:
        return _fail(f"账号暂时锁定，请 {locked} 秒后重试。", action="login_locked")

    user = db.get_user(username)
    if (
        user is None
        or not user["is_active"]
        or not auth.verify_password(password, user["password_hash"])
    ):
        auth.record_login_failure(username)
        return _fail("用户名或密码错误。")

    auth.clear_login_failures(username)
    auth.login_session(request, username)
    _audit_login(request, username, action="login", status_code=303)
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    username = request.session.get("username")
    auth.logout_session(request)
    if username:
        try:
            db.insert_audit_entry(
                username=username,
                action="logout",
                target=username,
                method="POST",
                path="/logout",
                status_code=303,
                ip=_client_ip(request),
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to write logout audit entry")
    return RedirectResponse("/login", status_code=303)
