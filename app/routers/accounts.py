"""Account management, self-service password change, and the audit log view
(admin-accounts-auth).

Router-level gate (applied in `main.py`) is `require_user`. The account-management
and audit routes additionally carry `require_admin`; the self-service password
routes are reachable by any authenticated account.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..auth import (
    PASSWORD_PATH,
    clear_login_failures,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)

router = APIRouter()
log = logging.getLogger("hls.accounts")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_MIN_PASSWORD_LEN = 6
_PATCH_FIELDS = {"role", "can_delete", "can_sync"}
_AUDIT_PAGE_SIZE = 50


def _check_password(pw: str) -> None:
    if len(pw or "") < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"密码至少 {_MIN_PASSWORD_LEN} 位",
        )


# ---------------------------------------------------------------------------
# account management (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/admin/accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def accounts_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "accounts.html",
        {"users": db.list_users(), "nav_active": "accounts"},
    )


@router.post("/admin/accounts", dependencies=[Depends(require_admin)])
async def accounts_create(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("staff"),
    can_delete: bool = Form(False),
    can_sync: bool = Form(False),
) -> RedirectResponse:
    _check_password(password)
    try:
        db.create_user(
            username=username,
            password_hash=hash_password(password),
            role=role,
            can_delete=can_delete,
            can_sync=can_sync,
            must_change_pw=True,
        )
    except db.UserValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.UserExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info("created account username=%s role=%s", username.strip(), role)
    return RedirectResponse(url="/admin/accounts", status_code=303)


@router.patch("/admin/accounts/{username}", dependencies=[Depends(require_admin)])
async def accounts_patch(
    username: str = PathParam(...),
    payload: dict = Body(...),
) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    unknown = set(payload.keys()) - _PATCH_FIELDS
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown / immutable fields: {sorted(unknown)}",
        )
    try:
        row = db.update_user(
            username,
            role=payload.get("role"),
            can_delete=payload.get("can_delete"),
            can_sync=payload.get("can_sync"),
        )
    except db.UserValidationError as e:
        raise HTTPException(status_code=400, detail=f"{e.field}: {e}")
    except db.LastAdminError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail=f"user '{username}' not found")
    log.info("updated account username=%s payload=%s", username, payload)
    return JSONResponse({
        "username": row["username"],
        "role": row["role"],
        "can_delete": bool(row["can_delete"]),
        "can_sync": bool(row["can_sync"]),
        "must_change_pw": bool(row["must_change_pw"]),
    })


@router.post(
    "/admin/accounts/{username}/password", dependencies=[Depends(require_admin)],
)
async def accounts_reset_password(
    username: str = PathParam(...),
    payload: dict = Body(...),
) -> JSONResponse:
    """Admin-driven password reset. Sets `must_change_pw=1` so the account is
    forced to pick its own password on next login."""
    new_password = (payload or {}).get("password", "")
    _check_password(new_password)
    ok = db.set_user_password(
        username, hash_password(new_password), must_change_pw=True,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"user '{username}' not found")
    # A reset clears any active login lockout for that account.
    clear_login_failures(username)
    log.info("reset password for account username=%s", username)
    return JSONResponse({"ok": True})


@router.delete(
    "/admin/accounts/{username}", dependencies=[Depends(require_admin)],
)
async def accounts_delete(username: str = PathParam(...)) -> Response:
    try:
        existed = db.delete_user(username)
    except db.LastAdminError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not existed:
        raise HTTPException(status_code=404, detail=f"user '{username}' not found")
    log.info("deleted account username=%s", username)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# self-service password change (any authenticated account)
# ---------------------------------------------------------------------------


@router.get(PASSWORD_PATH, response_class=HTMLResponse)
async def password_page(
    request: Request, user: dict = Depends(require_user),
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "account_password.html",
        {
            "must_change": bool(user["must_change_pw"]),
            "error": None,
            "nav_active": "",
        },
    )


@router.post(PASSWORD_PATH, response_class=HTMLResponse)
async def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: dict = Depends(require_user),
):
    def _render_error(message: str):
        return _TEMPLATES.TemplateResponse(
            request,
            "account_password.html",
            {
                "must_change": bool(user["must_change_pw"]),
                "error": message,
                "nav_active": "",
            },
            status_code=400,
        )

    if not verify_password(current_password, user["password_hash"]):
        return _render_error("当前密码不正确。")
    if new_password != confirm_password:
        return _render_error("两次输入的新密码不一致。")
    if len(new_password) < _MIN_PASSWORD_LEN:
        return _render_error(f"新密码至少 {_MIN_PASSWORD_LEN} 位。")
    if new_password == current_password:
        return _render_error("新密码不能与当前密码相同。")

    # Self-service change: the user chose this password, so clear must_change_pw.
    db.set_user_password(
        user["username"], hash_password(new_password), must_change_pw=False,
    )
    log.info("self-service password change username=%s", user["username"])
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# audit log view (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/admin/audit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def audit_page(request: Request, page: int = 1) -> HTMLResponse:
    page = max(1, page)
    total = db.count_audit_entries()
    total_pages = max(1, (total + _AUDIT_PAGE_SIZE - 1) // _AUDIT_PAGE_SIZE)
    page = min(page, total_pages)
    entries = db.list_audit_entries(
        limit=_AUDIT_PAGE_SIZE, offset=(page - 1) * _AUDIT_PAGE_SIZE,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "audit.html",
        {
            "entries": entries,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "nav_active": "audit",
        },
    )
