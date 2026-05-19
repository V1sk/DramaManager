"""Provider-agnostic 发布层：把本地资产（视频切片、海报、封面、字幕）传到 staging 前缀，
并在 sync 时通过 server-side copy 拷到 prod 前缀。

底层桶由 `app/storage/` 抽象，可在 OSS / TOS 之间切换（`STORAGE_PROVIDER` env）；
这层只调用 `storage.provider.{upload_file, copy_object, list_with_prefix, batch_delete}`
和 `storage.provider.{staging_prefix, prod_prefix, staging_base_url, prod_base_url}`，
不感知具体厂商。

四类资产对称处理（assets-to-oss 落地后）：

| 资产 | upload_*_to_staging | publish_*_to_prod | unpublish 单条 (staging) | unpublish 单条 (prod) |
|---|---|---|---|---|
| ladder（init+seg+m3u8 改写） | publish_ladder | publish_ladder_to_prod | —— (不暴露单条删) | unpublish_ladder_from_prod |
| poster | upload_poster_to_staging | publish_poster_to_prod | unpublish_poster_from_staging | unpublish_poster_from_prod |
| cover | upload_cover_to_staging | publish_cover_to_prod | —— | —— |
| subtitle | upload_subtitle_to_staging | publish_subtitle_to_prod | unpublish_subtitle_from_staging | unpublish_subtitle_from_prod |

整体清理（剧 / 集前缀级）由 `unpublish_drama_from_*` / `unpublish_episode_from_*` 包揽，
新增资产前缀天然落在它们的 list-by-prefix 范围内，无需特殊处理。

只在 `settings.storage_enabled` 为真时被调用；调用方决定何时触发。
"""

import re
from pathlib import Path

from . import storage
from .config import settings


def _provider():
    """Resolve the active provider, asserting storage is enabled. Every public
    helper in this module is only meant to run when `settings.storage_enabled`,
    so a None here is a programmer error (the caller failed to gate)."""
    p = storage.provider
    if p is None:
        raise RuntimeError(
            "publish.py called while storage is disabled; "
            "caller must gate on settings.storage_enabled"
        )
    return p


class PublishError(Exception):
    """worker 调用方据此把 episode 状态置为 failed。"""


_MAP_URI_RE = re.compile(r'(URI=")([^"]+)(")')


def rewrite_playlist(text: str, base_url: str) -> str:
    """把 m3u8 里的 init / segment 引用替换成绝对 bucket URL，#EXT-X-KEY 行不动。

    `base_url` 形如 `https://.../Drama/{slug}/{ep_dir}/{ladder}`，已含档位段；
    本函数只在尾部拼"文件名"。允许传入末尾带 / 的 base，内部会去掉。

    幂等：再次对已改写过的 m3u8 调用，输出与输入逐字节相等（迁移脚本依赖）。
    """
    base = base_url.rstrip("/")
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
    """把一档 ladder 目录下的 init.mp4 + 全部 .m4s 上传到 bucket，并把同档 m3u8 改写成绝对 URL。

    任一上传失败 / 改写失败 → raise PublishError，由 worker 把 episode 置为 failed。
    本地产物**保留不删**，便于排错和 follow-up "republish" 操作。
    """
    prov = _provider()
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

    remote_dir = f"{prov.staging_prefix}/{slug}/{ep_dir}/{ladder}"  # Drama/staging/...
    files_to_upload: list[Path] = [init_local, *seg_locals]
    for local in files_to_upload:
        remote_key = f"{remote_dir}/{local.name}"
        try:
            res = prov.upload_file(remote_key, str(local))
        except Exception as e:  # noqa: BLE001 — SDK 抛的所有异常都视作上传失败
            raise PublishError(
                f"storage upload raised for {ladder} {local.name}: {e}"
            ) from e
        if not res.get("result"):
            raise PublishError(
                f"storage upload failed for {ladder} {local.name}: {res}"
            )

    pl_local = rung_dir / f"media-{ladder}.m3u8"
    if not pl_local.is_file():
        raise PublishError(f"missing playlist: {pl_local}")
    try:
        text = pl_local.read_text()
        rewritten = rewrite_playlist(
            text,
            f"{prov.staging_base_url}/{slug}/{ep_dir}/{ladder}",
        )
        pl_local.write_text(rewritten)
    except Exception as e:  # noqa: BLE001
        raise PublishError(
            f"m3u8 rewrite failed for {ladder}: {e}"
        ) from e


