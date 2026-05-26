# admin-accounts-auth

`/admin` 后台的账号 / 鉴权 / 权限 / 审计能力：密码登录 + 签名 cookie 会话、`admin` / `staff` 两种角色与 per-account `can_delete` / `can_sync` 权限开关、管理员账号管理、首启 `admin` 引导、操作员行为审计日志。归档自 `admin-accounts-auth`。

## Requirements

### Requirement: Operator login with cookie session

The system SHALL require a valid authenticated session to access any `/admin` route. Authentication SHALL be username + password, verified against a bcrypt password hash, and SHALL establish a signed cookie session identifying the user.

#### Scenario: Successful login

- **WHEN** an operator submits a correct username and password on `POST /login`
- **THEN** the system establishes a signed cookie session and redirects to `/admin` (or to the `next` target if one was supplied)

#### Scenario: Wrong password

- **WHEN** an operator submits a username with an incorrect password
- **THEN** the system re-renders the login page with an error and does NOT establish a session

#### Scenario: Unknown account

- **WHEN** an operator submits credentials for a username that does not exist
- **THEN** the system rejects the login with the same generic error and does NOT establish a session

#### Scenario: Logout

- **WHEN** a logged-in operator triggers `POST /logout`
- **THEN** the system clears the session cookie and redirects to `/login`

### Requirement: Authentication gate on the admin console

The system SHALL reject any request to a `/admin` route that lacks a valid session, while leaving SDK-facing endpoints (`/api`, `/drm`, `/videos`) and the `/login` route open.

#### Scenario: Unauthenticated HTML request

- **WHEN** a request without a valid session hits an HTML `/admin` route
- **THEN** the system responds with a redirect to `/login`, preserving the originally requested path as a `next` parameter

#### Scenario: Unauthenticated JSON request

- **WHEN** a request without a valid session hits a JSON `/admin` route (or one sent with `Accept: application/json`)
- **THEN** the system responds with HTTP 401 and does not redirect

#### Scenario: SDK endpoints remain open

- **WHEN** an unauthenticated request hits `/api/*`, `/drm/*`, or `/videos/*`
- **THEN** the system serves the response normally, with no login required

### Requirement: Session reflects current account state on every request

The system SHALL treat the session cookie as an identity claim only, re-reading the user's row on every request so that account deletion and permission changes take effect immediately.

#### Scenario: Account deleted while logged in

- **WHEN** the admin deletes a staff account while that staff member holds a valid session cookie
- **THEN** the staff member's next `/admin` request is rejected as unauthenticated

#### Scenario: Permission revoked while logged in

- **WHEN** the admin disables a staff account's `can_delete` while that staff member is logged in
- **THEN** the staff member's next delete request is rejected with HTTP 403

### Requirement: Roles and per-account permissions

Each account SHALL have a `role` of `admin` or `staff`. An `admin` account SHALL have every permission unconditionally. A `staff` account's permissions SHALL be governed by per-account boolean toggles: `can_delete` (delete drama / episode / language / tag / actor) and `can_sync` (push sync to the business server). All other admin-console actions SHALL be available to any authenticated account.

#### Scenario: Admin has all permissions

- **WHEN** an account with `role='admin'` performs a delete or a sync action
- **THEN** the system allows it regardless of the `can_delete` / `can_sync` toggle values

#### Scenario: Staff without can_delete is blocked from deleting

- **WHEN** a `staff` account with `can_delete=0` calls a delete route for a drama, episode, language, tag, or actor
- **THEN** the system responds with HTTP 403 and does not perform the deletion

#### Scenario: Metadata-level deletions stay ungated

- **WHEN** a `staff` account with `can_delete=0` removes a translation, poster, or subtitle (metadata editing, not entity deletion)
- **THEN** the system allows the action

#### Scenario: Staff without can_sync is blocked from syncing

- **WHEN** a `staff` account with `can_sync=0` calls a `POST /admin/.../sync` route
- **THEN** the system responds with HTTP 403 and does not enqueue the sync

#### Scenario: Staff can perform ungated actions

- **WHEN** a `staff` account with both toggles off uploads an episode or edits drama metadata
- **THEN** the system allows the action

### Requirement: Admin account management

An `admin` account SHALL be able to create accounts, set and reset passwords, toggle `role` and the permission flags, and delete accounts. Accounts have no enabled/disabled state — to revoke access, the admin deletes the row. Account-management routes SHALL be inaccessible to `staff` accounts.

#### Scenario: Admin creates a staff account

- **WHEN** an admin submits `POST /admin/accounts` with a new username, an initial password, and permission selections
- **THEN** the system creates a `staff` account with the hashed password, the chosen permissions, and `must_change_pw=1`

#### Scenario: Duplicate username rejected

- **WHEN** an admin submits a username that already exists
- **THEN** the system rejects the request with HTTP 409 and creates no account

#### Scenario: Admin resets a password

