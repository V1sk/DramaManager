#!/usr/bin/env python3
"""Audit current DB rows against prod object-storage keys.

线上迁移完成后，sync 阶段不再逐对象检查 prod/staging，也不再执行
staging -> prod copy。本脚本用于离线确认当前 DB 指向的 prod 资产齐全：

    STORAGE_PROVIDER=tos ./venv/bin/python scripts/audit_prod_assets.py

退出码：
  0: 未发现缺失
  1: 发现缺失对象，或对象存储 list 调用失败
  2: 对象存储未启用
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db, storage  # noqa: E402
from app.config import ALLOWED_LADDERS, settings  # noqa: E402


_MAP_URI_RE = re.compile(r'URI="([^"]+)"')


def _filename(uri: str) -> str:
    return uri.rstrip().rsplit("/", 1)[-1]


def _playlist_asset_names(path: Path) -> tuple[set[str], list[str]]:
    """Return init/segment filenames referenced by a local media playlist."""
    if not path.is_file():
        return set(), [f"missing local playlist: {path}"]
    try:
        text = path.read_text()
    except OSError as e:
        return set(), [f"failed to read local playlist {path}: {e}"]

    names: set[str] = set()
    warnings: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            m = _MAP_URI_RE.search(line)
            if m:
                names.add(_filename(m.group(1)))
            else:
                warnings.append(f"playlist map uri missing: {path}")
            continue
        if line.startswith("#"):
            continue
        names.add(_filename(line))
    if not names:
        warnings.append(f"playlist references no media assets: {path}")
    return names, warnings


def _add_expected(prefix_map: dict[str, set[str]], prefix: str, key: str) -> None:
    prefix_map[prefix].add(key)


def _subtitle_langs_for_episode(episode_id: str) -> list[str]:
    """Read subtitle DB rows directly; audit must not hide rows by local file state."""
    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT lang_code FROM subtitles WHERE episode_id=? ORDER BY lang_code ASC",
            (episode_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _collect_expected() -> tuple[dict[str, set[str]], list[str]]:
    prov = storage.provider
    if prov is None:
        raise RuntimeError("storage provider is not configured")

    prefix_map: dict[str, set[str]] = defaultdict(set)
    warnings: list[str] = []

    for drama in db.list_dramas():
        slug = drama["slug"]
        translations = db.list_drama_translations(slug)
        for _lang, fields in translations.items():
            for field, poster_dir in (
                ("poster", "poster"),
                ("poster_landscape", "poster-landscape"),
            ):
                rel_url = fields.get(field)
                if not rel_url:
                    continue
                filename = rel_url.rsplit("/", 1)[-1]
                prefix = f"{prov.prod_prefix}/{slug}/{poster_dir}/"
                _add_expected(prefix_map, prefix, f"{prefix}{filename}")

    for row in db.list_all():
        if row.get("status") != "ready":
            continue
        slug = row["drama_slug"]
        ep_number = int(row["ep_number"])
        upload_version = int(row.get("upload_version") or 1)
        ep_dir = db.episode_ep_dir(ep_number, upload_version)
        ep_prefix = f"{prov.prod_prefix}/{slug}/{ep_dir}/"

        if row.get("cover_url"):
            _add_expected(prefix_map, ep_prefix, f"{ep_prefix}cover.jpg")

        for ladder in ALLOWED_LADDERS:
            playlist = (
                settings.out_dir
                / slug
                / ep_dir
                / ladder
                / f"media-{ladder}.m3u8"
            )
            names, playlist_warnings = _playlist_asset_names(playlist)
            warnings.extend(playlist_warnings)
            for name in names:
                _add_expected(
                    prefix_map,
                    ep_prefix,
                    f"{ep_prefix}{ladder}/{name}",
                )

        episode_id = row["episode_id"]
        for lang in _subtitle_langs_for_episode(episode_id):
            _add_expected(
                prefix_map,
                ep_prefix,
                f"{ep_prefix}subtitles/{lang}.vtt",
            )

    return dict(prefix_map), warnings


def main() -> int:
    if not settings.storage_enabled:
        print("[audit] object storage disabled; nothing to audit", file=sys.stderr)
        return 2
    if storage.provider is None:
        print("[audit] storage provider is not configured", file=sys.stderr)
        return 2

    db.init_db()
    prefix_map, warnings = _collect_expected()

    missing: list[str] = []
    list_errors: list[str] = []
    for prefix, expected in sorted(prefix_map.items()):
        try:
            existing = set(storage.provider.list_with_prefix(prefix))
        except Exception as e:  # noqa: BLE001
            list_errors.append(f"{prefix}: {e}")
            continue
        missing.extend(sorted(expected - existing))

    for warning in warnings:
        print(f"[warn] {warning}")
    for err in list_errors:
        print(f"[error] list_with_prefix failed: {err}", file=sys.stderr)
    for key in missing:
        print(f"[missing] {key}")

    expected_count = sum(len(v) for v in prefix_map.values())
    print(
        "[summary] "
        f"expected={expected_count} prefixes={len(prefix_map)} "
        f"missing={len(missing)} warnings={len(warnings)} "
        f"list_errors={len(list_errors)}"
    )
    return 1 if missing or list_errors else 0


if __name__ == "__main__":
    sys.exit(main())
