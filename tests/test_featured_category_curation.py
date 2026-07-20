import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _setup_env(tmp_path: Path, *, sync_enabled: bool = True) -> None:
    os.environ["OUT_DIR"] = str(tmp_path / "out")
    os.environ["DB_PATH"] = str(tmp_path / "hls.db")
    os.environ["UPLOAD_TMP_DIR"] = str(tmp_path / "tmp")
    os.environ["STORAGE_PROVIDER"] = "none"
    os.environ["SESSION_SECRET_KEY"] = "test-secret-key"
    os.environ["ADMIN_INITIAL_PASSWORD"] = "test-admin-pw"
    os.environ["BUSINESS_SYNC_BASE_URL"] = (
        "https://business.invalid" if sync_enabled else ""
    )
    os.environ["BUSINESS_SYNC_API_KEY"] = "test-sync-key" if sync_enabled else ""


def _reset_app_modules() -> None:
    for module_name in [name for name in sys.modules if name.startswith("app")]:
        del sys.modules[module_name]


def _seed_dramas(db, *slugs: str) -> None:
    db.init_db()
    if db.get_language("zh-rCN") is None:
        db.create_language("zh-rCN", "简体中文")
    for slug in slugs:
        if db.get_drama(slug) is None:
            db.create_drama(slug, f"剧-{slug}", "zh-rCN")


def test_ordered_category_replace_is_atomic_and_drama_editor_appends(tmp_path):
    _setup_env(tmp_path)
    _reset_app_modules()
    from app import db

    _seed_dramas(db, "a", "b", "c")
    assert db.FEATURED_CATEGORIES == ("recommend", "new", "hot", "exclusive")
    assert db.get_featured_category_sync_state()["is_dirty"] is False

    assert db.replace_featured_category_members(
        "recommend", ["b", "a", "b"],
    ) == ["b", "a"]
    assert db.list_featured_category_slugs("recommend") == ["b", "a"]
    assert db.get_featured_category_sync_state()["is_dirty"] is True

    db.mark_featured_categories_synced()
    assert db.get_featured_category_sync_state()["is_dirty"] is False
    db.replace_featured_category_members("recommend", ["b", "a", "b"])
    assert db.get_featured_category_sync_state()["is_dirty"] is False

    with pytest.raises(db.DramaNotFoundError):
        db.replace_featured_category_members("recommend", ["c", "missing"])
    assert db.list_featured_category_slugs("recommend") == ["b", "a"]
    assert db.get_featured_category_sync_state()["is_dirty"] is False

    db.set_drama_sync_status("c", "clean")
    db.replace_drama_featured_categories("c", ["recommend", "hot"])
    assert db.list_featured_category_slugs("recommend") == ["b", "a", "c"]
    assert db.list_featured_category_slugs("hot") == ["c"]
    assert db.get_drama_with_sync("c")["sync_status"] == "clean"

    db.replace_drama_featured_categories("a", ["hot"])
    assert db.list_featured_category_slugs("recommend") == ["b", "c"]
    assert db.list_featured_category_slugs("hot") == ["c", "a"]

    db.mark_featured_categories_synced()
    deleted, ep_count = db.delete_drama("b")
    assert deleted is True and ep_count == 0
    assert db.get_featured_category_sync_state()["is_dirty"] is True


