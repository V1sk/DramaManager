"""TestClient verification for `_row_to_episode_info` URL shape + schema validation.

The HLS server is for local審片 preview only, so all SDK URLs are now
**always** business-host-relative regardless of `STORAGE_PROVIDER`. The
production CDN base lives on the business server (`MEDIA_BASE_URL`) and is
applied at serve-time.

Covers:
  - videoTracks[].url / coverUrl / drm.keyUri 都是相对路径
  - 单集端点和列表端点 payload 逐字节一致
  - schema 严格校验通过（episode-info-schema.json, uri-reference）

Filename kept as-is (`test_api_oss_modes.py`) for git-blame continuity, but
content no longer reflects "modes" — there's only one mode now.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _setup_env(tmp: Path) -> None:
    os.environ["OUT_DIR"] = str(tmp / "out")
    os.environ["DB_PATH"] = str(tmp / "hls.db")
    os.environ["UPLOAD_TMP_DIR"] = str(tmp / "tmp")
    # Storage mode no longer affects API URL shape — explicitly drop both knobs
    # so we exercise the "no provider" default branch without leftover env
    # bleeding in from the shell that invoked pytest.
    os.environ.pop("OSS_ENABLED", None)
    os.environ.pop("STORAGE_PROVIDER", None)


def _seed_ready_row(slug: str, ep: int) -> dict:
    """直接走 db helper 塞一行 ready 记录。返回 row dict。"""
    # 必须在 _setup_env 之后 import，让 settings 拿到正确路径
    from app import db
    db.init_db()
    if db.get_language("zh-rCN") is None:
        db.create_language(code="zh-rCN", display_label="简体中文")
    if db.get_drama(slug) is None:
        db.create_drama(slug=slug, name="测试剧", default_lang="zh-rCN")
    db.upsert_pending(
        drama_slug=slug,
        ep_number=ep,
        episode_id=f"{slug}-ep-{ep}",
        duration_ms=120000,
        cover_url=f"/videos/{slug}/ep-{ep}/cover.jpg",
        source_filename="x.mp4",
    )
    db.set_status(
        episode_id=f"{slug}-ep-{ep}",
        status="ready",
        play_url=f"/videos/{slug}/ep-{ep}/720p/media-720p.m3u8",
        key_uri=f"/drm/{slug}/ep-{ep}/key",
        key_b64="AAECAwQFBgcICQoLDA0ODw==",  # 16 bytes b64
        iv_hex="abcdef0123456789abcdef0123456789",
    )
    return db.get_by_slug_ep(slug, ep)


def _client():
    """每次重新 import 让 module-level settings 重新加载。"""
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _validator():
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((REPO_ROOT / "episode-info-schema.json").read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def run_relative_url_case():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_env(tmp)
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        _seed_ready_row("ly", 3)
        client = _client()

        r = client.get("/api/episodes/ly/3")
        assert r.status_code == 200, r.text
        single = r.json()

        # videoTracks: all three rungs, business-host relative URLs
        tracks = single["videoTracks"]
        assert [t["id"] for t in tracks] == ["high", "mid", "low"]
        assert tracks[0]["url"] == "/videos/ly/ep-3/1080p/media-1080p.m3u8"
        assert tracks[1]["url"] == "/videos/ly/ep-3/720p/media-720p.m3u8"
        assert tracks[2]["url"] == "/videos/ly/ep-3/540p/media-540p.m3u8"
        assert single["coverUrl"] == "/videos/ly/ep-3/cover.jpg"
        assert single["drm"]["keyUri"] == "/drm/ly/ep-3/key"
        for t in tracks:
            assert "://" not in t["url"], f"track url must not be absolute: {t['url']}"
        assert "://" not in single["coverUrl"]
        # Old single-rung fields are gone from the contract.
        for gone in ("playUrl", "fallback", "initUrl", "firstSegUrl", "width", "height"):
            assert gone not in single, f"{gone} should have been removed"

        # Single-episode and list endpoints must agree byte-for-byte.
        r2 = client.get("/api/dramas/ly/episodes")
        assert r2.status_code == 200, r2.text
        listed = r2.json()
        assert len(listed) == 1
        assert listed[0] == single

        _validator().validate(single)
        print("OK: all URLs relative + single/list equivalence + schema validate")


if __name__ == "__main__":
    run_relative_url_case()
    print("\nall cases passed")
