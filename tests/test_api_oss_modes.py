"""TestClient verification for spec scenarios on _row_to_episode_info dual-mode + schema validation.

Covers:
  - OSS_ENABLED=true → initUrl / firstSegUrl 是绝对 OSS URL；其它字段相对路径。
  - OSS_ENABLED=true 列表与单集逐字节一致。
  - OSS 未启用 → initUrl / firstSegUrl 相对路径；行为同今日。
  - OSS 未启用 列表与单集逐字节一致。
  - 两种模式都通过 episode-info-schema.json 严格校验（uri-reference）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _setup_env(tmp: Path, oss_enabled: bool) -> None:
    os.environ["OUT_DIR"] = str(tmp / "out")
    os.environ["DB_PATH"] = str(tmp / "hls.db")
    os.environ["UPLOAD_TMP_DIR"] = str(tmp / "tmp")
    if oss_enabled:
        os.environ["OSS_ENABLED"] = "true"
    else:
        os.environ.pop("OSS_ENABLED", None)


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


OSS_PUBLIC = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging"


def run_oss_enabled_case():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_env(tmp, oss_enabled=True)
        # 重新 import 让 settings 看到 OSS_ENABLED=true
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        _seed_ready_row("ly", 3)
        client = _client()

        # 单集
        r = client.get("/api/episodes/ly/3")
        assert r.status_code == 200, r.text
        single = r.json()
        assert single["initUrl"] == f"{OSS_PUBLIC}/ly/ep-3/720p/init-720p.mp4"
        assert single["firstSegUrl"] == f"{OSS_PUBLIC}/ly/ep-3/720p/seg-720p-0.m4s"
        assert single["playUrl"] == "/videos/ly/ep-3/720p/media-720p.m3u8"
        assert single["fallback"]["low"] == "/videos/ly/ep-3/540p/media-540p.m3u8"
        assert single["fallback"]["high"] == "/videos/ly/ep-3/1080p/media-1080p.m3u8"
        assert single["coverUrl"] == "/videos/ly/ep-3/cover.jpg"
        assert single["drm"]["keyUri"] == "/drm/ly/ep-3/key"

        # 列表 vs 单集逐字段相等
        r2 = client.get("/api/dramas/ly/episodes")
        assert r2.status_code == 200, r2.text
        listed = r2.json()
        assert len(listed) == 1
        assert listed[0] == single

        # schema 严格校验
        _validator().validate(single)
        print("OK oss_enabled: single + list equivalence + schema validate")


def run_oss_disabled_case():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_env(tmp, oss_enabled=False)
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        _seed_ready_row("ly", 3)
        client = _client()

        r = client.get("/api/episodes/ly/3")
        assert r.status_code == 200, r.text
        single = r.json()
        assert single["initUrl"] == "/videos/ly/ep-3/720p/init-720p.mp4"
        assert single["firstSegUrl"] == "/videos/ly/ep-3/720p/seg-720p-0.m4s"
        # 所有 URL 都是相对路径
        for k in ("playUrl", "coverUrl", "initUrl", "firstSegUrl"):
            assert single[k].startswith("/"), single[k]
            assert "://" not in single[k]
        assert single["fallback"]["low"].startswith("/")
        assert single["fallback"]["high"].startswith("/")
        assert "://" not in single["fallback"]["low"]
        assert "://" not in single["fallback"]["high"]
        assert single["drm"]["keyUri"].startswith("/")

        r2 = client.get("/api/dramas/ly/episodes")
        listed = r2.json()
        assert listed[0] == single

        _validator().validate(single)
        print("OK oss_disabled: single + list equivalence + schema validate")


if __name__ == "__main__":
    run_oss_enabled_case()
    run_oss_disabled_case()
    print("\nall cases passed")
