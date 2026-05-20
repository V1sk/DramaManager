## Context

The HLS management server is a FastAPI app: server-rendered Jinja2 templates plus Alpine.js for the `/admin` console, SQLite (WAL, FK enforcement on) for state, and synchronous-then-queued pipeline work. CLAUDE.md states plainly: **"no auth on any endpoint (admin, upload, cover replacement, DRM key, static files). Must stay behind VPN / internal network."**

That posture no longer fits: multiple operators share the console, and destructive actions (delete drama, push sync to the business/prod server) need to be both gated and attributable. The product ask is one admin account plus multiple staff accounts, with the admin assigning passwords and per-account permissions, and a record of who did what.

Constraints carried from the existing codebase:
- Server-rendered HTML — sessions belong in cookies, not bearer tokens.
- SQLite schema changes in the drama/i18n stack are destructive (operators delete `hls.db` before redeploy). New tables added with `CREATE TABLE IF NOT EXISTS` are additive and MUST NOT force a rebuild — drama data is preserved.
- The SDK contract (`/api`, `/drm`, `/videos`) is consumed by the Android SDK and the business server with no credentials today; changing it is out of scope.
- `init_db()` already runs startup self-tests and a one-shot reap; bootstrap logic fits the same hook.

## Goals / Non-Goals

**Goals:**
- Password login for the `/admin` console with cookie-based sessions.
- One bootstrap `admin`; admin creates/disables `staff` accounts and assigns/resets passwords.
- Per-account permission toggles: `can_delete`, `can_sync`. Admin always has all permissions.
- An audit log of mutating `/admin` actions and login attempts, viewable by the admin.
- Disabling an account or changing its permissions takes effect immediately (next request).

**Non-Goals:**
- No auth on `/api`, `/drm`, `/videos` — they stay open behind the VPN.
- No self-service signup, email, or password-reset-by-email — the admin assigns every password.
- No named/custom roles or a role-permission matrix — only two roles (`admin`, `staff`) plus the two boolean toggles.
- No multi-factor auth, no SSO, no rate-limiting beyond a basic login throttle (see Risks).
- No retention/rotation policy for the audit log in this change (volume is tiny; revisit later).

## Decisions

### Decision: Cookie sessions via Starlette `SessionMiddleware`, not JWT/DB sessions

The console is server-rendered, so the browser already carries cookies. `SessionMiddleware` (signed, `itsdangerous`-backed) stores only `{"username": ...}` in the cookie — no server-side session table to reap. The session cookie is **only an identity claim**: every request re-reads the `users` row via the auth dependency and re-derives `is_active` + permissions. So a disabled account or a permission change is honored on the very next request without any "revoke session" machinery.

`SESSION_SECRET_KEY` comes from env and the server **fails fast at startup if unset** — a generated-per-boot key would silently log everyone out on every restart and is easy to misdiagnose.

*Alternatives considered:* (a) JWT bearer tokens — wrong fit for a cookie-driven server-rendered app, and would need client JS to attach headers. (b) Server-side session table — enables an explicit "log out everywhere" but adds a table, a reaper, and a per-request lookup we already do anyway for permissions. Not worth it for a handful of operators.

### Decision: Permissions = role + two boolean columns on `users`, evaluated per request

`users` carries `role TEXT ('admin'|'staff')`, `can_delete`, `can_sync`. Effective permission = `role == 'admin' OR <flag> == 1`. This matches the agreed "tick permissions per account" model exactly — no separate roles table, no permission-matrix UI. Adding a future permission is one column + one dependency.

Enforcement is via **FastAPI dependencies**, layered:
- `require_user` — resolves the session to an active `users` row; HTML routes get `303 → /login?next=...`, JSON/`Accept: application/json` routes get `401`. Attached at the `admin` router level (`include_router(admin.router, dependencies=[Depends(require_user)])`) so no admin route can be left unprotected by omission.
- `require_admin` — gate for account-management and audit routes.
- `require_can_delete` / `require_can_sync` — attached individually to each `DELETE /admin/...` route and each `POST /admin/.../sync` route. These return `403` when the permission is missing.

The delete/sync routes are spread across `admin.py`, `languages.py`, `tags.py`, `actors.py`, `sync.py`; each one named in the spec gets the dependency. The router-level `require_user` still covers them for the login check.

*Alternatives considered:* a single auth middleware doing path-prefix matching. Rejected — it cannot express "this route needs `can_delete`" without re-encoding the route table in the middleware, and dependencies give correct `403` vs `401` vs redirect semantics for free.

