import asyncio
import re
from pathlib import Path
from typing import Callable

from .config import settings

_STDERR_TAIL = 4096

# pipeline.sh emits one banner per stage on stdout; we parse them line-by-line
# so the worker can surface a per-rung sub-status to the admin UI. Examples:
#   "== Stage 0: generate DRM material for ep-1"
#   "== Stage 1: encode 720p (clear CMAF)"
#   "== Stage 2: encrypt 1080p segments"
_STAGE_ENCODE_RE = re.compile(r"^== Stage 1: encode (\S+)")
_STAGE_ENCRYPT_RE = re.compile(r"^== Stage 2: encrypt (\S+)")
_STAGE_DRM_RE = re.compile(r"^== Stage 0: generate DRM material")


def _stage_label(line: str) -> str | None:
    if _STAGE_DRM_RE.match(line):
        return "生成密钥"
    m = _STAGE_ENCODE_RE.match(line)
    if m:
        return f"编码 {m.group(1)}"
    m = _STAGE_ENCRYPT_RE.match(line)
    if m:
        return f"加密 {m.group(1)}"
    return None


async def run_pipeline(
    source: Path,
    out_dir: Path,
    episode_id: str,
    key_uri: str,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Invoke pipeline.sh and return (returncode, last 4 KiB of combined stderr).

    All paths MUST be absolute; pipeline.sh resolves its own SCRIPT_DIR so CWD
    does not matter, but stage scripts cd into the rung directory, so relative
    inputs would break.

    `on_progress` (optional) is invoked once per recognized stage marker on
    pipeline.sh's stdout with a Chinese label like "编码 720p". The callback
    runs on the event loop; keep it cheap (the worker passes a small SQLite
    UPDATE).
    """
    proc = await asyncio.create_subprocess_exec(
        str(settings.pipeline_script),
        str(source),
        str(out_dir),
        episode_id,
        key_uri,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_buf = bytearray()

    async def drain_stderr(stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            stderr_buf.extend(chunk)
            if len(stderr_buf) > _STDERR_TAIL * 4:
                del stderr_buf[: len(stderr_buf) - _STDERR_TAIL * 2]

    async def drain_stdout(stream: asyncio.StreamReader) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            if on_progress is None:
                continue
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:  # noqa: BLE001 — never let progress parsing kill the worker
                continue
            label = _stage_label(line)
            if label is not None:
                try:
                    on_progress(label)
                except Exception:  # noqa: BLE001
                    pass

    await asyncio.gather(
        drain_stdout(proc.stdout),
        drain_stderr(proc.stderr),
    )
    rc = await proc.wait()
    tail = bytes(stderr_buf)[-_STDERR_TAIL:].decode("utf-8", errors="replace")
    return rc, tail
