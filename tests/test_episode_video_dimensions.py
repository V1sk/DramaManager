"""TestClient verification for EpisodeInfo width / height pass-through.

Spec scenarios:
  - 新上传 ready 行响应含正整数 width / height
  - 老 ready 行响应 width / height 为 null
  - 单集与列表端点同一行 width / height 一致
  - schema 严格校验在两种形态下都通过
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
    os.environ.pop("OSS_ENABLED", None)


def _reset_app_modules():
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]


def _seed_ready_row(slug: str, ep: int, *, width=None, height=None) -> None:
    from app import db
    db.init_db()
    db.upsert_pending(
        drama_slug=slug,
        drama_name="测试剧",
        ep_number=ep,
        episode_id=f"{slug}-ep-{ep}",
        duration_ms=120000,
        cover_url=f"/videos/{slug}/ep-{ep}/cover.jpg",
        source_filename="x.mp4",
        width=width,
        height=height,
    )
    db.set_status(
        episode_id=f"{slug}-ep-{ep}",
        status="ready",
        play_url=f"/videos/{slug}/ep-{ep}/720p/media-720p.m3u8",
        key_uri=f"/drm/{slug}/ep-{ep}/key",
        key_b64="AAECAwQFBgcICQoLDA0ODw==",
        iv_hex="abcdef0123456789abcdef0123456789",
    )


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _validator():
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((REPO_ROOT / "episode-info-schema.json").read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def case_new_row_with_dimensions():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        _seed_ready_row("ly", 3, width=720, height=1280)
        client = _client()

        r = client.get("/api/episodes/ly/3")
        assert r.status_code == 200, r.text
        single = r.json()
        assert single["width"] == 720
        assert single["height"] == 1280

        r2 = client.get("/api/dramas/ly/episodes")
        listed = r2.json()
        assert len(listed) == 1
        assert listed[0] == single  # 单集 / 列表逐字节相等

        _validator().validate(single)
        print("OK new row: width=720 height=1280; single == list; schema validate")


def case_legacy_row_null_dimensions():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        _seed_ready_row("oldslug", 1)  # 不传 width/height → NULL
        client = _client()

        r = client.get("/api/episodes/oldslug/1")
        assert r.status_code == 200, r.text
        single = r.json()
        assert single["width"] is None
        assert single["height"] is None
        # 其它字段仍然完整
        assert single["episodeId"] == "oldslug-ep-1"
        assert single["durationMs"] == 120000
        assert single["playUrl"] == "/videos/oldslug/ep-1/720p/media-720p.m3u8"

        r2 = client.get("/api/dramas/oldslug/episodes")
        listed = r2.json()
        assert listed[0] == single

        _validator().validate(single)
        print("OK legacy row: width/height null; other fields intact; schema validate")


if __name__ == "__main__":
    case_new_row_with_dimensions()
    case_legacy_row_null_dimensions()
    print("\nall cases passed")
