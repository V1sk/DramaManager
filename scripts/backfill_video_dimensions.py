#!/usr/bin/env python3
"""一次性回填脚本：把 DB 里 width / height 为 NULL 的 ready 行从 init.mp4 探测出来后写回。

为什么读 init.mp4 而不是源 mp4：源 mp4 在 pipeline 跑完后已被 worker 清理（_cleanup_tmp）；
init.mp4 仍在本地 OUT_DIR 下，且包含 SPS（含编码后的 codec width/height），ffprobe 能读到。

注意：读到的是**编码后**（720p ladder 经 `scale=-2:720` 等约束）的尺寸，不是源视频原始
分辨率。aspect ratio 守恒（pad 不守恒，例如 720×720 被 pad 过的源会读到 pad 后的 1:1），
对客户端的"预定渲染容器 + aspect ratio"用例足够。新上传走 `probe_video_dimensions(源)`
拿源 dimension —— 两路绝对值口径轻微不一致，aspect ratio 一致；接受这个 trade-off。

幂等：仅 UPDATE 仍为 NULL 的行（`set_dimensions` 内部条件守门），重复跑不会覆盖已填值。

用法（在项目根目录跑）：
    ./venv/bin/python scripts/backfill_video_dimensions.py
"""

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.ffmpeg_utils import FfmpegError, probe_video_dimensions  # noqa: E402


# 优先尝试 720p（playUrl 默认指向的档），不存在时按 540p / 1080p 兜底。
_LADDERS_PRIORITY = ("720p", "540p", "1080p")


def _find_init_mp4(drama_slug: str, ep_number: int) -> Path | None:
    """返回第一个能读到的 init-{rung}.mp4 路径。两种历史目录布局都尝试一下。"""
    candidates_dirs = [
        f"ep-{ep_number}",                       # 新布局（sdk-drama-listing D8 之后）
        f"{drama_slug}-ep-{ep_number}",          # 老布局（hls-management-server 初版）
    ]
    for ep_dir in candidates_dirs:
        for ladder in _LADDERS_PRIORITY:
            candidate = settings.out_dir / drama_slug / ep_dir / ladder / f"init-{ladder}.mp4"
            if candidate.is_file():
                return candidate
    return None


def main() -> int:
    db.init_db()  # 确保 schema 已经 ALTER 过 width/height

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT drama_slug, ep_number, episode_id, status "
        "FROM episodes "
        "WHERE width IS NULL OR height IS NULL "
        "ORDER BY drama_slug, ep_number"
    ).fetchall()
    conn.close()

    if not rows:
        print("[backfill] 没有需要回填的行")
        return 0

    print(f"[backfill] 共 {len(rows)} 行待回填")

    filled = 0
    skipped_missing = 0
    skipped_failed = 0
    for r in rows:
        slug = r["drama_slug"]
        ep_n = r["ep_number"]
        ep_id = r["episode_id"]
        status = r["status"]

        init_path = _find_init_mp4(slug, ep_n)
        if init_path is None:
            skipped_missing += 1
            print(f"[backfill]   SKIP {ep_id} ({status}): 找不到任何 init-*.mp4")
            continue

        try:
            width, height = probe_video_dimensions(init_path)
        except FfmpegError as e:
            skipped_failed += 1
            print(f"[backfill]   FAIL {ep_id}: ffprobe {init_path.name} 失败: {e}")
            continue

        wrote = db.set_dimensions(ep_id, width, height)
        if wrote:
            filled += 1
            print(f"[backfill]   OK   {ep_id}: {width}x{height} (源 {init_path.relative_to(settings.out_dir)})")
        else:
            # 并发情况下可能已被另一个写入填掉
            print(f"[backfill]   NOOP {ep_id}: 已被其它进程填过")

    print(
        f"\n[backfill] 完成：填 {filled} 行 / "
        f"跳过 {skipped_missing} 行（缺 init.mp4）/ "
        f"失败 {skipped_failed} 行（ffprobe 错）"
    )
    return 0 if skipped_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