def test_legacy_featured_table_migrates_idempotently(tmp_path):
    _setup_env(tmp_path)
    _reset_app_modules()
    from app import db
    from app.config import settings

    _seed_dramas(db, "a", "b")
    db.replace_featured_category_members("hot", ["b", "a"])

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP INDEX idx_featured_category_order")
        conn.execute("ALTER TABLE drama_featured_categories RENAME TO featured_ordered")
        conn.execute(
            """
            CREATE TABLE drama_featured_categories (
              drama_slug TEXT NOT NULL,
              category TEXT NOT NULL CHECK(category IN ('hot','new','exclusive')),
              updated_at TEXT NOT NULL,
              PRIMARY KEY (drama_slug, category),
              FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "INSERT INTO drama_featured_categories(drama_slug, category, updated_at) "
            "SELECT drama_slug, category, updated_at FROM featured_ordered"
        )
        conn.execute("DROP TABLE featured_ordered")
        conn.execute("DROP TABLE featured_category_sync_state")

    db.init_db()
    first = db.list_featured_category_slugs("hot")
    assert set(first) == {"a", "b"}
    with sqlite3.connect(settings.db_path) as conn:
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(drama_featured_categories)")
        ]
        positions = [
            row[0]
            for row in conn.execute(
                "SELECT sort_order FROM drama_featured_categories "
                "WHERE category='hot' ORDER BY sort_order"
            )
        ]
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='drama_featured_categories'"
        ).fetchone()[0]
    assert "sort_order" in columns
    assert positions == [0, 1]
    assert "recommend" in table_sql
    assert db.get_featured_category_sync_state()["is_dirty"] is True

    db.init_db()
    assert db.list_featured_category_slugs("hot") == first


def test_featured_snapshot_is_dedicated_from_drama_payload(tmp_path):
    _setup_env(tmp_path)
    _reset_app_modules()
    from app import db
    from app.sync import build_drama_payload, build_featured_categories_payload

    _seed_dramas(db, "a", "b")
    db.replace_featured_category_members("recommend", ["b", "a"])
    db.replace_featured_category_members("exclusive", ["a"])

    assert "featured_categories" not in build_drama_payload("a")
    assert build_featured_categories_payload() == {
        "categories": {
            "recommend": ["b", "a"],
            "new": [],
            "hot": [],
            "exclusive": ["a"],
        },
    }


def test_featured_sync_button_highlights_only_when_dirty(tmp_path):
    _setup_env(tmp_path)
    _reset_app_modules()
    from fastapi import FastAPI
    from starlette.requests import Request

    from app import db
    from app.routers import admin

    _seed_dramas(db, "a")
    test_app = FastAPI()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/featured-categories",
        "raw_path": b"/admin/featured-categories",
        "query_string": b"",
        "headers": [],
        "client": ("test", 123),
        "server": ("testserver", 80),
        "app": test_app,
    }

    clean_html = asyncio.run(
        admin.admin_featured_categories_page(Request(scope))
    ).body.decode()
    assert 'class="btn btn-secondary"' in clean_html
    assert 'data-dirty="false"' in clean_html

    db.replace_featured_category_members("recommend", ["a"])
    dirty_html = asyncio.run(
        admin.admin_featured_categories_page(Request(scope))
    ).body.decode()
    assert 'class="btn btn-primary"' in dirty_html
    assert 'data-dirty="true"' in dirty_html


def test_featured_category_page_uses_live_drag_sort_with_keyboard_fallback(tmp_path):
    _setup_env(tmp_path)
    _reset_app_modules()
    from fastapi import FastAPI
    from starlette.requests import Request

    from app import db
    from app.routers import admin

    _seed_dramas(db, "a", "b")
    db.replace_featured_category_members("recommend", ["a", "b"])
    test_app = FastAPI()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/featured-categories",
        "raw_path": b"/admin/featured-categories",
        "query_string": b"",
        "headers": [],
        "client": ("test", 123),
        "server": ("testserver", 80),
        "app": test_app,
    }

    html = asyncio.run(
        admin.admin_featured_categories_page(Request(scope))
    ).body.decode()

    assert html.count("data-drag-handle data-category=") == 2
    assert 'data-sort-list="recommend"' in html
    assert html.count("data-sort-item") >= 2
    assert "drag_indicator" in html
    assert "按住上下拖动排序" in html
    assert 'data-action="up"' not in html
    assert 'data-action="down"' not in html

    assert "addEventListener('pointerdown'" in html
    assert "addEventListener('pointermove'" in html
    assert "addEventListener('pointerup'" in html
    assert "sortList.setPointerCapture(ev.pointerId)" in html
    assert "handle.setPointerCapture(ev.pointerId)" not in html
    assert "drag.sortList.hasPointerCapture(ev.pointerId)" in html
    assert "activeSortDrag?.sortList === ev.target" in html
    assert "insertBefore" in html
    assert "function captureSortPositions(sortList)" in html
    assert "function animateSortShift(sortList, previousPositions)" in html
    assert "item.getAnimations()" in html
    assert "item.animate(" in html
    assert "duration: 160" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "animateSortShift(drag.sortList, previousPositions)" in html
    assert "animateSortShift(sortList, previousPositions)" in html
    assert "await persistSortOrder" in html
    assert "restoreSortOrder(sortList, previousOrder)" in html
    assert "['ArrowUp', 'ArrowDown']" in html
    assert "markFeaturedSyncDirty()" in html


def test_featured_sync_route_calls_dedicated_business_endpoint(tmp_path, monkeypatch):
    _setup_env(tmp_path)
    _reset_app_modules()
    from app import db, sync_client
    from app.routers import sync as sync_router

    _seed_dramas(db, "a")
    db.replace_featured_category_members("hot", ["a"])
    captured = {}

    async def fake_call_business(method, path, *, json=None):
        captured.update(method=method, path=path, json=json)
        return {"ok": True}

    monkeypatch.setattr(sync_client, "call_business", fake_call_business)
    response = asyncio.run(sync_router.sync_featured_categories())
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["sync_state"]["is_dirty"] is False
    assert db.get_featured_category_sync_state()["is_dirty"] is False
    assert captured == {
        "method": "PUT",
        "path": "/sync/featured-categories",
        "json": {
            "categories": {
                "recommend": [],
                "new": [],
                "hot": ["a"],
                "exclusive": [],
            },
        },
    }
