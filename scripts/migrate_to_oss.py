#!/usr/bin/env python3
"""一次性迁移脚本：把已有的 status=ready 剧集的本地切片传到 OSS + 改写 m3u8。

前置：在环境里先设 `OSS_ENABLED=true` 且确保 app/oss_upload.py 凭证 / endpoint /
bucket 配置正确。

用法（在项目根目录跑）：
    OSS_ENABLED=true ./venv/bin/python scripts/migrate_to_oss.py

幂等：重复跑只是覆盖 OSS 对象 + m3u8 再次 rewrite 仍是 noop。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.publish import PublishError, publish_ladder  # noqa: E402


def main() -> int:
    if not settings.oss_enabled:
        print("[migrate] OSS_ENABLED 未启用，无需迁移；脚本退出。", file=sys.stderr)
        return 2

    db.init_db()
    success = 0
    failed = 0
    failed_items: list[tuple[str, int, str, str]] = []

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

    print(f"\n[migrate] 完成：成功 {success} 档 / 失败 {failed} 档")
    if failed_items:
        print("[migrate] 失败明细：")
        for slug, ep, ladder, err in failed_items:
            print(f"  - {slug}/ep-{ep}/{ladder}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
