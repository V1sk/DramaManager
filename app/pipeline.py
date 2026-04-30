import asyncio
from pathlib import Path

from .config import settings

_STDERR_TAIL = 4096


async def run_pipeline(
    source: Path,
    out_dir: Path,
    episode_id: str,
    key_uri: str,
) -> tuple[int, str]:
    """Invoke pipeline.sh and return (returncode, last 4 KiB of combined stderr).

    All paths MUST be absolute; pipeline.sh resolves its own SCRIPT_DIR so CWD
    does not matter, but stage scripts cd into the rung directory, so relative
    inputs would break.
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

    async def drain(stream: asyncio.StreamReader, sink: bytearray | None) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            if sink is not None:
                sink.extend(chunk)
                if len(sink) > _STDERR_TAIL * 4:
                    del sink[: len(sink) - _STDERR_TAIL * 2]

    await asyncio.gather(
        drain(proc.stdout, None),
        drain(proc.stderr, stderr_buf),
    )
    rc = await proc.wait()
    tail = bytes(stderr_buf)[-_STDERR_TAIL:].decode("utf-8", errors="replace")
    return rc, tail
