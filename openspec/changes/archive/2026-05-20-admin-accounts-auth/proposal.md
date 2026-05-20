## Why

The HLS management server currently has **no authentication on any endpoint** — anyone who can reach the box can upload, delete dramas, and push content to the business server. Security relies entirely on the VPN/internal network. As more operators use the `/admin` console, we need accountable, per-person access: who logged in, who did what, and which staff are allowed to perform destructive or production-affecting actions.

## What Changes

- Add a **login layer** for the `/admin` console: cookie-based sessions, a `/login` page, logout. SDK-facing endpoints (`/api`, `/drm`, `/videos`) stay open — they remain VPN-protected and unchanged.
- Add **accounts**: one bootstrap `admin` account plus any number of `staff` accounts. The admin creates accounts, assigns/resets passwords, and enables/disables accounts.
- Add **per-account permissions**: two boolean toggles — `can_delete` (delete drama / episode / language / tag / actor) and `can_sync` (push sync to the business server). The `admin` role always has every permission; `staff` get only what is toggled on. All other actions (upload, metadata editing, viewing) are available to any logged-in user.
- Add an **audit log**: every mutating `/admin` request (`POST`/`PATCH`/`PUT`/`DELETE`) and every login attempt is recorded with account, action, target, and timestamp. The admin views it on a new `/admin/audit` page.
- **BREAKING (operational)**: after deploy, the `/admin` console requires a login. `SESSION_SECRET_KEY` and `ADMIN_INITIAL_PASSWORD` env vars must be set before first boot, or the server fails fast.

## Capabilities

### New Capabilities
- `admin-accounts-auth`: operator accounts, password-based login with cookie sessions, per-account permission toggles (`can_delete` / `can_sync`), role-gated account management, first-boot admin bootstrap, and an audit log of operator actions.

### Modified Capabilities
<!-- None: existing admin routes are now gated, but the gating requirement is owned by the new capability spec rather than restated across every existing spec. -->

## Impact

- **New tables** (`CREATE TABLE IF NOT EXISTS`, additive — does not trigger the destructive `hls.db` rebuild posture): `users`, `audit_log`.
- **New env vars**: `SESSION_SECRET_KEY` (required, fail-fast if unset), `ADMIN_INITIAL_PASSWORD` (required on first boot only).
- **New dependencies**: `passlib[bcrypt]`, `itsdangerous` (added to `requirements.txt`).
- **Code**: `app/main.py` (SessionMiddleware, router wiring), `app/db.py` (schema + user/audit helpers + bootstrap in `init_db`), `app/config.py` (new settings), a new `app/auth.py` (hashing, session helpers, FastAPI auth/permission dependencies), a new `app/routers/auth.py` (login/logout), a new `app/routers/accounts.py` (account management + audit page). Existing `app/routers/admin.py`, `languages.py`, `tags.py`, `actors.py`, `sync.py` get `require_*` dependencies on their delete/sync routes.
- **Templates**: new `login.html`, `accounts.html`, `audit.html`, self-service `account_password.html`; `_base.html` nav renders the 账号 / 操作记录 links conditionally by role.
- **Docs**: `CLAUDE.md` — the "no auth on any endpoint" note and the URL map are updated.
