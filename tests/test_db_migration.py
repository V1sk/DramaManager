"""Verify init_db() ALTERs in width / height on legacy tables without losing data.

Spec scenarios:
  - 老库升级后两列存在但老行为 NULL
  - 重复启动不报错
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


LEGACY_SCHEMA = """
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
  source_filename  TEXT,
  error_message    TEXT,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  UNIQUE(drama_slug, ep_number)
);
"""


def _seed_legacy_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        """
        INSERT INTO episodes (
          drama_slug, drama_name, ep_number, episode_id, status,
          duration_ms, play_url, key_uri, key_b64, iv_hex,
          cover_url, source_filename, error_message,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            "oldslug", "老剧", 1, "oldslug-ep-1",
            120000, "/videos/oldslug/ep-1/720p/media-720p.m3u8",
            "/drm/oldslug/ep-1/key", "AAECAwQFBgcICQoLDA0ODw==",
            "abcdef0123456789abcdef0123456789",
            "/videos/oldslug/ep-1/cover.jpg", "src.mp4",
            "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db_path = tmp / "hls.db"
        _seed_legacy_db(db_path)

        # 用 init_db 在该路径上跑一次（通过环境变量让 settings 指向它）
        os.environ["DB_PATH"] = str(db_path)
        os.environ["OUT_DIR"] = str(tmp / "out")
        os.environ["UPLOAD_TMP_DIR"] = str(tmp / "tmp")
        # 强制重新 import settings + db
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        from app import db  # noqa: E402
        db.init_db()

        # 列存在
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
        assert "width" in cols, f"width column missing; got {cols}"
        assert "height" in cols, f"height column missing; got {cols}"

        # 老行字段透传 + width/height = NULL
        row = dict(zip(
            [c[0] for c in conn.execute("SELECT * FROM episodes LIMIT 1").description],
            conn.execute("SELECT * FROM episodes LIMIT 1").fetchone(),
        ))
        assert row["drama_slug"] == "oldslug"
        assert row["status"] == "ready"
        assert row["duration_ms"] == 120000
        assert row["width"] is None
        assert row["height"] is None
        conn.close()

        # 重复 init_db 不报错
        db.init_db()
        db.init_db()

        # 新行通过 upsert_pending 正常写入 width / height
        db.upsert_pending(
            drama_slug="newslug",
            drama_name="新剧",
            ep_number=1,
            episode_id="newslug-ep-1",
            duration_ms=60000,
            cover_url="/videos/newslug/ep-1/cover.jpg",
            source_filename="src.mp4",
            width=720,
            height=1280,
        )
        new_row = db.get_by_slug_ep("newslug", 1)
        assert new_row["width"] == 720
        assert new_row["height"] == 1280

        # 不传 width / height 时回退到 None（兼容潜在旧调用方）
        db.upsert_pending(
            drama_slug="defaultcase",
            drama_name="默认",
            ep_number=1,
            episode_id="defaultcase-ep-1",
            duration_ms=30000,
            cover_url="/videos/defaultcase/ep-1/cover.jpg",
            source_filename="src.mp4",
        )
        defrow = db.get_by_slug_ep("defaultcase", 1)
        assert defrow["width"] is None
        assert defrow["height"] is None

        print("OK legacy table ALTERed; old rows null; new rows accept width/height; idempotent")


if __name__ == "__main__":
    main()
