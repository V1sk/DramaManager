import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

# Schema rebuild posture: this codebase is in pre-production. Every schema
# change in the OpenSpec drama / i18n / sync stack is destructive — operators
# delete hls.db before redeploy. There is no production data to migrate.
#
# Statement order matters: `languages` must precede `dramas` (FK target),
# and both must precede `translations` and `episodes` (FK targets).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS languages (
  code           TEXT    PRIMARY KEY,
  display_label  TEXT    NOT NULL,
  is_active      INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  created_at     TEXT    NOT NULL,
  updated_at     TEXT    NOT NULL
);

-- drama-meta-translations (step 3c) drops `name` from this table; the drama's
-- name is now stored as a row in `translations` keyed by (entity_type='drama',
-- entity_id=slug, lang_code=<some lang>, field='name'). Synopsis and per-language
-- poster file URLs follow the same pattern (`field='synopsis'` / `field='poster'`).
CREATE TABLE IF NOT EXISTS dramas (
  slug             TEXT    PRIMARY KEY,
  default_lang     TEXT    NOT NULL,
  -- business-server-sync (step 6): drama-level sync state machine.
  -- Values: 'dirty' / 'syncing' / 'clean' / 'sync_failed' / 'pending_delete'.
  -- A fresh drama starts 'dirty'. Library cascades flip to 'dirty' (skipping
  -- 'pending_delete'). The sync worker is the only writer of 'syncing' /
  -- 'clean' / 'sync_failed' transitions.
  sync_status      TEXT    NOT NULL DEFAULT 'dirty',
  sync_error       TEXT,
  last_synced_at   TEXT,
  -- 业务字段：免费集数。值=3 表示前 3 集 (ep_number 1..3) 免费，第 4 集起收费。
  -- 0 = 全部付费；默认 3 与业务场景对齐。SDK / 业务服务器据此决定付费墙。
  free_episodes    INTEGER NOT NULL DEFAULT 3,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  FOREIGN KEY (default_lang) REFERENCES languages(code) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS translations (
  entity_type  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  lang_code    TEXT NOT NULL,
  field        TEXT NOT NULL,
  value        TEXT NOT NULL,
  PRIMARY KEY (entity_type, entity_id, lang_code, field),
  FOREIGN KEY (lang_code) REFERENCES languages(code) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS tags (
  slug          TEXT    PRIMARY KEY,
  default_lang  TEXT    NOT NULL,
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  FOREIGN KEY (default_lang) REFERENCES languages(code) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS drama_tags (
  drama_slug  TEXT NOT NULL,
  tag_slug    TEXT NOT NULL,
  PRIMARY KEY (drama_slug, tag_slug),
  FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE CASCADE,
  FOREIGN KEY (tag_slug)   REFERENCES tags(slug)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS actors (
  slug          TEXT    PRIMARY KEY,
  default_lang  TEXT    NOT NULL,
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  FOREIGN KEY (default_lang) REFERENCES languages(code) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS drama_actors (
  drama_slug  TEXT NOT NULL,
  actor_slug  TEXT NOT NULL,
  PRIMARY KEY (drama_slug, actor_slug),
  FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE CASCADE,
  FOREIGN KEY (actor_slug) REFERENCES actors(slug) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episodes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  drama_slug       TEXT    NOT NULL,
  ep_number        INTEGER NOT NULL,
  episode_id       TEXT    NOT NULL UNIQUE,
  status           TEXT    NOT NULL,
  duration_ms      INTEGER,
  play_url         TEXT,
  key_uri          TEXT,
  key_b64          TEXT,
  iv_hex           TEXT,
  cover_url        TEXT,
  width            INTEGER,
  height           INTEGER,
  source_filename  TEXT,
  error_message    TEXT,
  -- upload-progress-retry: `progress` is a free-text sub-status shown in the
  -- admin UI while `status='encoding'` (e.g. "编码 720p" / "上传 OSS · 540p");
  -- cleared on every non-encoding transition. `source_path` retains the temp
  -- upload file for `status='failed'` rows so a one-click retry can re-enqueue
  -- without a re-upload; it is cleared (and the file deleted) once the episode
  -- reaches `ready`.
  progress         TEXT,
  source_path      TEXT,
  -- business-server-sync (step 6): episode-level sync state machine. Mirrors
  -- the dramas columns above. Independent from the drama row's sync_status.
  sync_status      TEXT    NOT NULL DEFAULT 'dirty',
  sync_error       TEXT,
  last_synced_at   TEXT,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  UNIQUE(drama_slug, ep_number),
  FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS subtitles (
  episode_id   TEXT NOT NULL,
  lang_code    TEXT NOT NULL,
  file_url     TEXT NOT NULL,
  uploaded_at  TEXT NOT NULL,
  PRIMARY KEY (episode_id, lang_code),
  FOREIGN KEY (episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE,
  FOREIGN KEY (lang_code)  REFERENCES languages(code)      ON DELETE RESTRICT
);
"""


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


class DramaExistsError(Exception):
    """Raised by create_drama when the slug already exists."""


class DramaValidationError(Exception):
    """Raised by create_drama when slug / name fails validation.
    `field` names which input was bad so the router can produce a useful 400.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class LanguageExistsError(Exception):
    """Raised by create_language when the code already exists."""


class LanguageValidationError(Exception):
    """Raised by create_language / update_language on bad input."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class LanguageNotFoundError(Exception):
    """Raised by create_drama when default_lang refers to no language row."""


class LanguageInactiveError(Exception):
    """Raised by create_drama when default_lang refers to an inactive language."""


class TagExistsError(Exception):
    """Raised by create_tag when slug already exists."""


class TagValidationError(Exception):
    """Raised on bad tag input. `field` names which input was bad."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class TagNotFoundError(Exception):
    """Raised when an operation targets an unknown tag slug."""


class TagDefaultLangNotCoveredError(Exception):
    """Raised when PATCH /admin/tags/{slug} would switch default_lang to a code
    that has no `label` translation for this tag (would orphan the label).
    """


class TagDefaultTranslationProtectedError(Exception):
    """Raised when DELETE /admin/tags/{slug}/translations/{lang_code} targets
    the tag's current default_lang.
    """


class DramaNotFoundError(Exception):
    """Raised when an operation targets an unknown drama slug (used by tag/actor
    junction-table writes)."""


class DramaDefaultLangNotCoveredError(Exception):
    """Raised when PATCH /admin/dramas/{slug} would switch default_lang to a
    language that has no `name` translation for this drama."""


class DramaDefaultTranslationProtectedError(Exception):
    """Raised when DELETE /admin/dramas/{slug}/translations/{lang_code} targets
    the drama's current default_lang."""


class DramaTranslationFreshNameRequiredError(Exception):
    """Raised when PUT /admin/dramas/{slug}/translations/{lang_code} sets only
    synopsis (or only poster) for a language that has no existing `name`
    translation. Name is required for fresh languages."""


class DramaPosterMissingNameError(Exception):
    """Raised when POST /admin/dramas/{slug}/poster?lang= targets a language
    that has no `name` translation yet."""


class ActorExistsError(Exception):
    """Raised by create_actor when slug already exists."""


class ActorValidationError(Exception):
    """Raised on bad actor input. `field` names which input was bad."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class ActorNotFoundError(Exception):
    """Raised when an operation targets an unknown actor slug."""


class ActorDefaultLangNotCoveredError(Exception):
    """Raised when PATCH /admin/actors/{slug} would switch default_lang to a code
    that has no `name` translation for this actor.
    """


class ActorDefaultTranslationProtectedError(Exception):
    """Raised when DELETE /admin/actors/{slug}/translations/{lang_code} targets
    the actor's current default_lang.
    """


def _connect(db_path: Path = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or settings.db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate_add_columns(conn)
    # FK enforcement self-test: the i18n-foundation spec requires verifying
    # that the translations.lang_code FK isn't silently dropped. We attempt
    # to insert a translation referencing a non-existent language; SQLite
    # MUST raise IntegrityError. Run inside a savepoint so we don't leave
    # state behind.
    _verify_fk_enforcement()


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for an already-deployed `hls.db`.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so columns
    introduced after the first deploy must be ALTER-ed in. SQLite has no
    `ADD COLUMN IF NOT EXISTS`; we list the table's current columns and only
    add the missing ones. Safe to run on every startup.
    """
    wanted = {
        "episodes": [
            ("progress", "TEXT"),
            ("source_path", "TEXT"),
        ],
        # `free_episodes` was added after the dramas table shipped; existing
        # rows must back-fill to the default value (3) so the column is
        # immediately usable without manual data migration.
        "dramas": [
            ("free_episodes", "INTEGER NOT NULL DEFAULT 3"),
        ],
    }
    for table, cols in wanted.items():
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _verify_fk_enforcement() -> None:
    """One-shot startup check: confirm `PRAGMA foreign_keys = ON` is taking
    effect by attempting an INSERT that should fail under FK enforcement.
    Raises RuntimeError if the FK isn't enforced (catastrophic — the entire
    i18n / drama integrity story collapses without it).
    """
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                "VALUES ('__fk_self_test__', 'x', '__no_such_lang__', 'name', 'x')"
            )
        except sqlite3.IntegrityError:
            return  # expected
        # Insert succeeded → FK is NOT enforced. Roll back and raise.
        conn.execute(
            "DELETE FROM translations WHERE entity_type='__fk_self_test__'"
        )
        raise RuntimeError(
            "FK enforcement is not active on the SQLite connection; "
            "PRAGMA foreign_keys=ON should be set per-connection."
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# languages CRUD (i18n-foundation)
# ---------------------------------------------------------------------------


def create_language(code: str, display_label: str) -> dict:
    if not _LANG_RE.match(code):
        raise LanguageValidationError(
            "code", "code must match ^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"
        )
    label = display_label.strip()
    if not label:
        raise LanguageValidationError("display_label", "display_label must not be empty")
    now = _now_iso()
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO languages(code, display_label, is_active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (code, label, now, now),
            )
        except sqlite3.IntegrityError as e:
            raise LanguageExistsError(f"language '{code}' already exists") from e
        row = conn.execute("SELECT * FROM languages WHERE code=?", (code,)).fetchone()
    return dict(row)


def get_language(code: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM languages WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def list_languages(active_only: bool = False) -> list[dict]:
    with _connect() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM languages WHERE is_active=1 ORDER BY code ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM languages ORDER BY code ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_language(
    code: str,
    *,
    display_label: str | None = None,
    is_active: bool | int | None = None,
) -> dict | None:
    """Update mutable fields. `code` itself is immutable — the path identifies
    the row. Returns the updated row, or None if no row matched.
    Raises LanguageValidationError for malformed inputs.
    """
    fields: list[str] = []
    params: list[Any] = []
    if display_label is not None:
        label = display_label.strip()
        if not label:
            raise LanguageValidationError("display_label", "display_label must not be empty")
        fields.append("display_label = ?")
        params.append(label)
    if is_active is not None:
        if is_active in (True, 1, "1"):
            params.append(1)
        elif is_active in (False, 0, "0"):
            params.append(0)
        else:
            raise LanguageValidationError(
                "is_active", "is_active must be true/false (or 1/0)"
            )
        fields.append("is_active = ?")
    if not fields:
        # nothing to update; return current row (or None)
        return get_language(code)

    fields.append("updated_at = ?")
    params.append(_now_iso())
    params.append(code)

    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE languages SET {', '.join(fields)} WHERE code = ?",
            params,
        )
        if (cur.rowcount or 0) == 0:
            return None
        row = conn.execute("SELECT * FROM languages WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def delete_language(code: str) -> tuple[bool, dict]:
    """Pre-checks references before deleting.

    Returns:
      (True,  {"dramas": 0, "translations": 0})  — language existed and was deleted.
      (False, {"dramas": d, "translations": t})  — references exist; not deleted (d+t > 0).
      (False, {"dramas": 0, "translations": 0})  — language not found (router 404 path).
    """
    with _connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM languages WHERE code=?", (code,)
        ).fetchone()
        if not existed:
            return (False, {"dramas": 0, "translations": 0})
        d = conn.execute(
            "SELECT COUNT(*) AS n FROM dramas WHERE default_lang=?", (code,)
        ).fetchone()["n"]
        t = conn.execute(
            "SELECT COUNT(*) AS n FROM translations WHERE lang_code=?", (code,)
        ).fetchone()["n"]
        if d > 0 or t > 0:
            return (False, {"dramas": d, "translations": t})
        # FK ON DELETE RESTRICT is the safety net.
        conn.execute("DELETE FROM languages WHERE code=?", (code,))
    return (True, {"dramas": 0, "translations": 0})


# ---------------------------------------------------------------------------
# dramas CRUD (drama-as-entity, with i18n-foundation FK validation)
# ---------------------------------------------------------------------------


def create_drama(
    slug: str,
    name: str,
    default_lang: str,
    *,
    free_episodes: int = 3,
) -> dict:
    """Insert a new drama row + initial `name` translation atomically.

    Per drama-meta-translations (step 3c), the drama's name is stored in the
    `translations` table under (entity_type='drama', entity_id=slug,
    lang_code=default_lang, field='name'). The dramas table itself no longer
    carries the column.

    `free_episodes` is the count of free episodes from the start (1..N); 0
    means everything is paid. Default 3 mirrors typical short-drama UX.
    """
    if not _SLUG_RE.match(slug):
        raise DramaValidationError("drama_slug", "drama_slug must match ^[a-z0-9][a-z0-9-]*$")
    name_trimmed = name.strip()
    if not name_trimmed:
        raise DramaValidationError("drama_name", "drama_name must not be empty")
    free_episodes = _validate_free_episodes(free_episodes)

    lang = get_language(default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"default_lang '{default_lang}' is not a registered language; "
            f"create it via POST /admin/languages first"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"default_lang '{default_lang}' is registered but inactive; "
            f"reactivate via PATCH /admin/languages/{default_lang}"
        )

    now = _now_iso()
    with _connect() as conn:
        conn.execute("BEGIN")
        try:
            try:
                conn.execute(
                    "INSERT INTO dramas(slug, default_lang, free_episodes, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (slug, default_lang, free_episodes, now, now),
                )
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK")
                # Distinguish the three plausible causes of an IntegrityError on
                # this INSERT so the operator gets a useful error message.
                msg = str(e).lower()
                if "foreign key" in msg:
                    raise LanguageNotFoundError(
                        f"default_lang '{default_lang}' is no longer registered "
                        f"(language deleted between validation and insert)"
                    ) from e
                if "not null" in msg:
                    # The new schema only requires (slug, default_lang, created_at,
                    # updated_at) — drama-meta-translations dropped `name`. If we
                    # see a NOT NULL violation here, the on-disk schema still has
                    # the legacy `name` column. The fix is destructive: rm hls.db*
                    # and restart so init_db() recreates with the new schema.
                    raise RuntimeError(
                        f"dramas table schema mismatch: {e}. "
                        f"Likely cause: hls.db was created before drama-meta-translations "
                        f"was applied, and CREATE TABLE IF NOT EXISTS preserved the "
                        f"legacy `name` column. Stop the server, delete hls.db / "
                        f"hls.db-wal / hls.db-shm, restart, and re-seed languages."
                    ) from e
                raise DramaExistsError(f"drama with slug '{slug}' already exists") from e
            conn.execute(
                "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                "VALUES ('drama', ?, ?, 'name', ?)",
                (slug, default_lang, name_trimmed),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise

        row = conn.execute("SELECT * FROM dramas WHERE slug=?", (slug,)).fetchone()
    return dict(row)


def get_drama(slug: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM dramas WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def list_dramas() -> list[dict]:
    """All dramas, joined with their default-lang `name` translation and
    episode count. Ordered by created_at DESC, slug ASC.

    `name` defaults to '' when no translation row exists in the drama's
    `default_lang` (defense in depth — POST /admin/dramas should always
    create one, but downstream readers must not crash on missing data).
    """
    sql = """
      SELECT
        d.slug,
        COALESCE(
          (SELECT value FROM translations
            WHERE entity_type='drama' AND entity_id=d.slug
                  AND lang_code=d.default_lang AND field='name'),
          ''
        ) AS name,
        d.default_lang,
        d.created_at,
        d.updated_at,
        COALESCE((SELECT COUNT(*) FROM episodes e WHERE e.drama_slug = d.slug), 0) AS ep_count
      FROM dramas d
      ORDER BY datetime(d.created_at) DESC, d.slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def delete_drama(slug: str) -> tuple[bool, int]:
    """Delete the drama row. Pre-checks the episode count for a friendly 409.
    Also deletes every `translations` row for this drama (`entity_type='drama'`,
    `entity_id=slug`) so the storage stays consistent with the row's removal.

    Returns:
      (True, 0)  — drama existed and was deleted.
      (False, n) — drama exists but has n episodes; not deleted (router → 409).
      (False, 0) — drama not found.
    """
    with _connect() as conn:
        ep_row = conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE drama_slug=?", (slug,)
        ).fetchone()
        ep_count = ep_row["n"] if ep_row else 0
        if ep_count > 0:
            return (False, ep_count)
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM translations WHERE entity_type='drama' AND entity_id=?",
                (slug,),
            )
            cur = conn.execute("DELETE FROM dramas WHERE slug=?", (slug,))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        return ((cur.rowcount or 0) > 0, 0)


_MAX_FREE_EPISODES = 9999


def _validate_free_episodes(value) -> int:
    """Coerce + range-check `free_episodes`. Always returns a non-negative int
    bounded by `_MAX_FREE_EPISODES` (defensive cap so a typo can't store an
    absurd value). Raises DramaValidationError("free_episodes", ...) on bad
    input — routers translate to 400.
    """
    if isinstance(value, bool):
        raise DramaValidationError("free_episodes", "free_episodes must be an integer")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise DramaValidationError(
            "free_episodes", "free_episodes must be an integer >= 0"
        ) from None
    if n < 0 or n > _MAX_FREE_EPISODES:
        raise DramaValidationError(
            "free_episodes",
            f"free_episodes must be between 0 and {_MAX_FREE_EPISODES}",
        )
    return n


def update_drama_free_episodes(slug: str, new_value: int) -> dict | None:
    """Set drama.free_episodes. Returns the updated row, or None if drama
    doesn't exist. Caller is responsible for marking the drama dirty (we keep
    that out of here so a no-op write doesn't pointlessly flip sync state —
    the router checks for an actual delta before calling mark_drama_dirty).
    """
    value = _validate_free_episodes(new_value)
    if get_drama(slug) is None:
        return None
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE dramas SET free_episodes=?, updated_at=? WHERE slug=?",
            (value, now, slug),
        )
        row = conn.execute("SELECT * FROM dramas WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def update_drama_default_lang(slug: str, new_default_lang: str) -> dict | None:
    """Switch a drama's default_lang. Pre-checks that a `name` translation
    exists for the new lang AND the new lang is active. Returns the updated
    row or None if drama unknown.
    """
    if get_drama(slug) is None:
        return None
    lang = get_language(new_default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"new default_lang '{new_default_lang}' is not a registered language"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"new default_lang '{new_default_lang}' is inactive"
        )
    with _connect() as conn:
        cov = conn.execute(
            "SELECT 1 FROM translations "
            "WHERE entity_type='drama' AND entity_id=? AND lang_code=? AND field='name'",
            (slug, new_default_lang),
        ).fetchone()
        if not cov:
            raise DramaDefaultLangNotCoveredError(
                f"drama '{slug}' has no 'name' translation in '{new_default_lang}'; "
                f"upsert it first via PUT /admin/dramas/{slug}/translations/{new_default_lang}"
            )
        now = _now_iso()
        conn.execute(
            "UPDATE dramas SET default_lang=?, updated_at=? WHERE slug=?",
            (new_default_lang, now, slug),
        )
        row = conn.execute("SELECT * FROM dramas WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def upsert_drama_translation(
    slug: str,
    lang_code: str,
    *,
    name: str | None = None,
    synopsis: str | None = None,
) -> dict:
    """Upsert one or both of `name` / `synopsis` for a drama in a given language.

    Rules:
      - At least one of `name` / `synopsis` must be present (else
        DramaValidationError on field='body').
      - Each present field must be non-empty after trim.
      - If the drama has no existing `name` translation in this `lang_code`,
        the call MUST include `name` (else DramaTranslationFreshNameRequiredError).
      - lang_code must reference an active language.

    Returns the resulting per-language content `{lang_code, name?, synopsis?, poster?}`.
    """
    if get_drama(slug) is None:
        raise DramaNotFoundError(f"drama '{slug}' not found")
    if name is None and synopsis is None:
        raise DramaValidationError("body", "at least one of `name` or `synopsis` must be present")
    if name is not None:
        name_trimmed = name.strip()
        if not name_trimmed:
            raise DramaValidationError("name", "name must not be empty")
    if synopsis is not None:
        synopsis_trimmed = synopsis.strip()
        if not synopsis_trimmed:
            raise DramaValidationError("synopsis", "synopsis must not be empty")
    lang = get_language(lang_code)
    if lang is None:
        raise LanguageNotFoundError(f"lang_code '{lang_code}' is not a registered language")
    if not lang["is_active"]:
        raise LanguageInactiveError(f"lang_code '{lang_code}' is inactive")

    with _connect() as conn:
        # "name required for fresh language" precondition
        if name is None:
            existing_name = conn.execute(
                "SELECT 1 FROM translations "
                "WHERE entity_type='drama' AND entity_id=? AND lang_code=? AND field='name'",
                (slug, lang_code),
            ).fetchone()
            if not existing_name:
                raise DramaTranslationFreshNameRequiredError(
                    f"drama '{slug}' has no name translation in '{lang_code}'; "
                    f"include `name` in the body when first populating a language"
                )

        conn.execute("BEGIN")
        try:
            if name is not None:
                conn.execute(
                    "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                    "VALUES ('drama', ?, ?, 'name', ?) "
                    "ON CONFLICT(entity_type, entity_id, lang_code, field) DO UPDATE SET value=excluded.value",
                    (slug, lang_code, name_trimmed),
                )
            if synopsis is not None:
                conn.execute(
                    "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                    "VALUES ('drama', ?, ?, 'synopsis', ?) "
                    "ON CONFLICT(entity_type, entity_id, lang_code, field) DO UPDATE SET value=excluded.value",
                    (slug, lang_code, synopsis_trimmed),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise

        # Return the resulting per-lang content (name/synopsis/poster)
        rows = conn.execute(
            "SELECT field, value FROM translations "
            "WHERE entity_type='drama' AND entity_id=? AND lang_code=?",
            (slug, lang_code),
        ).fetchall()
    out: dict = {"lang_code": lang_code, "name": None, "synopsis": None, "poster": None}
    for r in rows:
        if r["field"] in ("name", "synopsis", "poster"):
            out[r["field"]] = r["value"]
    return out


def delete_drama_translation(slug: str, lang_code: str) -> bool:
    """Delete every translation row for `(drama, slug, lang_code)` (name +
    synopsis + poster). Returns True if at least one row was deleted, False if
    none matched. Does NOT touch any on-disk poster file — the route handler
    is responsible for that (so it can collect warnings).

    Raises:
      DramaNotFoundError if the drama is unknown.
      DramaDefaultTranslationProtectedError if `lang_code` equals the drama's
        current `default_lang` (would orphan the drama's name).
    """
    drama = get_drama(slug)
    if drama is None:
        raise DramaNotFoundError(f"drama '{slug}' not found")
    if drama["default_lang"] == lang_code:
        raise DramaDefaultTranslationProtectedError(
            f"cannot delete the default-lang ('{lang_code}') translations while the drama exists; "
            f"change the drama's default_lang first or delete the drama"
        )
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM translations WHERE entity_type='drama' AND entity_id=? AND lang_code=?",
            (slug, lang_code),
        )
    return (cur.rowcount or 0) > 0


def list_drama_translations(slug: str) -> dict:
    """Return per-language nested content for a drama:
        {lang_code: {name, synopsis, poster}, ...}
    Each field is null when no translation row exists; ordering of keys is by
    `lang_code ASC`. Languages with no translation rows for this drama are absent.

    Raises DramaNotFoundError if the drama is unknown.
    """
    if get_drama(slug) is None:
        raise DramaNotFoundError(f"drama '{slug}' not found")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lang_code, field, value FROM translations "
            "WHERE entity_type='drama' AND entity_id=? "
            "AND field IN ('name', 'synopsis', 'poster')",
            (slug,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        bucket = out.setdefault(
            r["lang_code"],
            {"name": None, "synopsis": None, "poster": None},
        )
        bucket[r["field"]] = r["value"]
    # Stable key order by lang_code
    return {k: out[k] for k in sorted(out.keys())}


def upsert_drama_poster(slug: str, lang_code: str, url: str) -> None:
    """Upsert the `(drama, slug, lang_code, poster, url)` translation row.

    Caller is responsible for the on-disk file and for ensuring the drama has
    a `name` translation in this `lang_code` (`upsert_drama_poster` is a
    storage helper; the route handler does the high-level validation).
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
            "VALUES ('drama', ?, ?, 'poster', ?) "
            "ON CONFLICT(entity_type, entity_id, lang_code, field) DO UPDATE SET value=excluded.value",
            (slug, lang_code, url),
        )


def delete_drama_poster(slug: str, lang_code: str) -> bool:
    """Delete the `(drama, slug, lang_code, poster)` translation row only.
    Caller handles the on-disk file. Returns True if a row was deleted.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM translations "
            "WHERE entity_type='drama' AND entity_id=? AND lang_code=? AND field='poster'",
            (slug, lang_code),
        )
    return (cur.rowcount or 0) > 0


def get_drama_poster_url(slug: str, lang_code: str) -> str | None:
    """Return the stored poster URL for `(slug, lang_code)`, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM translations "
            "WHERE entity_type='drama' AND entity_id=? AND lang_code=? AND field='poster'",
            (slug, lang_code),
        ).fetchone()
    return row["value"] if row else None


def get_drama_name_translation(slug: str, lang_code: str) -> str | None:
    """Return the drama's `name` translation in a specific language, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM translations "
            "WHERE entity_type='drama' AND entity_id=? AND lang_code=? AND field='name'",
            (slug, lang_code),
        ).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# admin-redesign: aggregate read endpoints for the cards homepage and the
# drama-detail page.
# ---------------------------------------------------------------------------


def list_dramas_for_homepage() -> list[dict]:
    """Per-drama summary for `/admin` cards. Each row has:
      slug, default_lang, name (default-lang), synopsis_preview (~80 chars),
      poster_url (default-lang), ep_count (ready), latest_ep_number (ready),
      latest_ready_updated_at, drama_created_at.

    Sort: dramas with at least one ready episode first, by `latest_ready_updated_at DESC`;
    then dramas with zero ready episodes by `drama_created_at DESC`. Tie-break by `slug ASC`.
    """
    sql = """
      SELECT
        d.slug                                                              AS slug,
        d.default_lang                                                      AS default_lang,
        d.sync_status                                                       AS sync_status,
        d.created_at                                                        AS drama_created_at,
        COALESCE(
          (SELECT value FROM translations
            WHERE entity_type='drama' AND entity_id=d.slug
                  AND lang_code=d.default_lang AND field='name'),
          ''
        )                                                                   AS name,
        (SELECT value FROM translations
          WHERE entity_type='drama' AND entity_id=d.slug
                AND lang_code=d.default_lang AND field='synopsis')          AS synopsis,
        (SELECT value FROM translations
          WHERE entity_type='drama' AND entity_id=d.slug
                AND lang_code=d.default_lang AND field='poster')            AS poster_url,
        COALESCE(
          (SELECT COUNT(*) FROM episodes e
            WHERE e.drama_slug = d.slug AND e.status='ready'),
          0)                                                                AS ep_count,
        (SELECT MAX(e.ep_number) FROM episodes e
          WHERE e.drama_slug = d.slug AND e.status='ready')                 AS latest_ep_number,
        (SELECT MAX(e.updated_at) FROM episodes e
          WHERE e.drama_slug = d.slug AND e.status='ready')                 AS latest_ready_updated_at,
        (SELECT COUNT(*) FROM episodes e
          WHERE e.drama_slug = d.slug AND e.sync_status != 'clean')         AS non_clean_episodes,
        CASE WHEN d.sync_status != 'clean' THEN 1 ELSE 0 END                AS drama_non_clean
      FROM dramas d
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        synopsis = d.pop("synopsis") or ""
        # 80-char preview, ASCII-byte-naive (Chinese chars count as one)
        if len(synopsis) > 80:
            d["synopsis_preview"] = synopsis[:80] + "…"
        else:
            d["synopsis_preview"] = synopsis
        # Total non-clean items for the homepage card badge: drama itself plus
        # any non-clean episode rows. The two are independent dirty bits per
        # the design.
        d["non_clean_count"] = int(d.pop("non_clean_episodes") or 0) + int(d.pop("drama_non_clean") or 0)
        out.append(d)

    def _sort_key(row: dict):
        # dramas with ≥1 ready ep first (group_a=0), sorted by latest_ready_updated_at DESC;
        # then empty dramas (group_b=1) by drama_created_at DESC. Tie-break by slug ASC.
        if row["ep_count"] > 0:
            return (0, _neg_iso(row["latest_ready_updated_at"]), row["slug"])
        return (1, _neg_iso(row["drama_created_at"]), row["slug"])

    out.sort(key=_sort_key)
    return out


def _neg_iso(iso_str: str | None) -> str:
    """Helper for descending-by-ISO-string sort: invert lexicographic order."""
    if not iso_str:
        return "\xff"  # push null/empty to the end of DESC order (i.e. earliest)
    # Invert each character so lex-ascending becomes lex-descending. Cheap trick.
    return "".join(chr(255 - ord(c)) for c in iso_str)


def get_drama_full(slug: str) -> dict | None:
    """Aggregate read for the drama detail page. Returns a single dict consolidating:
      - drama row: slug, default_lang, created_at, updated_at
      - translations: {lang_code: {name, synopsis, poster}, ...}
      - tags:   [{slug, label}, ...]   (label localized per tag.default_lang)
      - actors: [{slug, name}, ...]    (name localized per actor.default_lang)
      - episodes: [{ep_number, episode_id, status, duration_ms, width, height,
                    cover_url, play_url, source_filename, error_message,
                    progress, can_retry, subtitle_count, updated_at}, ...]
                    ordered by ep_number ASC

    Returns None if the drama is unknown.
    """
    drama = get_drama(slug)
    if drama is None:
        return None
    out: dict = dict(drama)
    out["translations"] = list_drama_translations(slug)
    out["tags"] = list_drama_tags(slug)
    out["actors"] = list_drama_actors(slug)

    sql = """
      SELECT
        e.drama_slug,
        e.ep_number,
        e.episode_id,
        e.status,
        e.duration_ms,
        e.width,
        e.height,
        e.cover_url,
        e.play_url,
        e.source_filename,
        e.error_message,
        e.progress,
        (e.source_path IS NOT NULL) AS can_retry,
        e.sync_status,
        e.sync_error,
        e.last_synced_at,
        e.created_at,
        e.updated_at,
        (SELECT COUNT(*) FROM subtitles s WHERE s.episode_id=e.episode_id) AS subtitle_count
      FROM episodes e
      WHERE e.drama_slug=?
      ORDER BY e.ep_number ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, (slug,)).fetchall()
    out["episodes"] = [_apply_default_ladder(dict(r)) for r in rows]
    return out


def upsert_pending(
    *,
    drama_slug: str,
    ep_number: int,
    episode_id: str,
    duration_ms: int,
    cover_url: str,
    source_filename: str,
    width: int | None = None,
    height: int | None = None,
    source_path: str | None = None,
) -> str | None:
    """Insert a new pending row, or overwrite an existing (drama_slug, ep_number) row
    in place. On overwrite, created_at is preserved, error_message is cleared, DRM
    fields are cleared, and updated_at is refreshed.

    `source_path` is the temp upload file the pipeline will consume; it is kept
    on the row so a failed episode can be retried without a re-upload. On
    overwrite the *previous* `source_path` is returned (when it differs) so the
    caller can delete the now-orphaned file from a prior failed attempt.

    The drama row keyed by `drama_slug` MUST already exist (FK enforces this);
    the upload handler is responsible for the precondition check.
    """
    now = _now_iso()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id, created_at, source_path FROM episodes "
            "WHERE drama_slug=? AND ep_number=?",
            (drama_slug, ep_number),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO episodes (
                  drama_slug, ep_number, episode_id, status,
                  duration_ms, cover_url, width, height, source_filename,
                  source_path, progress, sync_status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, NULL, 'dirty', ?, ?)
                """,
                (
                    drama_slug, ep_number, episode_id,
                    duration_ms, cover_url, width, height, source_filename,
                    source_path, now, now,
                ),
            )
            return None
        else:
            # Re-upload: keep last_synced_at intact (the row's prior sync history
            # remains meaningful — operators may want "last synced 2 days ago,
            # then re-encoded today" visible). sync_status flips back to dirty
            # because the new content has not yet been pushed to prod.
            conn.execute(
                """
                UPDATE episodes SET
                  episode_id = ?,
                  status = 'pending',
                  duration_ms = ?,
                  cover_url = ?,
                  width = ?,
                  height = ?,
                  source_filename = ?,
                  source_path = ?,
                  progress = NULL,
                  play_url = NULL,
                  key_uri = NULL,
                  key_b64 = NULL,
                  iv_hex = NULL,
                  error_message = NULL,
                  sync_status = 'dirty',
                  sync_error = NULL,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    episode_id, duration_ms, cover_url,
                    width, height, source_filename, source_path,
                    now, existing["id"],
                ),
            )
        old_source = existing["source_path"]
        if old_source and old_source != source_path:
            return old_source
        return None


def set_status(
    episode_id: str,
    status: str,
    *,
    error_message: str | None = None,
    play_url: str | None = None,
    key_uri: str | None = None,
    key_b64: str | None = None,
    iv_hex: str | None = None,
) -> None:
    now = _now_iso()
    fields = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now]
    if error_message is not None:
        fields.append("error_message = ?"); params.append(error_message)
    else:
        fields.append("error_message = NULL")
    # `progress` is only meaningful mid-encode; every other transition clears it
    # so a stale "编码 720p" never lingers on a ready/failed/pending row.
    if status != "encoding":
        fields.append("progress = NULL")
    if play_url is not None:
        fields.append("play_url = ?"); params.append(play_url)
    if key_uri is not None:
        fields.append("key_uri = ?"); params.append(key_uri)
    if key_b64 is not None:
        fields.append("key_b64 = ?"); params.append(key_b64)
    if iv_hex is not None:
        fields.append("iv_hex = ?"); params.append(iv_hex)
    params.append(episode_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE episodes SET {', '.join(fields)} WHERE episode_id = ?",
            params,
        )


def set_episode_progress(episode_id: str, progress: str | None) -> None:
    """Update the free-text `progress` sub-status of an encoding episode.

    Called repeatedly by the pipeline worker as it moves through encode /
    encrypt / OSS-upload stages. `updated_at` is bumped so the admin UI's
    poll-driven refresh keeps surfacing the latest stage.
    """
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE episodes SET progress=?, updated_at=? WHERE episode_id=?",
            (progress, now, episode_id),
        )


def clear_episode_source_path(episode_id: str) -> None:
    """Drop the retained temp-upload pointer once an episode reaches `ready`.

    The worker deletes the file itself; this just clears the DB column so the
    admin UI stops offering a one-click retry for a now-succeeded episode.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE episodes SET source_path=NULL WHERE episode_id=?",
            (episode_id,),
        )


def set_dimensions(episode_id: str, width: int, height: int) -> bool:
    """回填 width / height（值为正整数）。仅在两列至少一个为 NULL 时更新；返回是否真的写了。

    给一次性回填脚本（`scripts/backfill_video_dimensions.py`）用 —— 不动 status / 不刷
    updated_at（避免老剧集"被推到剧目录顶端"）。
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"width/height must be positive: {width}x{height}")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE episodes SET width=?, height=? "
            "WHERE episode_id=? AND (width IS NULL OR height IS NULL)",
            (width, height, episode_id),
        )
        return (cur.rowcount or 0) > 0


def bump_updated_at(drama_slug: str, ep_number: int) -> None:
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE episodes SET updated_at=? WHERE drama_slug=? AND ep_number=?",
            (now, drama_slug, ep_number),
        )


# --- helpers used by the readers below to surface drama_name via translations --
# The drama's name lives at translations(entity_type='drama', entity_id=slug,
# lang_code=dramas.default_lang, field='name'). LEFT JOIN with COALESCE('') so
# a missing translation never makes the response null.
_DRAMA_NAME_JOIN = """
  LEFT JOIN translations dn
    ON dn.entity_type='drama'
    AND dn.entity_id=d.slug
    AND dn.lang_code=d.default_lang
    AND dn.field='name'
"""


def _apply_default_ladder(row: dict) -> dict:
    """Overwrite the row's `play_url` with one derived from `settings.default_ladder`.

    The DB column is informational (it reflects whatever was persisted at encode
    time). Outbound responses — both `/api/*` (SDK) and `/admin/*` (admin UI) —
    must agree on which rung is "default". Doing the override at the DB-helper
    boundary keeps every reader consistent without each route re-implementing it.

    Episodes that aren't `ready` (no `play_url` persisted) keep `None`.
    """
    if not row.get("play_url"):
        return row
    slug = row.get("drama_slug")
    ep_number = row.get("ep_number")
    if slug is None or ep_number is None:
        return row
    ladder = settings.default_ladder
    row["play_url"] = f"/videos/{slug}/ep-{ep_number}/{ladder}/media-{ladder}.m3u8"
    return row


def get_by_slug_ep(drama_slug: str, ep_number: int) -> dict | None:
    """Episode row joined with the drama's default-lang `name`. Returned dict
    includes `drama_name` (defaults to '' when no translation exists)."""
    sql = (
        "SELECT e.*, COALESCE(dn.value, '') AS drama_name FROM episodes e "
        "LEFT JOIN dramas d ON d.slug = e.drama_slug "
        + _DRAMA_NAME_JOIN +
        "WHERE e.drama_slug=? AND e.ep_number=?"
    )
    with _connect() as conn:
        row = conn.execute(sql, (drama_slug, ep_number)).fetchone()
    return _apply_default_ladder(dict(row)) if row else None


def list_all() -> list[dict]:
    """Admin episode list. Each row carries `drama_name` via the translations join."""
    sql = (
        "SELECT e.*, COALESCE(dn.value, '') AS drama_name FROM episodes e "
        "LEFT JOIN dramas d ON d.slug = e.drama_slug "
        + _DRAMA_NAME_JOIN +
        "ORDER BY datetime(e.created_at) DESC"
    )
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_apply_default_ladder(dict(r)) for r in rows]


def list_ready_by_slug(drama_slug: str) -> list[dict]:
    """Per-drama ready episodes."""
    sql = (
        "SELECT e.*, COALESCE(dn.value, '') AS drama_name FROM episodes e "
        "LEFT JOIN dramas d ON d.slug = e.drama_slug "
        + _DRAMA_NAME_JOIN +
        "WHERE e.drama_slug=? AND e.status='ready' "
        "ORDER BY e.ep_number ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, (drama_slug,)).fetchall()
    return [_apply_default_ladder(dict(r)) for r in rows]


def list_ready_dramas() -> list[dict]:
    """聚合剧目录视图。字段按 DramaSummary 需要投影；仅包含至少有一集 ready 的剧。

    drama_name 现在源自 translations 表（默认语言行）；wire-format 不变。
    """
    sql = """
      SELECT
        e.drama_slug,
        COALESCE(dn.value, '') AS drama_name,
        COUNT(*)               AS ep_count,
        MAX(e.ep_number)       AS latest_ep_number,
        MAX(e.updated_at)      AS last_updated_at,
        (SELECT e2.cover_url FROM episodes e2
           WHERE e2.drama_slug = e.drama_slug AND e2.status = 'ready'
           ORDER BY e2.ep_number ASC
           LIMIT 1)             AS poster_url
      FROM episodes e
      INNER JOIN dramas d ON d.slug = e.drama_slug
      LEFT JOIN translations dn
        ON dn.entity_type='drama' AND dn.entity_id=d.slug
        AND dn.lang_code=d.default_lang AND dn.field='name'
      WHERE e.status = 'ready'
      GROUP BY e.drama_slug, COALESCE(dn.value, '')
      ORDER BY last_updated_at DESC, e.drama_slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def delete_by_slug_ep(drama_slug: str, ep_number: int) -> bool:
    """删除 (drama_slug, ep_number) 对应的一行，返回是否真的删了（行存在则 True）。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM episodes WHERE drama_slug=? AND ep_number=?",
            (drama_slug, ep_number),
        )
        return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# tags CRUD + drama-tag junction (tag-library)
# ---------------------------------------------------------------------------


def create_tag(slug: str, default_lang: str, label: str) -> dict:
    """Atomically insert a `tags` row + the `(tag, slug, default_lang, 'label', label)`
    translation row. Validates inputs and raises typed exceptions.
    """
    if not _SLUG_RE.match(slug):
        raise TagValidationError("slug", "slug must match ^[a-z0-9][a-z0-9-]*$")
    label_trimmed = label.strip()
    if not label_trimmed:
        raise TagValidationError("label", "label must not be empty")
    lang = get_language(default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"default_lang '{default_lang}' is not a registered language"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"default_lang '{default_lang}' is registered but inactive"
        )

    now = _now_iso()
    with _connect() as conn:
        # Manual transaction: SQLite isolation_level=None (autocommit) + explicit BEGIN.
        conn.execute("BEGIN")
        try:
            try:
                conn.execute(
                    "INSERT INTO tags(slug, default_lang, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (slug, default_lang, now, now),
                )
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK")
                raise TagExistsError(f"tag with slug '{slug}' already exists") from e
            conn.execute(
                "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                "VALUES ('tag', ?, ?, 'label', ?)",
                (slug, default_lang, label_trimmed),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise

        row = conn.execute("SELECT * FROM tags WHERE slug=?", (slug,)).fetchone()
    return dict(row)


def get_tag(slug: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tags WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def list_tags() -> list[dict]:
    """All tags joined with default-lang label, available_langs (CSV → list),
    and usage_count. Ordered by `created_at DESC, slug ASC`.
    """
    sql = """
      SELECT
        t.slug,
        t.default_lang,
        t.created_at,
        t.updated_at,
        (SELECT value FROM translations
           WHERE entity_type='tag' AND entity_id=t.slug
                 AND lang_code=t.default_lang AND field='label') AS default_label,
        (SELECT GROUP_CONCAT(lang_code, ',') FROM (
            SELECT DISTINCT lang_code FROM translations
             WHERE entity_type='tag' AND entity_id=t.slug AND field='label'
             ORDER BY lang_code ASC
        )) AS available_langs_csv,
        (SELECT COUNT(*) FROM drama_tags WHERE tag_slug=t.slug) AS usage_count
      FROM tags t
      ORDER BY datetime(t.created_at) DESC, t.slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        csv = d.pop("available_langs_csv") or ""
        d["available_langs"] = sorted(csv.split(",")) if csv else []
        out.append(d)
    return out


def update_tag_default_lang(slug: str, new_default_lang: str) -> dict | None:
    """Switch a tag's default_lang. Pre-checks that a `label` translation exists
    for the new lang AND the new lang is active. Returns the updated row or
    None if the tag is unknown.
    """
    if get_tag(slug) is None:
        return None
    lang = get_language(new_default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"new default_lang '{new_default_lang}' is not a registered language"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"new default_lang '{new_default_lang}' is inactive"
        )
    with _connect() as conn:
        cov = conn.execute(
            "SELECT 1 FROM translations "
            "WHERE entity_type='tag' AND entity_id=? AND lang_code=? AND field='label'",
            (slug, new_default_lang),
        ).fetchone()
        if not cov:
            raise TagDefaultLangNotCoveredError(
                f"tag '{slug}' has no 'label' translation in '{new_default_lang}'; "
                f"upsert it first"
            )
        now = _now_iso()
        conn.execute(
            "UPDATE tags SET default_lang=?, updated_at=? WHERE slug=?",
            (new_default_lang, now, slug),
        )
        row = conn.execute("SELECT * FROM tags WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def delete_tag(slug: str) -> bool:
    """Atomically delete tag's translation rows + the tag row. Junction
    rows in `drama_tags` are removed by FK CASCADE. Returns True if a row
    was deleted, False if the slug was unknown.
    """
    with _connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM tags WHERE slug=?", (slug,)
        ).fetchone()
        if not existed:
            return False
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM translations WHERE entity_type='tag' AND entity_id=?",
                (slug,),
            )
            conn.execute("DELETE FROM tags WHERE slug=?", (slug,))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
    return True


def upsert_tag_translation(slug: str, lang_code: str, label: str) -> dict:
    """Upsert the `(entity_type='tag', entity_id=slug, lang_code, field='label')`
    translation row. Returns the row content (slug, lang_code, label).
    """
    if get_tag(slug) is None:
        raise TagNotFoundError(f"tag '{slug}' not found")
    label_trimmed = label.strip()
    if not label_trimmed:
        raise TagValidationError("label", "label must not be empty")
    lang = get_language(lang_code)
    if lang is None:
        raise LanguageNotFoundError(f"lang_code '{lang_code}' is not a registered language")
    if not lang["is_active"]:
        raise LanguageInactiveError(f"lang_code '{lang_code}' is inactive")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
            "VALUES ('tag', ?, ?, 'label', ?) "
            "ON CONFLICT(entity_type, entity_id, lang_code, field) DO UPDATE SET value=excluded.value",
            (slug, lang_code, label_trimmed),
        )
    return {"slug": slug, "lang_code": lang_code, "label": label_trimmed}


def delete_tag_translation(slug: str, lang_code: str) -> bool:
    """Delete one tag's translation in one language. Returns True if a row
    was deleted.

    Raises:
      TagNotFoundError if the tag is unknown.
      TagDefaultTranslationProtectedError if `lang_code` equals the tag's `default_lang`.
    """
    tag = get_tag(slug)
    if tag is None:
        raise TagNotFoundError(f"tag '{slug}' not found")
    if tag["default_lang"] == lang_code:
        raise TagDefaultTranslationProtectedError(
            f"cannot delete the default-lang ('{lang_code}') translation while the tag exists; "
            f"change the tag's default_lang first or delete the tag"
        )
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM translations WHERE entity_type='tag' AND entity_id=? "
            "AND lang_code=? AND field='label'",
            (slug, lang_code),
        )
        return (cur.rowcount or 0) > 0


def replace_drama_tags(drama_slug: str, tag_slugs: list[str]) -> None:
    """Replace the drama's tag set with exactly `tag_slugs` (no duplicates).

    Raises:
      DramaNotFoundError if the drama is unknown.
      TagNotFoundError if any tag slug in the list is unknown (no rows are
        modified — atomic).
    """
    if get_drama(drama_slug) is None:
        raise DramaNotFoundError(f"drama '{drama_slug}' not found")
    # Dedup + preserve order for predictable error reporting.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in tag_slugs:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    with _connect() as conn:
        # Validate every slug exists before any write.
        for s in deduped:
            row = conn.execute("SELECT 1 FROM tags WHERE slug=?", (s,)).fetchone()
            if not row:
                raise TagNotFoundError(f"tag '{s}' not found")
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM drama_tags WHERE drama_slug=?", (drama_slug,))
            for s in deduped:
                conn.execute(
                    "INSERT INTO drama_tags(drama_slug, tag_slug) VALUES (?, ?)",
                    (drama_slug, s),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise


def list_drama_tags(drama_slug: str) -> list[dict]:
    """Return `[{slug, label}]` where `label` is each tag's default-lang label.
    Ordered by tag_slug ASC."""
    sql = """
      SELECT
        dt.tag_slug AS slug,
        (SELECT value FROM translations
           WHERE entity_type='tag' AND entity_id=dt.tag_slug
                 AND lang_code=t.default_lang AND field='label') AS label
      FROM drama_tags dt
      INNER JOIN tags t ON t.slug = dt.tag_slug
      WHERE dt.drama_slug = ?
      ORDER BY dt.tag_slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, (drama_slug,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# actors CRUD + drama-actor junction (actor-library)
# ---------------------------------------------------------------------------


def create_actor(slug: str, default_lang: str, name: str) -> dict:
    """Atomically insert an `actors` row + the `(actor, slug, default_lang, 'name', name)`
    translation row.
    """
    if not _SLUG_RE.match(slug):
        raise ActorValidationError("slug", "slug must match ^[a-z0-9][a-z0-9-]*$")
    name_trimmed = name.strip()
    if not name_trimmed:
        raise ActorValidationError("name", "name must not be empty")
    lang = get_language(default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"default_lang '{default_lang}' is not a registered language"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"default_lang '{default_lang}' is registered but inactive"
        )

    now = _now_iso()
    with _connect() as conn:
        conn.execute("BEGIN")
        try:
            try:
                conn.execute(
                    "INSERT INTO actors(slug, default_lang, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (slug, default_lang, now, now),
                )
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK")
                raise ActorExistsError(f"actor with slug '{slug}' already exists") from e
            conn.execute(
                "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
                "VALUES ('actor', ?, ?, 'name', ?)",
                (slug, default_lang, name_trimmed),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise

        row = conn.execute("SELECT * FROM actors WHERE slug=?", (slug,)).fetchone()
    return dict(row)


def get_actor(slug: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM actors WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def list_actors() -> list[dict]:
    """All actors joined with default-lang name, available_langs, usage_count."""
    sql = """
      SELECT
        a.slug,
        a.default_lang,
        a.created_at,
        a.updated_at,
        (SELECT value FROM translations
           WHERE entity_type='actor' AND entity_id=a.slug
                 AND lang_code=a.default_lang AND field='name') AS default_name,
        (SELECT GROUP_CONCAT(lang_code, ',') FROM (
            SELECT DISTINCT lang_code FROM translations
             WHERE entity_type='actor' AND entity_id=a.slug AND field='name'
             ORDER BY lang_code ASC
        )) AS available_langs_csv,
        (SELECT COUNT(*) FROM drama_actors WHERE actor_slug=a.slug) AS usage_count
      FROM actors a
      ORDER BY datetime(a.created_at) DESC, a.slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        csv = d.pop("available_langs_csv") or ""
        d["available_langs"] = sorted(csv.split(",")) if csv else []
        out.append(d)
    return out


def update_actor_default_lang(slug: str, new_default_lang: str) -> dict | None:
    if get_actor(slug) is None:
        return None
    lang = get_language(new_default_lang)
    if lang is None:
        raise LanguageNotFoundError(
            f"new default_lang '{new_default_lang}' is not a registered language"
        )
    if not lang["is_active"]:
        raise LanguageInactiveError(
            f"new default_lang '{new_default_lang}' is inactive"
        )
    with _connect() as conn:
        cov = conn.execute(
            "SELECT 1 FROM translations "
            "WHERE entity_type='actor' AND entity_id=? AND lang_code=? AND field='name'",
            (slug, new_default_lang),
        ).fetchone()
        if not cov:
            raise ActorDefaultLangNotCoveredError(
                f"actor '{slug}' has no 'name' translation in '{new_default_lang}'; "
                f"upsert it first"
            )
        now = _now_iso()
        conn.execute(
            "UPDATE actors SET default_lang=?, updated_at=? WHERE slug=?",
            (new_default_lang, now, slug),
        )
        row = conn.execute("SELECT * FROM actors WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def delete_actor(slug: str) -> bool:
    with _connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM actors WHERE slug=?", (slug,)
        ).fetchone()
        if not existed:
            return False
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM translations WHERE entity_type='actor' AND entity_id=?",
                (slug,),
            )
            conn.execute("DELETE FROM actors WHERE slug=?", (slug,))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
    return True


def upsert_actor_translation(slug: str, lang_code: str, name: str) -> dict:
    if get_actor(slug) is None:
        raise ActorNotFoundError(f"actor '{slug}' not found")
    name_trimmed = name.strip()
    if not name_trimmed:
        raise ActorValidationError("name", "name must not be empty")
    lang = get_language(lang_code)
    if lang is None:
        raise LanguageNotFoundError(f"lang_code '{lang_code}' is not a registered language")
    if not lang["is_active"]:
        raise LanguageInactiveError(f"lang_code '{lang_code}' is inactive")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO translations(entity_type, entity_id, lang_code, field, value) "
            "VALUES ('actor', ?, ?, 'name', ?) "
            "ON CONFLICT(entity_type, entity_id, lang_code, field) DO UPDATE SET value=excluded.value",
            (slug, lang_code, name_trimmed),
        )
    return {"slug": slug, "lang_code": lang_code, "name": name_trimmed}


def delete_actor_translation(slug: str, lang_code: str) -> bool:
    actor = get_actor(slug)
    if actor is None:
        raise ActorNotFoundError(f"actor '{slug}' not found")
    if actor["default_lang"] == lang_code:
        raise ActorDefaultTranslationProtectedError(
            f"cannot delete the default-lang ('{lang_code}') translation while the actor exists"
        )
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM translations WHERE entity_type='actor' AND entity_id=? "
            "AND lang_code=? AND field='name'",
            (slug, lang_code),
        )
        return (cur.rowcount or 0) > 0


def replace_drama_actors(drama_slug: str, actor_slugs: list[str]) -> None:
    if get_drama(drama_slug) is None:
        raise DramaNotFoundError(f"drama '{drama_slug}' not found")
    seen: set[str] = set()
    deduped: list[str] = []
    for s in actor_slugs:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    with _connect() as conn:
        for s in deduped:
            row = conn.execute("SELECT 1 FROM actors WHERE slug=?", (s,)).fetchone()
            if not row:
                raise ActorNotFoundError(f"actor '{s}' not found")
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM drama_actors WHERE drama_slug=?", (drama_slug,))
            for s in deduped:
                conn.execute(
                    "INSERT INTO drama_actors(drama_slug, actor_slug) VALUES (?, ?)",
                    (drama_slug, s),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise


def list_drama_actors(drama_slug: str) -> list[dict]:
    sql = """
      SELECT
        da.actor_slug AS slug,
        (SELECT value FROM translations
           WHERE entity_type='actor' AND entity_id=da.actor_slug
                 AND lang_code=a.default_lang AND field='name') AS name
      FROM drama_actors da
      INNER JOIN actors a ON a.slug = da.actor_slug
      WHERE da.drama_slug = ?
      ORDER BY da.actor_slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, (drama_slug,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# subtitles CRUD (episode-subtitles)
# ---------------------------------------------------------------------------


def upsert_subtitle(episode_id: str, lang_code: str, file_url: str) -> dict:
    """Upsert a subtitle row keyed by (episode_id, lang_code). Sets `uploaded_at`
    to the current timestamp on every call (insert or update). Returns the
    resulting row.

    Caller is responsible for ensuring `episode_id` exists and `lang_code`
    references an active language; the DB-layer FK is the safety net.
    """
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO subtitles(episode_id, lang_code, file_url, uploaded_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(episode_id, lang_code) DO UPDATE SET "
            "  file_url=excluded.file_url, uploaded_at=excluded.uploaded_at",
            (episode_id, lang_code, file_url, now),
        )
        row = conn.execute(
            "SELECT * FROM subtitles WHERE episode_id=? AND lang_code=?",
            (episode_id, lang_code),
        ).fetchone()
    return dict(row) if row else {}


def list_subtitles_for_episode(episode_id: str) -> list[dict]:
    """Return `[{lang_code, label, file_url, uploaded_at}, ...]` joined with
    `languages.display_label` as `label`. Ordered by `lang_code ASC`.
    """
    sql = """
      SELECT s.lang_code, s.file_url, s.uploaded_at,
             l.display_label AS label
      FROM subtitles s
      INNER JOIN languages l ON l.code = s.lang_code
      WHERE s.episode_id = ?
      ORDER BY s.lang_code ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, (episode_id,)).fetchall()
    return [dict(r) for r in rows]


def list_subtitles_for_slug_ep(drama_slug: str, ep_number: int) -> list[dict]:
    """Convenience wrapper: resolve `episode_id` from `(drama_slug, ep_number)`
    then call `list_subtitles_for_episode`. Returns `[]` if the episode is
    unknown — read-side endpoints don't need to distinguish "no episode" from
    "no subtitles."
    """
    episode_id = f"{drama_slug}-ep-{ep_number}"
    return list_subtitles_for_episode(episode_id)


def delete_subtitle(episode_id: str, lang_code: str) -> tuple[bool, str | None]:
    """Delete the subtitle row for `(episode_id, lang_code)`. Returns
    `(deleted, file_url)`:
      - `(True, file_url)`  — row existed and was deleted; caller should
        unlink the on-disk file at the URL's mapped path.
      - `(False, None)`     — no row matched.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT file_url FROM subtitles WHERE episode_id=? AND lang_code=?",
            (episode_id, lang_code),
        ).fetchone()
        if row is None:
            return (False, None)
        conn.execute(
            "DELETE FROM subtitles WHERE episode_id=? AND lang_code=?",
            (episode_id, lang_code),
        )
    return (True, row["file_url"])


# ---------------------------------------------------------------------------
# Misc startup
# ---------------------------------------------------------------------------


def reap_orphaned_encoding() -> int:
    """Flip any row stuck in status=encoding (orphaned by prior process crash)
    to status=failed. Called from the lifespan startup hook.
    """
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE episodes SET status='failed', error_message='orphaned by restart', progress=NULL, updated_at=? WHERE status='encoding'",
            (now,),
        )
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# business-server-sync (step 6): sync state machine helpers
# ---------------------------------------------------------------------------

# Allowed values for sync_status. Validated at the helper boundary so callers
# don't smuggle in typos.
_SYNC_STATUSES = {"dirty", "syncing", "clean", "sync_failed", "pending_delete"}


def set_drama_sync_status(
    slug: str,
    status: str,
    *,
    error: str | None = None,
    last_synced_at: str | None = None,
) -> None:
    """Write the drama row's sync columns. Refreshes `updated_at`.

    `error` is set verbatim (caller passes `None` to clear it). `last_synced_at`
    is set verbatim too — the worker passes a fresh ISO timestamp on success.
    """
    if status not in _SYNC_STATUSES:
        raise ValueError(f"invalid sync_status: {status!r}")
    now = _now_iso()
    with _connect() as conn:
        if last_synced_at is not None:
            conn.execute(
                "UPDATE dramas SET sync_status=?, sync_error=?, last_synced_at=?, "
                "updated_at=? WHERE slug=?",
                (status, error, last_synced_at, now, slug),
            )
        else:
            conn.execute(
                "UPDATE dramas SET sync_status=?, sync_error=?, updated_at=? WHERE slug=?",
                (status, error, now, slug),
            )


def set_episode_sync_status(
    slug: str,
    ep_number: int,
    status: str,
    *,
    error: str | None = None,
    last_synced_at: str | None = None,
) -> None:
    if status not in _SYNC_STATUSES:
        raise ValueError(f"invalid sync_status: {status!r}")
    now = _now_iso()
    with _connect() as conn:
        if last_synced_at is not None:
            conn.execute(
                "UPDATE episodes SET sync_status=?, sync_error=?, last_synced_at=?, "
                "updated_at=? WHERE drama_slug=? AND ep_number=?",
                (status, error, last_synced_at, now, slug, ep_number),
            )
        else:
            conn.execute(
                "UPDATE episodes SET sync_status=?, sync_error=?, updated_at=? "
                "WHERE drama_slug=? AND ep_number=?",
                (status, error, now, slug, ep_number),
            )


def mark_drama_dirty(slug: str) -> None:
    """Flip drama to dirty unless it's currently `pending_delete`. Refreshes
    `updated_at`. No-op if drama doesn't exist.
    """
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE dramas SET sync_status='dirty', updated_at=? "
            "WHERE slug=? AND sync_status != 'pending_delete'",
            (now, slug),
        )


def mark_episode_dirty(slug: str, ep_number: int) -> None:
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE episodes SET sync_status='dirty', updated_at=? "
            "WHERE drama_slug=? AND ep_number=? AND sync_status != 'pending_delete'",
            (now, slug, ep_number),
        )


def cascade_dirty_dramas_via_tag(tag_slug: str) -> int:
    """Flip every drama referencing `tag_slug` to dirty (skipping pending_delete).
    Returns rowcount.
    """
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE dramas SET sync_status='dirty', updated_at=? "
            "WHERE slug IN (SELECT drama_slug FROM drama_tags WHERE tag_slug=?) "
            "  AND sync_status != 'pending_delete'",
            (now, tag_slug),
        )
        return cur.rowcount or 0


def cascade_dirty_dramas_via_actor(actor_slug: str) -> int:
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE dramas SET sync_status='dirty', updated_at=? "
            "WHERE slug IN (SELECT drama_slug FROM drama_actors WHERE actor_slug=?) "
            "  AND sync_status != 'pending_delete'",
            (now, actor_slug),
        )
        return cur.rowcount or 0


def cascade_dirty_dramas_via_language(lang_code: str) -> int:
    """A language label change cascades to every drama with at least one
    episode subtitle in that language (the subtitle picker shows the label,
    so prod must learn the new label)."""
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE dramas SET sync_status='dirty', updated_at=? "
            "WHERE slug IN ("
            "  SELECT DISTINCT e.drama_slug FROM subtitles s "
            "  INNER JOIN episodes e ON e.episode_id = s.episode_id "
            "  WHERE s.lang_code = ?"
            ") AND sync_status != 'pending_delete'",
            (now, lang_code),
        )
        return cur.rowcount or 0


def list_episodes_needing_sync(slug: str) -> list[int]:
    """ep_numbers (ASC) where the episode row is dirty or pending_delete."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ep_number FROM episodes "
            "WHERE drama_slug=? AND sync_status IN ('dirty','pending_delete') "
            "ORDER BY ep_number ASC",
            (slug,),
        ).fetchall()
    return [r["ep_number"] for r in rows]


