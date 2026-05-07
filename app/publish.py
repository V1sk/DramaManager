"""OSS 发布层：把本地切片传到 OSS，并把 m3u8 里的 init / segment 引用改写成绝对 URL。

- `rewrite_playlist` 是纯函数，可独立单测。
- `publish_ladder` 是 worker 在 pipeline 三个 stage 跑完后的后处理钩子。

只在 `settings.oss_enabled` 为真时被调用；调用方决定何时触发。
"""

import re
from pathlib import Path

from . import oss_upload
from .config import settings
from .oss_upload import (
    OSS_PROD_PREFIX,
    OSS_STAGING_PREFIX,
    oss_prod_public_base_url,
    oss_staging_public_base_url,
    upload_file,
)


class PublishError(Exception):
    """worker 调用方据此把 episode 状态置为 failed。"""


_MAP_URI_RE = re.compile(r'(URI=")([^"]+)(")')


def rewrite_playlist(text: str, oss_base: str) -> str:
    """把 m3u8 里的 init / segment 引用替换成绝对 OSS URL，#EXT-X-KEY 行不动。

    `oss_base` 形如 `https://.../Drama/{slug}/{ep_dir}/{ladder}`，已含档位段；
    本函数只在尾部拼"文件名"。允许传入末尾带 / 的 base，内部会去掉。

    幂等：再次对已改写过的 m3u8 调用，输出与输入逐字节相等（迁移脚本依赖）。
    """
    base = oss_base.rstrip("/")
    out_lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        # 拆出"内容部分"做模式匹配，行尾换行单独保留
        if raw_line.endswith("\r\n"):
            content, eol = raw_line[:-2], "\r\n"
        elif raw_line.endswith("\n"):
            content, eol = raw_line[:-1], "\n"
        else:
            content, eol = raw_line, ""

        if content.startswith("#EXT-X-MAP:"):
            # 把内层 URI="..." 的值替换为 {base}/{原文件名}
            def _rewrite(m: re.Match) -> str:
                inner = m.group(2)
                if inner.startswith("http://") or inner.startswith("https://"):
                    # 已是绝对 URL（迁移幂等场景）—— 不再叠前缀
                    return m.group(0)
                # 取最后一段当文件名，避免 inner 已是相对路径中段的情况
                filename = inner.rsplit("/", 1)[-1]
                return f'{m.group(1)}{base}/{filename}{m.group(3)}'
            out_lines.append(_MAP_URI_RE.sub(_rewrite, content) + eol)
            continue

        if content.startswith("#EXT-X-KEY:") or content.startswith("#") or content == "":
            # 注释 / 空行透传
            out_lines.append(raw_line)
            continue

        # 非注释 / 非空 → 视为 segment 行
        if content.startswith("http://") or content.startswith("https://"):
            # 已是绝对 URL（幂等）—— 不再叠
            out_lines.append(raw_line)
            continue
        filename = content.rsplit("/", 1)[-1]
        out_lines.append(f"{base}/{filename}{eol}")

    return "".join(out_lines)


def publish_ladder(slug: str, ep_dir: str, ladder: str) -> None:
    """把一档 ladder 目录下的 init.mp4 + 全部 .m4s 上传到 OSS，并把同档 m3u8 改写成绝对 URL。

    任一上传失败 / 改写失败 → raise PublishError，由 worker 把 episode 置为 failed。
    本地产物**保留不删**，便于排错和 follow-up "republish" 操作。
    """
    rung_dir: Path = settings.out_dir / slug / ep_dir / ladder
    if not rung_dir.is_dir():
        raise PublishError(f"missing rung dir for publish: {rung_dir}")

    init_name = f"init-{ladder}.mp4"
    init_local = rung_dir / init_name
    if not init_local.is_file():
        raise PublishError(f"missing init file: {init_local}")

    seg_locals = sorted(rung_dir.glob(f"seg-{ladder}-*.m4s"))
    if not seg_locals:
        raise PublishError(f"no segments matched seg-{ladder}-*.m4s in {rung_dir}")

    remote_dir = f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/{ladder}"  # Drama/staging/...
    files_to_upload: list[Path] = [init_local, *seg_locals]
    for local in files_to_upload:
        oss_path = f"{remote_dir}/{local.name}"
        try:
            res = upload_file(oss_path, str(local))
        except Exception as e:  # noqa: BLE001 — oss2 抛的所有异常都视作上传失败
            raise PublishError(
                f"OSS upload raised for {ladder} {local.name}: {e}"
            ) from e
        if not res.get("result"):
            raise PublishError(
                f"OSS upload failed for {ladder} {local.name}: {res}"
            )

    pl_local = rung_dir / f"media-{ladder}.m3u8"
    if not pl_local.is_file():
        raise PublishError(f"missing playlist: {pl_local}")
    try:
        text = pl_local.read_text()
        rewritten = rewrite_playlist(
            text,
            f"{oss_staging_public_base_url}/{slug}/{ep_dir}/{ladder}",
        )
        pl_local.write_text(rewritten)
    except Exception as e:  # noqa: BLE001
        raise PublishError(
            f"m3u8 rewrite failed for {ladder}: {e}"
        ) from e


