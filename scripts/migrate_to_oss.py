#!/usr/bin/env python3
"""一次性迁移脚本：把已有的 status=ready 剧集的本地切片重新发布到 OSS staging 前缀。

随着 oss-staging-prod-separation 启用，publish_ladder 改写到 `Drama/staging/...`。
本脚本对所有 ready 行重跑 publish_ladder：
  - 上传到 staging 前缀（已存在则覆盖）
  - 改写本地 m3u8 引用为 staging 绝对 URL（幂等）
  - 列出但**不删**遗留的 `Drama/{slug}/{ep_dir}/...` 旧前缀对象，仅作 cleanup 候选

前置：`OSS_ENABLED=true`，`app/oss_upload.py` 的凭证 / endpoint / bucket 配置正确。

用法：
    OSS_ENABLED=true ./venv/bin/python scripts/migrate_to_oss.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db, oss_upload  # noqa: E402
from app.config import settings  # noqa: E402
from app.publish import PublishError, publish_ladder  # noqa: E402


def _list_legacy_keys(slug: str, ep_dir: str) -> list[str]:
    """列出 `Drama/{slug}/{ep_dir}/...` 旧扁平前缀下的对象（候选清理项）。"""
    legacy_prefix = f"{oss_upload.ossBaseDir}/{slug}/{ep_dir}/"
    return oss_upload.list_with_prefix(legacy_prefix)


def main() -> int:
    if not settings.oss_enabled:
        print("[migrate] OSS_ENABLED 未启用，无需迁移；脚本退出。", file=sys.stderr)
        return 2

    db.init_db()
    success = 0
    failed = 0
    failed_items: list[tuple[str, int, str, str]] = []
    legacy_candidates: list[str] = []

    dramas = db.list_ready_dramas()
    print(f"[migrate] 共 {len(dramas)} 部剧待处理")
    for drama in dramas:
        slug = drama["drama_slug"]
        rows = db.list_ready_by_slug(slug)
        print(f"[migrate]   剧 {slug}: {len(rows)} 集 ready")
        for row in rows:
            ep_dir = f"ep-{row['ep_number']}"
            for ladder in ("540p", "720p", "1080p"):
                try:
                    publish_ladder(slug, ep_dir, ladder)
                    success += 1
                except PublishError as e:
                    failed += 1
                    failed_items.append((slug, row["ep_number"], ladder, str(e)))
                    print(f"[migrate]     FAIL {slug}/{ep_dir}/{ladder}: {e}", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    failed_items.append((slug, row["ep_number"], ladder, f"unexpected: {e}"))
                    print(f"[migrate]     FAIL {slug}/{ep_dir}/{ladder}: unexpected: {e}", file=sys.stderr)
                else:
                    print(f"[migrate]     OK   {slug}/{ep_dir}/{ladder}")
            try:
                stale = _list_legacy_keys(slug, ep_dir)
                if stale:
                    legacy_candidates.extend(stale)
                    print(
                        f"[migrate]     LEGACY {slug}/{ep_dir}: {len(stale)} 个旧前缀对象 (候选清理)"
                    )
            except Exception as e:  # noqa: BLE001 — 列举失败不阻塞迁移本身
                print(
                    f"[migrate]     LEGACY {slug}/{ep_dir}: 列举失败: {e}",
                    file=sys.stderr,
                )

    print(f"\n[migrate] 完成：成功 {success} 档 / 失败 {failed} 档")
    if failed_items:
        print("[migrate] 失败明细：")
        for slug, ep, ladder, err in failed_items:
            print(f"  - {slug}/ep-{ep}/{ladder}: {err}")
    if legacy_candidates:
        print(f"\n[migrate] 检测到 {len(legacy_candidates)} 个旧前缀 OSS 对象（候选手动清理）：")
        for k in legacy_candidates[:20]:
            print(f"  - {k}")
        if len(legacy_candidates) > 20:
            print(f"  ... ({len(legacy_candidates) - 20} more)")
        print("[migrate] 这些对象未被脚本删除；如确认无引用，可在 OSS 控制台批量删除。")
    return 1 if failed_items else 0


if __name__ == "__main__":
    sys.exit(main())
