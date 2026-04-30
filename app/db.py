import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  drama_slug       TEXT    NOT NULL,
  drama_name       TEXT    NOT NULL,
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
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  UNIQUE(drama_slug, ep_number)
);
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
        # 老库（升级前不含 width / height 列）幂等加列。SQLite 没有 ADD COLUMN IF
        # NOT EXISTS，捕 OperationalError 静默通过即可。
        for col in ("width", "height"):
            try:
                conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_pending(
    *,
    drama_slug: str,
    drama_name: str,
    ep_number: int,
    episode_id: str,
    duration_ms: int,
    cover_url: str,
    source_filename: str,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Insert a new pending row, or overwrite an existing (drama_slug, ep_number) row
    in place. On overwrite, created_at is preserved, error_message is cleared, DRM
    fields are cleared, and updated_at is refreshed.

    width / height 是源视频 codec dimension（episode-video-dimensions change 引入）。
    可空：旧调用方不传时保持 NULL，新上传 handler 会填具体值。
    """
    now = _now_iso()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM episodes WHERE drama_slug=? AND ep_number=?",
            (drama_slug, ep_number),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO episodes (
                  drama_slug, drama_name, ep_number, episode_id, status,
                  duration_ms, cover_url, width, height, source_filename,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drama_slug, drama_name, ep_number, episode_id,
                    duration_ms, cover_url, width, height, source_filename,
                    now, now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE episodes SET
                  drama_name = ?,
                  episode_id = ?,
                  status = 'pending',
                  duration_ms = ?,
                  cover_url = ?,
                  width = ?,
                  height = ?,
                  source_filename = ?,
                  play_url = NULL,
                  key_uri = NULL,
                  key_b64 = NULL,
                  iv_hex = NULL,
                  error_message = NULL,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    drama_name, episode_id, duration_ms, cover_url,
                    width, height, source_filename,
                    now, existing["id"],
                ),
            )


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


def get_by_slug_ep(drama_slug: str, ep_number: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM episodes WHERE drama_slug=? AND ep_number=?",
            (drama_slug, ep_number),
        ).fetchone()
    return dict(row) if row else None


def list_all() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes ORDER BY datetime(created_at) DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_ready_by_slug(drama_slug: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes WHERE drama_slug=? AND status='ready' "
            "ORDER BY ep_number ASC",
            (drama_slug,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_ready_dramas() -> list[dict]:
    """聚合剧目录视图。字段按 DramaSummary 需要投影；仅包含至少有一集 ready 的剧。"""
    sql = """
      SELECT
        e.drama_slug,
        MAX(e.drama_name)  AS drama_name,
        COUNT(*)           AS ep_count,
        MAX(e.ep_number)   AS latest_ep_number,
        MAX(e.updated_at)  AS last_updated_at,
        (SELECT e2.cover_url FROM episodes e2
           WHERE e2.drama_slug = e.drama_slug AND e2.status = 'ready'
           ORDER BY e2.ep_number ASC
           LIMIT 1)         AS poster_url
      FROM episodes e
      WHERE e.status = 'ready'
      GROUP BY e.drama_slug
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


def count_by_slug(drama_slug: str) -> int:
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE drama_slug=?",
            (drama_slug,),
        ).fetchone()
    return r[0] if r else 0


def reap_orphaned_encoding() -> int:
    """Flip any row stuck in status=encoding (orphaned by prior process crash)
    to status=failed. Called from the lifespan startup hook.
    """
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE episodes SET status='failed', error_message='orphaned by restart', updated_at=? WHERE status='encoding'",
            (now,),
        )
        return cur.rowcount or 0