def publish_ladder_to_prod(slug: str, ep_dir: str, ladder: str) -> str:
    """把一档 ladder 的 staging OSS 对象服务端拷到 prod 前缀，并返回 prod-flavored m3u8 文本。

    入参语义：
      `slug` — drama 目录名；`ep_dir` 形如 `ep-3`；`ladder` 是 `540p`/`720p`/`1080p`。

    幂等：重复调用会覆盖 prod 端对象、返回逐字节相等的 m3u8 文本。
    若 staging 端没有任何对象 → raise PublishError（说明 publish_ladder 还没跑过）。
    """
    src_dir = f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/{ladder}"
    dst_dir = f"{OSS_PROD_PREFIX}/{slug}/{ep_dir}/{ladder}"

    src_keys = oss_upload.list_with_prefix(src_dir + "/")
    if not src_keys:
        raise PublishError(
            f"no staging objects under {src_dir}/; was publish_ladder ever called?"
        )
    for src_key in src_keys:
        if not src_key.endswith((".mp4", ".m4s")):
            continue  # 防御：只拷媒体对象，忽略 m3u8 / 其他
        filename = src_key.rsplit("/", 1)[-1]
        try:
            oss_upload.copy_object(src_key, f"{dst_dir}/{filename}")
        except Exception as e:  # noqa: BLE001
            raise PublishError(
                f"OSS copy_object failed for {ladder} {filename}: {e}"
            ) from e

    # 本地 staging m3u8 → 字符串替换得到 prod 版本。staging base 只会出现在
    # init / segment 行；#EXT-X-KEY:URI="/drm/..." 是相对路径不会命中。
    local_m3u8 = settings.out_dir / slug / ep_dir / ladder / f"media-{ladder}.m3u8"
    if not local_m3u8.is_file():
        raise PublishError(f"missing local playlist: {local_m3u8}")
    try:
        text = local_m3u8.read_text()
    except OSError as e:
        raise PublishError(f"failed to read local playlist {local_m3u8}: {e}") from e
    return text.replace(
        oss_staging_public_base_url + "/",
        oss_prod_public_base_url + "/",
    )


def unpublish_ladder_from_prod(slug: str, ep_dir: str, ladder: str) -> None:
    """删 prod 端某档 ladder 下所有对象。无对象 / 部分缺失 → no-op。"""
    keys = oss_upload.list_with_prefix(
        f"{OSS_PROD_PREFIX}/{slug}/{ep_dir}/{ladder}/"
    )
    oss_upload.batch_delete(keys)


def unpublish_drama_from_prod(slug: str) -> None:
    """删 prod 端整部剧的所有对象（含 poster / 全部集 / 全部 ladder）。"""
    keys = oss_upload.list_with_prefix(f"{OSS_PROD_PREFIX}/{slug}/")
    oss_upload.batch_delete(keys)


def unpublish_episode_from_staging(slug: str, ep_dir: str) -> None:
    """删 staging 端某集的所有对象（三档 ladder × init + 全部 segment）。"""
    keys = oss_upload.list_with_prefix(f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/")
    oss_upload.batch_delete(keys)


def unpublish_drama_from_staging(slug: str) -> None:
    """删 staging 端整部剧的所有对象。drama 删除时调用。"""
    keys = oss_upload.list_with_prefix(f"{OSS_STAGING_PREFIX}/{slug}/")
    oss_upload.batch_delete(keys)