def list_dramas_needing_sync() -> list[dict]:
    """All drama rows where sync_status is non-clean. Ordered by updated_at DESC."""
    sql = """
      SELECT d.slug, d.default_lang, d.sync_status, d.sync_error,
             d.last_synced_at, d.created_at, d.updated_at,
             COALESCE(
               (SELECT t.value FROM translations t
                 WHERE t.entity_type='drama' AND t.entity_id=d.slug
                   AND t.lang_code=d.default_lang AND t.field='name'),
               ''
             ) AS name
        FROM dramas d
       WHERE d.sync_status != 'clean'
       ORDER BY datetime(d.updated_at) DESC, d.slug ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def list_episodes_needing_sync_all() -> list[dict]:
    """All episode rows where sync_status is non-clean (across all dramas)."""
    sql = """
      SELECT e.drama_slug, e.ep_number, e.episode_id, e.status,
             e.sync_status, e.sync_error, e.last_synced_at,
             e.created_at, e.updated_at
        FROM episodes e
       WHERE e.sync_status != 'clean'
       ORDER BY datetime(e.updated_at) DESC, e.drama_slug ASC, e.ep_number ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def count_non_clean_sync_rows() -> int:
    """Count of drama+episode rows whose sync_status != 'clean'. Used by
    /admin/sync/summary for the nav-bar polling.
    """
    with _connect() as conn:
        d = conn.execute(
            "SELECT COUNT(*) AS n FROM dramas WHERE sync_status != 'clean'"
        ).fetchone()
        e = conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE sync_status != 'clean'"
        ).fetchone()
    return int(d["n"]) + int(e["n"])