def publish_ladder_to_prod(slug: str, ep_dir: str, ladder: str) -> str:
    """把一档 ladder 的 staging bucket 对象服务端拷到 prod 前缀，并返回 prod-flavored m3u8 文本。

    入参语义：
      `slug` — drama 目录名；`ep_dir` 形如 `ep-3`；`ladder` 是 `540p`/`720p`/`1080p`。

    幂等：重复调用会覆盖 prod 端对象、返回逐字节相等的 m3u8 文本。
    若 staging 端没有任何对象 → raise PublishError（说明 publish_ladder 还没跑过）。
    """
    prov = _provider()
    src_dir = f"{prov.staging_prefix}/{slug}/{ep_dir}/{ladder}"
    dst_dir = f"{prov.prod_prefix}/{slug}/{ep_dir}/{ladder}"

    src_keys = prov.list_with_prefix(src_dir + "/")
    if not src_keys:
        raise PublishError(
            f"no staging objects under {src_dir}/; was publish_ladder ever called?"
        )
    for src_key in src_keys:
        if not src_key.endswith((".mp4", ".m4s")):
            continue  # 防御：只拷媒体对象，忽略 m3u8 / 其他
        filename = src_key.rsplit("/", 1)[-1]
        try:
            prov.copy_object(src_key, f"{dst_dir}/{filename}")
        except Exception as e:  # noqa: BLE001
            raise PublishError(
                f"storage copy_object failed for {ladder} {filename}: {e}"
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
        prov.staging_base_url + "/",
        prov.prod_base_url + "/",
    )


def unpublish_ladder_from_prod(slug: str, ep_dir: str, ladder: str) -> None:
    """删 prod 端某档 ladder 下所有对象。无对象 / 部分缺失 → no-op。"""
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.prod_prefix}/{slug}/{ep_dir}/{ladder}/")
    prov.batch_delete(keys)


def unpublish_drama_from_prod(slug: str) -> None:
    """删 prod 端整部剧的所有对象（含 poster / 全部集 / 全部 ladder）。"""
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.prod_prefix}/{slug}/")
    prov.batch_delete(keys)


def unpublish_episode_from_staging(slug: str, ep_dir: str) -> None:
    """删 staging 端某集的所有对象（三档 ladder × init + 全部 segment）。"""
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.staging_prefix}/{slug}/{ep_dir}/")
    prov.batch_delete(keys)


def unpublish_drama_from_staging(slug: str) -> None:
    """删 staging 端整部剧的所有对象。drama 删除时调用。"""
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.staging_prefix}/{slug}/")
    prov.batch_delete(keys)


def unpublish_episode_from_prod(slug: str, ep_dir: str) -> None:
    """删 prod 端某集的所有对象 —— 单次 prefix sweep 覆盖三档 ladder + cover + 字幕。

    取代之前 sync delete 路径对 `unpublish_ladder_from_prod` 的三次循环调用。
    """
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.prod_prefix}/{slug}/{ep_dir}/")
    prov.batch_delete(keys)


# ---------------------------------------------------------------------------
# Per-asset staging upload helpers
#
# 这三个 helper 都是同步阻塞（底层 SDK 是同步的），调用方 `await asyncio.to_thread(...)`。
# 调用方 MUST 在 `settings.storage_enabled` 为真时才调用；为假时调用是程序员错误，raise RuntimeError。
# ---------------------------------------------------------------------------


