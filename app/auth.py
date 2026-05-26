"""Authentication, sessions, and permission guards for the `/admin` console
(admin-accounts-auth).

- Password hashing via passlib + bcrypt.
- Cookie-session helpers (the session only carries `{"username": ...}`; every
  request re-reads the `users` row, so deactivation / permission changes apply
  immediately).
- FastAPI dependencies: `require_user`, `require_admin`, `require_can_delete`,
  `require_can_sync`. The forced-password-change guard lives inside
  `require_user`.
- A minimal in-process login throttle.

Only `/admin` routes use these guards; `/api`, `/drm`, `/videos` stay open.
"""
import time
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext

from . import db

# Self-service password page — exempt from the forced-password-change redirect
# so a user with must_change_pw=1 can actually reach the form.
PASSWORD_PATH = "/admin/account/password"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(plain, password_hash)
    except (ValueError, TypeError):
        # Malformed / empty stored hash — treat as a non-match rather than 500.
        return False


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------


def login_session(request: Request, username: str) -> None:
    request.session["username"] = username


def logout_session(request: Request) -> None:
    request.session.clear()


def resolve_current_user(request: Request) -> dict | None:
    """Resolve the session cookie to a fresh `users` row, or None.

    The cookie is only an identity claim; the row is re-read every call so a
    deleted account or a permission change takes effect on the next request.
    A stale / invalid session is cleared as a side effect.
    """
    username = request.session.get("username")
    if not username:
        return None
    user = db.get_user(username)
    if user is None:
        request.session.clear()
        return None
    return user


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _redirect(location: str) -> HTTPException:
    # A 303 carried by HTTPException: the default exception handler emits the
    # status + Location header, which the browser follows regardless of body.
    return HTTPException(status_code=303, headers={"Location": location})


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


def require_user(request: Request) -> dict:
    """Gate every `/admin` route. Unauthenticated HTML requests are redirected
    to `/login` (preserving the original path as `next`); JSON / XHR requests
    get 401. Also enforces the forced-password-change redirect.
    """
    user = resolve_current_user(request)
    if user is None:
        if _wants_html(request):
            nxt = quote(request.url.path, safe="")
            raise _redirect(f"/login?next={nxt}")
        raise HTTPException(status_code=401, detail="authentication required")

    request.state.current_user = user

    if user["must_change_pw"] and request.url.path != PASSWORD_PATH:
        if _wants_html(request):
            raise _redirect(PASSWORD_PATH)
        raise HTTPException(
            status_code=403,
            detail="password change required before continuing",
        )
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    """Gate account-management and audit routes to `admin` accounts."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="管理员权限")
    return user


def require_can_delete(user: dict = Depends(require_user)) -> dict:
    """Gate destructive routes. Admins always pass; staff need `can_delete`."""
    if user["role"] != "admin" and not user["can_delete"]:
        raise HTTPException(status_code=403, detail="需要「删除」权限")
    return user


def require_can_sync(user: dict = Depends(require_user)) -> dict:
    """Gate business-server sync routes. Admins always pass; staff need
    `can_sync`."""
    if user["role"] != "admin" and not user["can_sync"]:
        raise HTTPException(status_code=403, detail="需要「同步到业务服务器」权限")
    return user


# ---------------------------------------------------------------------------
# login throttle (in-process; resets on restart)
# ---------------------------------------------------------------------------

_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 300

# username -> (consecutive_failure_count, lockout_until_epoch)
_failures: dict[str, tuple[int, float]] = {}


def lockout_remaining(username: str) -> int:
    """Seconds the account is locked for after too many failed logins; 0 if
    not locked."""
    entry = _failures.get(username)
    if not entry:
        return 0
    _, until = entry
    remaining = until - time.time()
    return int(remaining) if remaining > 0 else 0


def record_login_failure(username: str) -> None:
    count, _ = _failures.get(username, (0, 0.0))
    count += 1
    until = time.time() + _LOCKOUT_SECONDS if count >= _MAX_FAILURES else 0.0
    _failures[username] = (count, until)


def clear_login_failures(username: str) -> None:
    _failures.pop(username, None)