def physical_delete_episode(slug: str, ep_number: int) -> bool:
    """Hard-delete an episode row + its subtitle rows (CASCADE handles the latter
    via FK). Only the sync worker calls this, after a successful delete-sync.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM episodes WHERE drama_slug=? AND ep_number=?",
            (slug, ep_number),
        )
        return (cur.rowcount or 0) > 0


def physical_delete_drama(slug: str) -> bool:
    """Hard-delete a drama row + its translations + (per-FK CASCADE) its
    drama_tags / drama_actors links. Episodes are gone by the time we get here
    (the worker physically-deletes each episode first, or no synced episodes
    ever existed). Returns True if the row existed.
    """
    with _connect() as conn:
        ep_count = conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE drama_slug=?", (slug,)
        ).fetchone()["n"]
        if ep_count > 0:
            raise RuntimeError(
                f"physical_delete_drama: drama {slug!r} still has {ep_count} "
                f"episode rows; the sync worker must delete them first"
            )
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM translations WHERE entity_type='drama' AND entity_id=?",
                (slug,),
            )
            cur = conn.execute("DELETE FROM dramas WHERE slug=?", (slug,))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        return (cur.rowcount or 0) > 0


def reap_orphaned_syncing() -> int:
    """Flip every drama / episode row stuck in `sync_status='syncing'` to
    `sync_failed` with a marker error. Called once at lifespan startup —
    mirrors `reap_orphaned_encoding` for the pipeline worker.
    """
    now = _now_iso()
    n = 0
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE dramas SET sync_status='sync_failed', "
            "  sync_error='orphaned by restart', updated_at=? "
            "WHERE sync_status='syncing'",
            (now,),
        )
        n += cur.rowcount or 0
        cur = conn.execute(
            "UPDATE episodes SET sync_status='sync_failed', "
            "  sync_error='orphaned by restart', updated_at=? "
            "WHERE sync_status='syncing'",
            (now,),
        )
        n += cur.rowcount or 0
    return n


def get_drama_with_sync(slug: str) -> dict | None:
    """Like get_drama() but includes the sync columns. Used by the sync worker
    when it needs to read the row back as part of a job.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT slug, default_lang, sync_status, sync_error, last_synced_at, "
            "free_episodes, created_at, updated_at FROM dramas WHERE slug=?",
            (slug,),
        ).fetchone()
    return dict(row) if row else None