def _ensure_storage_enabled(caller: str) -> None:
    if not settings.storage_enabled:
        raise RuntimeError(
            f"{caller} called while storage is disabled; "
            f"caller must gate on settings.storage_enabled"
        )


def _put_object(remote_key: str, local_path: Path, label: str) -> None:
    """`provider.upload_file` wrapper，把 result=False 也翻成 PublishError。"""
    prov = _provider()
    try:
        res = prov.upload_file(remote_key, str(local_path))
    except Exception as e:  # noqa: BLE001
        raise PublishError(f"storage upload raised for {label}: {e}") from e
    if not res.get("result"):
        raise PublishError(f"storage upload failed for {label}: {res}")


def upload_poster_to_staging(slug: str, lang: str, local_path: Path) -> str:
    """上传 poster 到 `Drama/staging/{slug}/poster/{lang}.{ext}`。

    `ext` 由 `local_path.suffix` 决定，需带 `.`（例如 `.jpg` / `.png` / `.webp`）。
    返回 staging 公网 URL。上传失败 → PublishError。

    调用方负责：
      - 在调本函数前已经写好 `local_path`。
      - 在调本函数前清理过同 (slug, lang) 旧扩展名的 staging 对象（用 `unpublish_poster_from_staging`），
        否则同一语言可能在 staging 残留两个扩展名的对象。
    """
    _ensure_storage_enabled("upload_poster_to_staging")
    prov = _provider()
    ext = local_path.suffix.lstrip(".")
    if not ext:
        raise PublishError(f"poster local_path has no extension: {local_path}")
    remote_key = f"{prov.staging_prefix}/{slug}/poster/{lang}.{ext}"
    _put_object(remote_key, local_path, f"poster {slug}/{lang}.{ext}")
    return f"{prov.staging_base_url}/{slug}/poster/{lang}.{ext}"


def upload_cover_to_staging(slug: str, ep_dir: str, local_path: Path) -> str:
    """上传 cover 到 `Drama/staging/{slug}/{ep_dir}/cover.jpg`。固定文件名。"""
    _ensure_storage_enabled("upload_cover_to_staging")
    prov = _provider()
    remote_key = f"{prov.staging_prefix}/{slug}/{ep_dir}/cover.jpg"
    _put_object(remote_key, local_path, f"cover {slug}/{ep_dir}")
    return f"{prov.staging_base_url}/{slug}/{ep_dir}/cover.jpg"


def upload_subtitle_to_staging(
    slug: str, ep_dir: str, lang: str, local_path: Path
) -> str:
    """上传 vtt 到 `Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt`。"""
    _ensure_storage_enabled("upload_subtitle_to_staging")
    prov = _provider()
    remote_key = f"{prov.staging_prefix}/{slug}/{ep_dir}/subtitles/{lang}.vtt"
    _put_object(remote_key, local_path, f"subtitle {slug}/{ep_dir}/{lang}")
    return f"{prov.staging_base_url}/{slug}/{ep_dir}/subtitles/{lang}.vtt"


# ---------------------------------------------------------------------------
# Per-asset prod publish helpers (server-side copy staging → prod)
# ---------------------------------------------------------------------------


def _copy_one(src_key: str, dst_key: str, label: str) -> None:
    prov = _provider()
    try:
        prov.copy_object(src_key, dst_key)
    except Exception as e:  # noqa: BLE001
        raise PublishError(f"storage copy_object failed for {label}: {e}") from e


def publish_poster_to_prod(slug: str, lang: str, ext: str) -> str:
    """staging→prod 服务端拷贝 poster。返回 prod 公网 URL。

    `ext` 不带 `.`（与 `upload_poster_to_staging` 返回的扩展名一致）。
    Staging 对象不存在 → PublishError。
    """
    prov = _provider()
    src_key = f"{prov.staging_prefix}/{slug}/poster/{lang}.{ext}"
    dst_key = f"{prov.prod_prefix}/{slug}/poster/{lang}.{ext}"
    # Pre-flight check: staging object must exist
    if not prov.list_with_prefix(src_key):
        raise PublishError(
            f"no staging object at {src_key}; "
            f"upload_poster_to_staging must run first"
        )
    _copy_one(src_key, dst_key, f"poster {slug}/{lang}.{ext}")
    return f"{prov.prod_base_url}/{slug}/poster/{lang}.{ext}"


