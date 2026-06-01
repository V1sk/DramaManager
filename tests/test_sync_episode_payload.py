"""Verify build_episode_payload emits the per-rung `video_tracks` array
(mirroring EpisodeInfo.videoTracks) instead of the old single top-level
width/height + `playlists` map.

Spec: openspec/specs/business-server-sync/spec.md — "POST /sync/episodes" body
+ "episode sync request shape includes per-rung prod m3u8".
"""

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
    os.environ["STORAGE_PROVIDER"] = "none"
    os.environ["SESSION_SECRET_KEY"] = "test-secret-key"
    os.environ["ADMIN_INITIAL_PASSWORD"] = "test-admin-pw"


def _reset_app_modules():
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]


def _seed_ready_row(slug: str, ep: int, *, width=None, height=None) -> None:
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
        duration_ms=150000,
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


# Fake prod-flavored m3u8 text per ladder — build_episode_payload only threads
# these through verbatim, so a sentinel string per rung is enough.
def _fake_playlists() -> dict[str, str]:
    return {
        "540p": "#EXTM3U\n#PLAYLIST-540\n",
        "720p": "#EXTM3U\n#PLAYLIST-720\n",
        "1080p": "#EXTM3U\n#PLAYLIST-1080\n",
    }


def case_per_rung_tracks():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        _seed_ready_row("ly", 3, width=720, height=1280)
        from app import sync

        payload = sync.build_episode_payload("ly", 3, _fake_playlists())

        # Old shape is gone.
        assert "playlists" not in payload, payload.keys()
        assert "width" not in payload and "height" not in payload, payload.keys()

        # New shape: per-rung array ordered high → mid → low.
        tracks = payload["video_tracks"]
        assert [t["id"] for t in tracks] == ["high", "mid", "low"], tracks
        assert [t["ladder"] for t in tracks] == ["1080p", "720p", "540p"], tracks

        by_id = {t["id"]: t for t in tracks}
        # Source 720×1280 → same per-rung dims the SDK derives.
        assert (by_id["high"]["width"], by_id["high"]["height"]) == (608, 1080)
        assert (by_id["mid"]["width"], by_id["mid"]["height"]) == (406, 720)
        assert (by_id["low"]["width"], by_id["low"]["height"]) == (304, 540)

        # Each rung carries its own prod m3u8 text.
        assert by_id["mid"]["playlist"] == "#EXTM3U\n#PLAYLIST-720\n"
        assert by_id["high"]["playlist"] == "#EXTM3U\n#PLAYLIST-1080\n"
        assert by_id["low"]["playlist"] == "#EXTM3U\n#PLAYLIST-540\n"

        # DRM + envelope still intact.
        assert payload["episode_id"] == "ly-ep-3"
        assert payload["duration_ms"] == 150000
        assert payload["drm"]["key_uri"] == "/drm/ly/ep-3/key"
        assert payload["cover_key"] == "/videos/ly/ep-3/cover.jpg"
        print("OK per-rung video_tracks: high/mid/low, dims 608/406/304, m3u8 threaded")


def case_legacy_row_null_dims():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        _seed_ready_row("oldslug", 1)  # no source dims → null per rung
        from app import sync

        payload = sync.build_episode_payload("oldslug", 1, _fake_playlists())
        tracks = payload["video_tracks"]
        assert [t["id"] for t in tracks] == ["high", "mid", "low"]
        for t in tracks:
            assert t["width"] is None and t["height"] is None, t
            assert t["playlist"]  # still threaded
        print("OK legacy row: per-rung width/height null, playlists still present")


if __name__ == "__main__":
    case_per_rung_tracks()
    case_legacy_row_null_dims()
    print("\nall cases passed")