def list_translations_for_entity(
    entity_type: str, entity_id: str, field: str
) -> dict[str, str]:
    """Return `{lang_code: value}` for every (entity_type, entity_id, field)
    translation. Used by the sync payload builder to assemble multilingual
    name / label maps for tags / actors.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lang_code, value FROM translations "
            "WHERE entity_type=? AND entity_id=? AND field=? "
            "ORDER BY lang_code ASC",
            (entity_type, entity_id, field),
        ).fetchall()
    return {r["lang_code"]: r["value"] for r in rows}


def list_languages_used_by_drama(slug: str) -> list[str]:
    """Union of every lang_code referenced (transitively) by a drama:
      - drama-level translations (name / synopsis / poster)
      - tag translations (label) for the drama's tags
      - actor translations (name) for the drama's actors
      - subtitle lang_codes for the drama's episodes
    Returned sorted ASC. Used by the sync payload builder to compose the
    `languages` array sent to the business server.
    """
    sql = """
      SELECT DISTINCT lang_code FROM (
        SELECT lang_code FROM translations
         WHERE entity_type='drama' AND entity_id=?
        UNION
        SELECT lang_code FROM translations
         WHERE entity_type='tag' AND entity_id IN (
           SELECT tag_slug FROM drama_tags WHERE drama_slug=?
         )
        UNION
        SELECT lang_code FROM translations
         WHERE entity_type='actor' AND entity_id IN (
           SELECT actor_slug FROM drama_actors WHERE drama_slug=?
         )
        UNION
        SELECT s.lang_code FROM subtitles s
         INNER JOIN episodes e ON e.episode_id = s.episode_id
         WHERE e.drama_slug=?
      )
      ORDER BY lang_code ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, (slug, slug, slug, slug)).fetchall()
    return [r["lang_code"] for r in rows]
