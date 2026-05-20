## 1. Dependencies & configuration

- [x] 1.1 Add `passlib[bcrypt]` and `itsdangerous` to `requirements.txt`; install into `venv`.
- [x] 1.2 Add `session_secret_key` and `admin_initial_password` to `Settings` in `app/config.py`; read `SESSION_SECRET_KEY` (required — fail-fast in `load_settings()` if unset) and `ADMIN_INITIAL_PASSWORD` (optional here, enforced at bootstrap).
- [x] 1.3 Document the two new env vars in `CLAUDE.md`'s environment-variable table.

## 2. Database schema & helpers

- [x] 2.1 Add `users` and `audit_log` `CREATE TABLE IF NOT EXISTS` statements to `_SCHEMA` in `app/db.py` (additive — must not disturb existing tables).
- [x] 2.2 Add user CRUD helpers to `app/db.py`: `get_user`, `list_users`, `create_user`, `update_user` (role / is_active / can_delete / can_sync), `set_user_password` (sets `must_change_pw`), `clear_must_change_pw`, `delete_user`, `count_active_admins`.
- [x] 2.3 Add audit helpers to `app/db.py`: `insert_audit_entry` and `list_audit_entries` (newest-first, paginated); add an index on `audit_log(ts)`.
- [x] 2.4 Add first-boot bootstrap to `init_db()`: when `users` is empty, create `admin` from `ADMIN_INITIAL_PASSWORD` with `must_change_pw=1`; fail fast if that env var is unset. No-op when accounts already exist.

## 3. Auth core (`app/auth.py`)

- [x] 3.1 Create `app/auth.py` with bcrypt hash/verify helpers (`passlib` `CryptContext`).
- [x] 3.2 Add session helpers: write/read/clear `{"username": ...}` on `request.session`; resolve the session to a fresh `users` row.
- [x] 3.3 Implement `require_user` dependency — resolves an active user; redirects HTML requests to `/login?next=...`, returns 401 for JSON / `Accept: application/json` requests.
- [x] 3.4 Implement `require_admin` dependency — 403 for non-admin accounts.
- [x] 3.5 Implement `require_can_delete` and `require_can_sync` dependencies — admin always passes; staff pass only with the matching flag; 403 otherwise.
- [x] 3.6 Implement the forced-password-change guard — when the current user has `must_change_pw=1`, redirect any admin request (except the password-change route and logout) to the self-service password page.
- [x] 3.7 Add a minimal login throttle (per-username failure count + short lockout window).

## 4. Session middleware & login router

- [x] 4.1 Register Starlette `SessionMiddleware` in `app/main.py` using `settings.session_secret_key`; cookie flagged `HttpOnly` + `SameSite=Lax`.
- [x] 4.2 Create `app/routers/auth.py` (no auth dependencies): `GET /login`, `POST /login`, `POST /logout`; record login success/failure to the audit log.
- [x] 4.3 Create the `login.html` template (extends `_base.html` or a minimal standalone layout) with the `next` field and error display.
- [x] 4.4 Register the `auth` router in `app/main.py` before/outside the gated admin routers.

## 5. Gate the admin console

- [x] 5.1 Attach `require_user` at router-include level for `admin`, `languages`, `tags`, `actors`, and `sync` routers (every `/admin` route), keeping `/api`, `/drm`, `/videos` ungated.
- [x] 5.2 Attach `require_can_delete` to each delete route: `DELETE /admin/dramas/{slug}`, `DELETE /admin/episodes/{slug}/{ep}`, `DELETE /admin/languages/{code}`, drama tag/actor deletes, and tag/actor library deletes.
- [x] 5.3 Attach `require_can_sync` to the sync routes: `POST /admin/dramas/{slug}/sync`, `POST /admin/episodes/{slug}/{ep}/sync`, and the `/admin/sync` "sync all" action.
- [x] 5.4 Verify `GET /` → `/admin` → `/login` redirect chain works for an unauthenticated visitor.

## 6. Account management

- [x] 6.1 Create `app/routers/accounts.py` gated by `require_admin`: `GET /admin/accounts`, `POST /admin/accounts`, `PATCH /admin/accounts/{username}`, `POST /admin/accounts/{username}/password`, `DELETE /admin/accounts/{username}`.
- [x] 6.2 Enforce the last-active-admin guard on delete / deactivate / demote (reject when it would leave zero active admins).
- [x] 6.3 Return 409 on duplicate username at create time.
- [x] 6.4 Create the `accounts.html` template — account table plus create form and per-row permission/status/role/reset-password controls (Alpine.js inline panels, consistent with existing admin pages).
- [x] 6.5 Add self-service password routes (`GET/POST /admin/account/password`) and the `account_password.html` template — accepts current + new password; clears `must_change_pw`.

## 7. Audit log

- [x] 7.1 Add an HTTP middleware in `app/main.py` scoped to `/admin/*` that, after the response, writes an audit row for `POST`/`PATCH`/`PUT`/`DELETE` requests (username, method, path, status, ip, ts); best-effort, never fails the request.
- [x] 7.2 Map well-known routes (drama/episode/language/tag/actor deletes, sync, account CRUD) to a semantic `action` + `target` in the audit middleware.
- [x] 7.3 Add `GET /admin/audit` in `accounts.py` (gated by `require_admin`) + `audit.html` template with newest-first pagination.

## 8. Navigation & docs

- [x] 8.1 Update `_base.html` nav to render the 账号 (`/admin/accounts`) and 操作记录 (`/admin/audit`) links only when the session user is an admin; show the logged-in username + a logout control.
- [x] 8.2 Update `CLAUDE.md`: replace the "no auth on any endpoint" deployment-posture note, add the new routes to the URL map, and document the bootstrap / lockout-recovery procedure.

## 9. Verification

- [x] 9.1 Fresh-boot test: with `SESSION_SECRET_KEY` + `ADMIN_INITIAL_PASSWORD` set and an empty `users` table, confirm the `admin` account is bootstrapped and forced to change its password on first login.
- [x] 9.2 Fail-fast test: confirm startup aborts when `SESSION_SECRET_KEY` is unset, and when `users` is empty with `ADMIN_INITIAL_PASSWORD` unset.
- [x] 9.3 Permission test: create a staff account, verify it can upload/edit, is blocked (403) from delete and sync, and gains access immediately when the flags are toggled on.
- [x] 9.4 Session test: disable an account mid-session and confirm its next request is rejected.
- [x] 9.5 Audit test: perform deletes, syncs, and login attempts; confirm rows appear correctly on `/admin/audit` and that SDK endpoints (`/api`, `/drm`, `/videos`) still serve without login.