- **WHEN** an admin submits a new password for an existing account via `POST /admin/accounts/{username}/password`
- **THEN** the system stores the new hash and sets that account's `must_change_pw` to `1`

#### Scenario: Admin updates role or permissions

- **WHEN** an admin submits `PATCH /admin/accounts/{username}` changing `role`, `can_delete`, or `can_sync`
- **THEN** the system persists the changes and they apply on the account's next request

#### Scenario: Admin deletes an account

- **WHEN** an admin calls `DELETE /admin/accounts/{username}` for an existing account
- **THEN** the system removes the account row and returns HTTP 204

#### Scenario: Staff cannot reach account management

- **WHEN** a `staff` account requests any `/admin/accounts` route or `GET /admin/audit`
- **THEN** the system responds with HTTP 403

#### Scenario: Admin cannot lock everyone out

- **WHEN** an admin attempts to delete or demote the last remaining `admin` account
- **THEN** the system rejects the request and keeps at least one admin

### Requirement: Forced password change

When an account has `must_change_pw=1`, the system SHALL require the account to set a new password before performing any other admin action, and SHALL clear the flag once a new password is set.

#### Scenario: Forced change on first login

- **WHEN** an account with `must_change_pw=1` logs in and requests any `/admin` page other than the password-change page
- **THEN** the system redirects the account to the self-service password-change page

#### Scenario: Clearing the flag

- **WHEN** that account submits a valid new password
- **THEN** the system stores the new hash, sets `must_change_pw` to `0`, and allows normal access

### Requirement: Self-service password change

Any authenticated account SHALL be able to change its own password by supplying its current password and a new password.

#### Scenario: Successful self-service change

- **WHEN** an authenticated account submits its correct current password and a new password
- **THEN** the system stores the new hash for that account

#### Scenario: Wrong current password

- **WHEN** an authenticated account submits an incorrect current password
- **THEN** the system rejects the change and does not modify the stored hash

### Requirement: First-boot admin bootstrap

On startup, when the `users` table contains no rows, the system SHALL create a single `admin` account using the `ADMIN_INITIAL_PASSWORD` environment variable and SHALL set that account's `must_change_pw` to `1`.

#### Scenario: Bootstrap on an empty user table

- **WHEN** the server starts and `users` is empty and `ADMIN_INITIAL_PASSWORD` is set
- **THEN** the system creates an `admin` account with that password hashed and `must_change_pw=1`

#### Scenario: Missing bootstrap password fails fast

- **WHEN** the server starts, `users` is empty, and `ADMIN_INITIAL_PASSWORD` is unset
- **THEN** startup fails with a clear error rather than running with an unreachable console

#### Scenario: No bootstrap when accounts exist

- **WHEN** the server starts and `users` already contains at least one row
- **THEN** the system does not create any account

### Requirement: Session secret configuration

The system SHALL require a `SESSION_SECRET_KEY` environment variable to sign session cookies and SHALL fail fast at startup when it is unset.

#### Scenario: Missing session secret fails fast

- **WHEN** the server starts with `SESSION_SECRET_KEY` unset
- **THEN** startup fails with a clear error

### Requirement: Audit log of operator actions

The system SHALL record an audit entry for every mutating `/admin` request (`POST`, `PATCH`, `PUT`, `DELETE`) and for every login attempt. Each entry SHALL capture the timestamp, the acting username (when known), the action, the affected target (when identifiable), the request method and path, the response status code, and the client IP. Audit writes SHALL be best-effort and SHALL NOT cause the underlying request to fail.

#### Scenario: Mutating action is recorded

- **WHEN** an authenticated operator performs a mutating `/admin` request such as deleting a drama
- **THEN** the system appends an audit entry naming the operator, a semantic action and target, the path, and the resulting status code

#### Scenario: Login attempts are recorded

- **WHEN** a login succeeds or fails
- **THEN** the system appends an audit entry recording the outcome and the attempted username

#### Scenario: Audit failure does not break the request

- **WHEN** writing an audit entry raises an error
- **THEN** the underlying request still completes normally

#### Scenario: Read-only requests are not audited

- **WHEN** an operator issues a `GET` request to an `/admin` page
- **THEN** the system does not append an audit entry

### Requirement: Admin audit log view

An `admin` account SHALL be able to view the audit log, newest entries first, with pagination.

#### Scenario: Admin views the audit log

- **WHEN** an admin opens `GET /admin/audit`
- **THEN** the system renders audit entries ordered newest-first with pagination controls

### Requirement: Role-aware navigation

The admin console navigation SHALL show the account-management and audit-log entry points only to `admin` accounts.

#### Scenario: Admin sees management links

- **WHEN** an `admin` account loads any admin page
- **THEN** the navigation includes the 账号 (accounts) and 操作记录 (audit) links

#### Scenario: Staff does not see management links

- **WHEN** a `staff` account loads any admin page
- **THEN** the navigation omits the 账号 and 操作记录 links
