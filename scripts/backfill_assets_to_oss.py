#!/usr/bin/env python3
"""一次性回填脚本：把现存的本地海报 / 集封面 / 字幕全部同步到 OSS staging 前缀。

assets-to-oss change 上线之前的 deploy 会有这些资源只在本地磁盘。开启
`OSS_ENABLED=true` 后跑一次：

    OSS_ENABLED=true ./venv/bin/python scripts/backfill_assets_to_oss.py

幂等：OSS PUT 是覆盖；重跑只是再传一次同样的字节。

只会上传那些 **DB 行存在** 的资源，避免把孤立文件（之前删了 row 但没删盘）误传上去。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db, publish  # noqa: E402
from app.config import settings  # noqa: E402


def _backfill_posters() -> tuple[int, int, int]:
    """For every drama, walk OUT_DIR/{slug}/poster/* and upload files whose
    `(slug, lang, 'poster')` translation row exists.

    Returns (uploaded, skipped_no_row, failed).
    """
    uploaded = 0
    skipped = 0
    failed = 0
    for drama in db.list_dramas():
        slug = drama["slug"]
        poster_dir = settings.out_dir / slug / "poster"
        if not poster_dir.is_dir():
            continue
        # Translations with field='poster' for this drama → set of valid langs
        valid_langs = {
            lang_code
            for lang_code, fields in db.list_drama_translations(slug).items()
            if fields.get("poster")
        }
        for f in poster_dir.iterdir():
            if not f.is_file():
                continue
            # filename like "{lang}.{ext}"
            stem = f.stem
            if stem not in valid_langs:
                print(f"[skip-no-row] poster {slug}/{f.name}")
                skipped += 1
                continue
            try:
                publish.upload_poster_to_staging(slug, stem, f)
                print(f"[ok] poster {slug}/{f.name}")
                uploaded += 1
            except Exception as e:  # noqa: BLE001
                print(f"[fail] poster {slug}/{f.name}: {e}", file=sys.stderr)
                failed += 1
    return uploaded, skipped, failed


def _backfill_covers() -> tuple[int, int, int]:
    """For every episode row, upload its OUT_DIR/{slug}/{ep_dir}/cover.jpg if present."""
    uploaded = 0
    skipped = 0
    failed = 0
    for row in db.list_all():
        slug = row["drama_slug"]
        ep_dir = f"ep-{row['ep_number']}"
        cover_path = settings.out_dir / slug / ep_dir / "cover.jpg"
        if not cover_path.is_file():
            print(f"[skip-no-file] cover {slug}/{ep_dir}/cover.jpg")
            skipped += 1
            continue
        try:
            publish.upload_cover_to_staging(slug, ep_dir, cover_path)
            print(f"[ok] cover {slug}/{ep_dir}/cover.jpg")
            uploaded += 1
        except Exception as e:  # noqa: BLE001
            print(f"[fail] cover {slug}/{ep_dir}: {e}", file=sys.stderr)
            failed += 1
    return uploaded, skipped, failed


def _backfill_subtitles() -> tuple[int, int, int]:
    """For every subtitles row, upload its OUT_DIR/{slug}/{ep_dir}/subtitles/{lang}.vtt."""
    uploaded = 0
    skipped = 0
    failed = 0
    for row in db.list_all():
        slug = row["drama_slug"]
        ep_number = row["ep_number"]
        ep_dir = f"ep-{ep_number}"
        sub_rows = db.list_subtitles_for_slug_ep(slug, ep_number)
        for s in sub_rows:
            lang = s["lang_code"]
            sub_path = settings.out_dir / slug / ep_dir / "subtitles" / f"{lang}.vtt"
            if not sub_path.is_file():
                print(f"[skip-no-file] subtitle {slug}/{ep_dir}/{lang}.vtt")
                skipped += 1
                continue
            try:
                publish.upload_subtitle_to_staging(slug, ep_dir, lang, sub_path)
                print(f"[ok] subtitle {slug}/{ep_dir}/{lang}.vtt")
                uploaded += 1
            except Exception as e:  # noqa: BLE001
                print(f"[fail] subtitle {slug}/{ep_dir}/{lang}: {e}", file=sys.stderr)
                failed += 1
    return uploaded, skipped, failed


def main() -> int:
    if not settings.oss_enabled:
        print(
            "[backfill] OSS_ENABLED 未启用，无需回填；脚本退出。",
            file=sys.stderr,
        )
        return 2

    db.init_db()
    print("=== posters ===")
    p_up, p_skip, p_fail = _backfill_posters()
    print(f"=== posters: uploaded={p_up} skipped={p_skip} failed={p_fail}")
    print("\n=== covers ===")
    c_up, c_skip, c_fail = _backfill_covers()
    print(f"=== covers: uploaded={c_up} skipped={c_skip} failed={c_fail}")
    print("\n=== subtitles ===")
    s_up, s_skip, s_fail = _backfill_subtitles()
    print(f"=== subtitles: uploaded={s_up} skipped={s_skip} failed={s_fail}")

    total_failed = p_fail + c_fail + s_fail
    if total_failed:
        print(f"\n[backfill] {total_failed} failures — exit 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