### Decision: `/login` lives in a separate, ungated `auth` router

`require_user` is attached to the whole `admin` router, so the login page cannot live there (chicken-and-egg). A separate `app/routers/auth.py` holds `GET/POST /login` and `POST /logout` with no auth dependency. `GET /` keeps redirecting to `/admin`; an unauthenticated visitor is then bounced to `/login`.

### Decision: Audit log written by middleware, enriched for known routes

An HTTP middleware scoped to `/admin/*` writes one `audit_log` row **after** the response for every mutating method (`POST`/`PATCH`/`PUT`/`DELETE`), capturing `username` (from session), `method`, `path`, `status_code`, `ip`, and `ts`. Doing it in middleware means a new mutating route is audited automatically — it cannot be forgotten. For well-known routes (delete drama/episode/language/tag/actor, sync, account CRUD) the middleware maps the path to a semantic `action` (e.g. `delete_drama`) and `target` (the slug/episode/account). Login success/failure are written explicitly by the `auth` router (the middleware does not cover `/login`).

Audit rows are best-effort: a logging failure MUST NOT fail the underlying request (wrap in try/except, log to stderr). The table is append-only; the admin reads it newest-first with pagination at `/admin/audit`.

*Alternatives considered:* explicit `audit(...)` calls in each handler — more precise `target` data, but easy to forget on a new route and noisy across ~20 handlers. The middleware + targeted enrichment gives completeness with semantic labels where they matter.

### Decision: First-boot bootstrap inside `init_db()`

After `CREATE TABLE`, `init_db()` counts `users`. If zero, it inserts `admin` with `role='admin'`, password hashed from `ADMIN_INITIAL_PASSWORD`, and `must_change_pw=1`. If `users` is empty and `ADMIN_INITIAL_PASSWORD` is unset, **startup fails fast** — a server with a login wall and no way in is a worse outcome than a clear error. `must_change_pw=1` forces the admin to set a real password on first login (and applies whenever the admin resets any account's password). Password hashing uses `passlib` with bcrypt.

### Decision: Password storage — bcrypt via `passlib`

`passlib[bcrypt]` is the de-facto standard, handles salting and the verify/identify API, and lets us bump the scheme later. Passwords are write-only: stored as `password_hash`, never returned by any endpoint or JSON aggregate.

## Risks / Trade-offs

- **Lock-out from a lost `SESSION_SECRET_KEY` / forgotten admin password** → Recovery is operator-level: the box is internal, so an operator with shell access can run a small `passlib` snippet to rewrite `password_hash`, or delete the `users` rows to re-trigger bootstrap. Documented in CLAUDE.md.
- **Signed-cookie sessions can't be force-revoked server-side** → Mitigated by re-reading the `users` row every request: `is_active=0` blocks access immediately even with a valid cookie. A stolen cookie remains valid until expiry — acceptable on an internal VPN; cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` when served over TLS.
- **No real brute-force protection** → Add a minimal in-process throttle (e.g. small fixed delay + lockout after N failures per username/IP) so the login isn't trivially scriptable. Full rate-limiting is a non-goal; the VPN is the primary boundary.
- **DRM key endpoint stays unauthenticated** → `/drm/.../key` still serves raw key bytes to anyone on the network. Explicitly out of scope (SDK contract). Called out so it is a known, accepted gap, not an oversight.
- **Audit log growth** → A few operators produce on the order of thousands of rows/year — negligible for SQLite. No pruning in this change; an index on `ts` keeps the `/admin/audit` query fast.
- **Operational breaking change** → After deploy the console requires login. Mitigated by the fail-fast env checks and a CLAUDE.md deploy note; there is no production user data at risk because the new tables are additive.

## Migration Plan

1. Add `passlib[bcrypt]` + `itsdangerous` to `requirements.txt`; `pip install -r`.
2. Set `SESSION_SECRET_KEY` and `ADMIN_INITIAL_PASSWORD` in the deploy env.
3. Deploy. `init_db()` creates `users` + `audit_log` (additive — existing `hls.db` and drama data untouched) and bootstraps `admin`.
4. Admin logs in, is forced to change the password, then creates staff accounts and sets `can_delete` / `can_sync` per person.
5. Rollback: revert the code; the extra tables are inert and can be left in place. `hls.db` needs no downgrade.

## Open Questions

- Session lifetime / idle timeout — propose a fixed ~12h expiry (one work day) unless operators want "remember me". Confirm during apply.
- Login throttle thresholds (N failures, lockout window) — propose 5 failures → 5-minute lockout per username; tune if too strict.