def publish_cover_to_prod(slug: str, ep_dir: str) -> str:
    """staging→prod 服务端拷贝 cover.jpg。"""
    prov = _provider()
    src_key = f"{prov.staging_prefix}/{slug}/{ep_dir}/cover.jpg"
    dst_key = f"{prov.prod_prefix}/{slug}/{ep_dir}/cover.jpg"
    if not prov.list_with_prefix(src_key):
        raise PublishError(
            f"no staging object at {src_key}; "
            f"upload_cover_to_staging must run first"
        )
    _copy_one(src_key, dst_key, f"cover {slug}/{ep_dir}")
    return f"{prov.prod_base_url}/{slug}/{ep_dir}/cover.jpg"


def publish_subtitle_to_prod(slug: str, ep_dir: str, lang: str) -> str:
    """staging→prod 服务端拷贝 subtitle vtt。"""
    prov = _provider()
    src_key = f"{prov.staging_prefix}/{slug}/{ep_dir}/subtitles/{lang}.vtt"
    dst_key = f"{prov.prod_prefix}/{slug}/{ep_dir}/subtitles/{lang}.vtt"
    if not prov.list_with_prefix(src_key):
        raise PublishError(
            f"no staging object at {src_key}; "
            f"upload_subtitle_to_staging must run first"
        )
    _copy_one(src_key, dst_key, f"subtitle {slug}/{ep_dir}/{lang}")
    return f"{prov.prod_base_url}/{slug}/{ep_dir}/subtitles/{lang}.vtt"


# ---------------------------------------------------------------------------
# Per-asset partial unpublish helpers
#
# 整剧 / 整集 / 整 ladder 删除继续走 `unpublish_drama_from_*` / `unpublish_episode_from_*` /
# `unpublish_ladder_from_prod`（按前缀 list+batch_delete），那条路径自动覆盖新增的
# poster / cover / subtitle 对象，不需要单独的 helper。这里的 helper 只服务于
# "替换一个海报 / 删一条字幕" 这类**单对象**清理场景。
# ---------------------------------------------------------------------------


def unpublish_poster_from_staging(slug: str, lang: str) -> None:
    """删 staging 端 `Drama/staging/{slug}/poster/{lang}.*`（任意扩展名）。

    用于 poster 替换前清掉旧扩展、或单语言海报删除时。Idempotent。
    """
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.staging_prefix}/{slug}/poster/{lang}.")
    prov.batch_delete(keys)


def unpublish_poster_from_prod(slug: str, lang: str) -> None:
    """删 prod 端 `Drama/prod/{slug}/poster/{lang}.*`。Idempotent。"""
    prov = _provider()
    keys = prov.list_with_prefix(f"{prov.prod_prefix}/{slug}/poster/{lang}.")
    prov.batch_delete(keys)


def unpublish_subtitle_from_staging(slug: str, ep_dir: str, lang: str) -> None:
    """删 staging 端单条字幕。Idempotent。"""
    prov = _provider()
    key = f"{prov.staging_prefix}/{slug}/{ep_dir}/subtitles/{lang}.vtt"
    keys = prov.list_with_prefix(key)
    prov.batch_delete(keys)


def unpublish_subtitle_from_prod(slug: str, ep_dir: str, lang: str) -> None:
    """删 prod 端单条字幕。Idempotent。"""
    prov = _provider()
    key = f"{prov.prod_prefix}/{slug}/{ep_dir}/subtitles/{lang}.vtt"
    keys = prov.list_with_prefix(key)
    prov.batch_delete(keys)
