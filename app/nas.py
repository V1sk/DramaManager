"""NAS source ingest helpers (nas-source-ingest).

The deploy box shares a read-only mount with operators (the company NAS, e.g.
`/Volumes/酷讯短剧组`). Instead of re-uploading 片源 through the browser, an
operator points at a file/folder already on that mount and the pipeline encodes
it IN PLACE. These helpers confine every operator-supplied path to the configured
root (`SOURCE_NAS_DIR`) so a crafted `../` / symlink can't read arbitrary server
files, and list directories for the in-page browser.

Path policy: candidates are `resolve()`d (which collapses `..` and follows
symlinks) and then checked to still live under the resolved root — so a symlink
inside the NAS that points outside the root is rejected, not followed out.
"""
from __future__ import annotations

from pathlib import Path

from .config import settings

# Video container extensions we let operators pick. ffmpeg handles far more, but
# this keeps the browser focused on plausible 片源 and avoids listing junk.
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".ts", ".webm"}


class NasPathError(ValueError):
    """Operator-facing path problem (out of root / missing / wrong type)."""


def root() -> Path | None:
    return settings.source_nas_dir


def is_enabled() -> bool:
    """True iff a root is configured AND currently a reachable directory."""
    r = root()
    return r is not None and r.is_dir()


def _root_resolved() -> Path:
    r = root()
    if r is None or not r.is_dir():
        raise NasPathError("NAS 源目录未启用或不可访问")
    return r.resolve()


def resolve(rel: str, *, must_be: str | None = None) -> Path:
    """Resolve `rel` (relative to the NAS root) into an absolute path confined to
    the root. `must_be` ∈ {'file','dir'} enforces the entry type. Raises
    `NasPathError` on any violation (escape / missing / wrong type)."""
    root_resolved = _root_resolved()
    rel = (rel or "").strip().strip("/")
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise NasPathError("路径越界（必须在 NAS 源目录内）")
    if not candidate.exists():
        raise NasPathError("路径不存在（可能已被移动 / NAS 未挂载）")
    if must_be == "file" and not candidate.is_file():
        raise NasPathError("选择的不是文件")
    if must_be == "dir" and not candidate.is_dir():
        raise NasPathError("选择的不是文件夹")
    return candidate


def rel_of(p: Path) -> str:
    """Path relative to the root (''=root). Assumes `p` is already confined."""
    rp = p.resolve()
    root_resolved = _root_resolved()
    return "" if rp == root_resolved else str(rp.relative_to(root_resolved))


def list_dir(rel: str) -> dict:
    """List one directory for the in-page browser. Returns
    `{path, parent, dirs:[{name,rel}], files:[{name,rel,size}]}`, sorted
    case-insensitively, hiding dotfiles and any entry that resolves outside the
    root (escaping symlinks). Only `VIDEO_EXTS` files are listed."""
    d = resolve(rel, must_be="dir")
    root_resolved = _root_resolved()
    dirs: list[dict] = []
    files: list[dict] = []
    for entry in sorted(d.iterdir(), key=lambda e: e.name.lower()):
        if entry.name.startswith("."):
            continue
        try:
            rp = entry.resolve()
            entry_rel = str(rp.relative_to(root_resolved))  # ValueError if escapes
        except (ValueError, OSError):
            continue  # symlink out of root, or broken entry → hide it
        try:
            if entry.is_dir():
                dirs.append({"name": entry.name, "rel": entry_rel})
            elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                files.append({"name": entry.name, "rel": entry_rel, "size": entry.stat().st_size})
        except OSError:
            continue
    cur_rel = rel_of(d)
    parent_rel = None
    if cur_rel:
        parent = str(Path(cur_rel).parent)
        parent_rel = "" if parent == "." else parent
    return {"path": cur_rel, "parent": parent_rel, "dirs": dirs, "files": files}
