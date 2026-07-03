"""Regression tests for two fixes:

1. Retry resume: `_encode_artifacts_complete` detects a fully-encoded ep_dir so
   the worker can skip re-encoding, and `publish_ladder(skip_existing=True)`
   only re-uploads the segments that never made it to the bucket. Sync-time
   `publish_*_to_prod` helpers only return prod keys and do not re-check/copy
   staging objects.
2. Episode numbering: `_next_ep_number` excludes `pending_delete` rows so a
   re-upload after deleting the only (synced) episode reuses ep 1 instead of
   jumping to ep 2.
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


_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:7\n"
    '#EXT-X-MAP:URI="init-{ladder}.mp4"\n'
    '#EXT-X-KEY:METHOD=AES-128,URI="/drm/x/ep-1/key",IV=0x{iv}\n'
    "#EXTINF:2.000000,\n"
    "seg-{ladder}-0.m4s\n"
    "#EXTINF:2.000000,\n"
    "seg-{ladder}-1.m4s\n"
    "#EXT-X-ENDLIST\n"
)


def _build_complete_ep_dir(out_dir: Path, slug: str, ep_dir: str) -> None:
    keys = out_dir / slug / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / f"{ep_dir}.key.b64").write_text("AAECAwQFBgcICQoLDA0ODw==")
    (keys / f"{ep_dir}.iv").write_text("abcdef0123456789abcdef0123456789")
    for ladder in ("540p", "720p", "1080p"):
        d = out_dir / slug / ep_dir / ladder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"init-{ladder}.mp4").write_bytes(b"init")
        (d / f"seg-{ladder}-0.m4s").write_bytes(b"seg0")
        (d / f"seg-{ladder}-1.m4s").write_bytes(b"seg1")
        (d / f"media-{ladder}.m3u8").write_text(
            _M3U8.format(ladder=ladder, iv="0" * 32)
        )


def case_encode_artifacts_complete():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app.config import settings
        from app.work_queue import _encode_artifacts_complete

        out = settings.out_dir
        _build_complete_ep_dir(out, "ly", "ep-1")
        assert _encode_artifacts_complete(out / "ly", "ep-1") is True

        # Missing a segment that the playlist references → incomplete.
        (out / "ly" / "ep-1" / "1080p" / "seg-1080p-1.m4s").unlink()
        assert _encode_artifacts_complete(out / "ly", "ep-1") is False

        # Restore that, but drop a key file → incomplete.
        (out / "ly" / "ep-1" / "1080p" / "seg-1080p-1.m4s").write_bytes(b"seg1")
        assert _encode_artifacts_complete(out / "ly", "ep-1") is True
        (out / "ly" / "keys" / "ep-1.iv").unlink()
        assert _encode_artifacts_complete(out / "ly", "ep-1") is False

        # An un-encrypted playlist (no #EXT-X-KEY) is not "complete".
        (out / "ly" / "keys" / "ep-1.iv").write_text("abcdef0123456789abcdef0123456789")
        plain = out / "ly" / "ep-1" / "720p" / "media-720p.m3u8"
        plain.write_text(plain.read_text().replace(
            '#EXT-X-KEY:METHOD=AES-128,URI="/drm/x/ep-1/key",IV=0x' + "0" * 32 + "\n",
            "",
        ))
        assert _encode_artifacts_complete(out / "ly", "ep-1") is False
        print("OK _encode_artifacts_complete: complete/seg-missing/key-missing/unencrypted")


class _FakeProvider:
    staging_prefix = "Drama/staging"
    prod_prefix = "Drama/prod"
    staging_base_url = "https://fake/Drama/staging"
    prod_base_url = "https://fake/Drama/prod"

    def __init__(self, existing=None):
        self.store = set(existing or [])
        self.uploaded = []
        self.copied = []

    def upload_file(self, remote_key, local_file_path):
        self.uploaded.append(remote_key)
        self.store.add(remote_key)
        return {"result": True, "code": 200, "msg": "ok"}

    def list_with_prefix(self, prefix):
        return [k for k in self.store if k.startswith(prefix)]

    def copy_object(self, s, d):
        self.copied.append((s, d))
        self.store.add(d)

    def batch_delete(self, keys):  # pragma: no cover
        pass


def case_publish_ladder_skip_existing():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app.config import settings
        import app.storage as storage_mod
        from app import publish

        _build_complete_ep_dir(settings.out_dir, "ly", "ep-1")
        prefix = "Drama/prod/ly/ep-1/720p"
        # Simulate a prior partial publish: init + seg-0 already in the bucket,
        # seg-1 never made it.
        existing = [f"{prefix}/init-720p.mp4", f"{prefix}/seg-720p-0.m4s"]

        # skip_existing=True → only the missing seg-1 is uploaded.
        prov = _FakeProvider(existing=existing)
        storage_mod.provider = prov
        uploaded, skipped = publish.publish_ladder(
            "ly", "ep-1", "720p", skip_existing=True,
        )
        assert uploaded == 1, (uploaded, prov.uploaded)
        assert skipped == 2, skipped
        assert prov.uploaded == [f"{prefix}/seg-720p-1.m4s"], prov.uploaded
        playlist = publish.publish_ladder_to_prod("ly", "ep-1", "720p")
        assert "Drama/prod/ly/ep-1/720p/init-720p.mp4" in playlist
        assert prov.copied == []

        # skip_existing=False → everything (re-)uploaded, overwriting.
        prov2 = _FakeProvider(existing=existing)
        storage_mod.provider = prov2
        uploaded2, skipped2 = publish.publish_ladder(
            "ly", "ep-1", "720p", skip_existing=False,
        )
        assert uploaded2 == 3, uploaded2   # init + seg-0 + seg-1
        assert skipped2 == 0, skipped2

        staging_prefix = "Drama/staging/ly/ep-1/720p"
        prov3 = _FakeProvider(existing=[
            f"{staging_prefix}/init-720p.mp4",
            f"{staging_prefix}/seg-720p-0.m4s",
            f"{staging_prefix}/seg-720p-1.m4s",
        ])
        storage_mod.provider = prov3
        playlist3 = publish.publish_ladder_to_prod("ly", "ep-1", "720p")
        assert "Drama/prod/ly/ep-1/720p/seg-720p-1.m4s" in playlist3
        assert prov3.copied == []
        print("OK publish_ladder direct-prod resume + sync-time no-copy")


def case_versioned_asset_publish_keys():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        import app.storage as storage_mod
        from app import publish

        direct = _FakeProvider(existing=[])
        storage_mod.provider = direct
        assert publish.publish_cover_to_prod("ly", "ep-1-v2") == "Drama/prod/ly/ep-1-v2/cover.jpg"
        assert publish.publish_subtitle_to_prod("ly", "ep-1-v2", "zh-rCN") == (
            "Drama/prod/ly/ep-1-v2/subtitles/zh-rCN.vtt"
        )
        assert publish.publish_poster_to_prod("ly", "zh-rCN", "zh-rCN-v2.jpg") == (
            "Drama/prod/ly/poster/zh-rCN-v2.jpg"
        )
        assert publish.publish_poster_to_prod(
            "ly", "zh-rCN", "zh-rCN-v2.jpg", "poster-landscape",
        ) == "Drama/prod/ly/poster-landscape/zh-rCN-v2.jpg"
        assert direct.copied == []

        prov = _FakeProvider(existing=[
            "Drama/staging/ly/ep-1/cover.jpg",
            "Drama/staging/ly/ep-1/subtitles/zh-rCN.vtt",
            "Drama/staging/ly/poster/zh-rCN-v2.jpg",
            "Drama/staging/ly/poster-landscape/zh-rCN-v2.jpg",
        ])
        storage_mod.provider = prov

        cover_key = publish.publish_cover_to_prod("ly", "ep-1-v2")
        assert cover_key == "Drama/prod/ly/ep-1-v2/cover.jpg"

        sub_key = publish.publish_subtitle_to_prod("ly", "ep-1-v2", "zh-rCN")
        assert sub_key == "Drama/prod/ly/ep-1-v2/subtitles/zh-rCN.vtt"

        poster_key = publish.publish_poster_to_prod("ly", "zh-rCN", "zh-rCN-v2.jpg")
        assert poster_key == "Drama/prod/ly/poster/zh-rCN-v2.jpg"
        landscape_key = publish.publish_poster_to_prod(
            "ly", "zh-rCN", "zh-rCN-v2.jpg", "poster-landscape",
        )
        assert landscape_key == "Drama/prod/ly/poster-landscape/zh-rCN-v2.jpg"
        assert prov.copied == []
        print("OK versioned cover/subtitle/poster prod-key publish without sync copy")


def case_next_ep_excludes_pending_delete():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app import db
        from app.routers.admin import _next_ep_number

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")
        db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=1000, cover_url="/videos/ly/ep-1/cover.jpg",
            source_filename="x.mp4",
        )
        # A live (non-pending) ep1 → next is 2.
        assert _next_ep_number("ly") == 2

        # Mark it pending_delete (simulating delete of a previously-synced ep).
        db.set_episode_sync_status("ly", 1, "pending_delete")
        # Now the only row is hidden/pending_delete → next reuses ep 1.
        assert _next_ep_number("ly") == 1

        # upsert_pending onto ep 1 must UPDATE the hidden row, not collide.
        old_source, version = db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=2000, cover_url="/videos/ly/ep-1/cover.jpg",
            source_filename="y.mp4",
        )
        assert version == 2, version  # resurrected row bumps upload_version
        row = db.get_by_slug_ep("ly", 1)
        assert row["sync_status"] == "dirty", row["sync_status"]
        print("OK _next_ep_number: live→2, pending_delete→1, upsert resurrects (v2, dirty)")


def case_upsert_pending_computes_versioned_cover_url():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app import db

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")
        _, v1 = db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=1000, cover_url=None, source_filename="v1.mp4",
        )
        row1 = db.get_by_slug_ep("ly", 1)
        assert v1 == 1
        assert row1["cover_url"] == "/videos/ly/ep-1/cover.jpg"

        _, v2 = db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=2000, cover_url=None, source_filename="v2.mp4",
        )
        row2 = db.get_by_slug_ep("ly", 1)
        assert v2 == 2
        assert row2["cover_url"] == "/videos/ly/ep-1-v2/cover.jpg"
        print("OK upsert_pending computes versioned cover_url")


def case_missing_subtitle_file_is_hidden():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app import db
        from app.config import settings

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_language(code="en-US", display_label="English")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")
        db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=1000, cover_url=None, source_filename="v1.mp4",
        )
        db.upsert_subtitle("ly-ep-1", "en-US", "/videos/ly/ep-1/subtitles/en-US.vtt")
        assert db.list_subtitles_for_slug_ep("ly", 1) == []

        p = settings.out_dir / "ly" / "ep-1" / "subtitles" / "en-US.vtt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n")
        rows = db.list_subtitles_for_slug_ep("ly", 1)
        assert [r["lang_code"] for r in rows] == ["en-US"]
        print("OK missing subtitle files are hidden until local file exists")


def case_landscape_poster_sync_payload():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app import db
        from app.sync import build_drama_payload

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")
        db.upsert_drama_poster("ly", "zh-rCN", "/videos/ly/poster/zh-rCN.jpg")
        db.upsert_drama_landscape_poster(
            "ly", "zh-rCN", "/videos/ly/poster-landscape/zh-rCN-v2.jpg",
        )
        translations = db.list_drama_translations("ly")
        assert translations["zh-rCN"]["poster"] == "/videos/ly/poster/zh-rCN.jpg"
        assert translations["zh-rCN"]["poster_landscape"] == (
            "/videos/ly/poster-landscape/zh-rCN-v2.jpg"
        )

        payload = build_drama_payload(
            "ly",
            poster_prod_keys={"zh-rCN": "Drama/prod/ly/poster/zh-rCN.jpg"},
            poster_landscape_prod_keys={
                "zh-rCN": "Drama/prod/ly/poster-landscape/zh-rCN-v2.jpg",
            },
        )
        zh = payload["translations"]["zh-rCN"]
        assert zh["poster_key"] == "Drama/prod/ly/poster/zh-rCN.jpg"
        assert zh["poster_landscape_key"] == (
            "Drama/prod/ly/poster-landscape/zh-rCN-v2.jpg"
        )
        print("OK landscape poster appears in translations and sync payload")


def case_featured_categories_sync_payload_and_overview():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        _reset_app_modules()
        from app import db
        from app.sync import build_drama_payload

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")
        stored = db.replace_drama_featured_categories(
            "ly", ["exclusive", "hot", "hot"],
        )
        assert stored == ["hot", "exclusive"]
        full = db.get_drama_full("ly")
        assert full["featured_categories"] == ["hot", "exclusive"]

        payload = build_drama_payload("ly")
        assert payload["featured_categories"] == ["hot", "exclusive"]

        overview = db.list_featured_category_overview()
        assert [r["slug"] for r in overview["hot"]] == ["ly"]
        assert overview["new"] == []
        assert [r["slug"] for r in overview["exclusive"]] == ["ly"]
        print("OK fixed featured categories persist, aggregate, and sync")


def case_default_ladder_keeps_reupload_version():
    with tempfile.TemporaryDirectory() as td:
        _setup_env(Path(td))
        os.environ["DEFAULT_LADDER"] = "720p"
        _reset_app_modules()
        from app import db

        db.init_db()
        db.create_language(code="zh-rCN", display_label="简体中文")
        db.create_drama(slug="ly", name="测试剧", default_lang="zh-rCN")

        db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=1000, cover_url="/videos/ly/ep-1/cover.jpg",
            source_filename="v1.mp4",
        )
        db.set_status(
            "ly-ep-1", "ready",
            play_url="/videos/ly/ep-1/720p/media-720p.m3u8",
            key_uri="/drm/ly/ep-1/key",
            key_b64="AAECAwQFBgcICQoLDA0ODw==",
            iv_hex="abcdef0123456789abcdef0123456789",
        )
        db.set_episode_sync_status("ly", 1, "clean")

        _, version = db.upsert_pending(
            drama_slug="ly", ep_number=1, episode_id="ly-ep-1",
            duration_ms=2000, cover_url="/videos/ly/ep-1/cover.jpg",
            source_filename="v2.mp4",
        )
        assert version == 2
        db.set_status(
            "ly-ep-1", "ready",
            play_url="/videos/ly/ep-1-v2/720p/media-720p.m3u8",
            key_uri="/drm/ly/ep-1-v2/key",
            key_b64="AAECAwQFBgcICQoLDA0ODw==",
            iv_hex="abcdef0123456789abcdef0123456789",
        )

        row = db.get_by_slug_ep("ly", 1)
        assert row["upload_version"] == 2
        assert row["play_url"] == "/videos/ly/ep-1-v2/720p/media-720p.m3u8"
        print("OK default ladder rewrite keeps reupload version in play_url")


def test_default_ladder_keeps_reupload_version():
    case_default_ladder_keeps_reupload_version()


def test_versioned_asset_publish_keys():
    case_versioned_asset_publish_keys()


def test_upsert_pending_computes_versioned_cover_url():
    case_upsert_pending_computes_versioned_cover_url()


def test_missing_subtitle_file_is_hidden():
    case_missing_subtitle_file_is_hidden()


def test_landscape_poster_sync_payload():
    case_landscape_poster_sync_payload()


def test_featured_categories_sync_payload_and_overview():
    case_featured_categories_sync_payload_and_overview()


if __name__ == "__main__":
    case_encode_artifacts_complete()
    case_publish_ladder_skip_existing()
    case_versioned_asset_publish_keys()
    case_next_ep_excludes_pending_delete()
    case_upsert_pending_computes_versioned_cover_url()
    case_missing_subtitle_file_is_hidden()
    case_landscape_poster_sync_payload()
    case_featured_categories_sync_payload_and_overview()
    case_default_ladder_keeps_reupload_version()
    print("\nall cases passed")
