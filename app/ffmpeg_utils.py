import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def probe_duration_ms(src: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(src),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise FfmpegError(f"ffprobe failed ({result.returncode}): {result.stderr.strip()}")
    out = result.stdout.strip()
    if not out:
        raise FfmpegError("ffprobe returned no duration")
    try:
        seconds = float(out)
    except ValueError as e:
        raise FfmpegError(f"ffprobe returned unparseable duration: {out!r}") from e
    return int(seconds * 1000)


def extract_first_frame(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", "0",
            "-i", str(src),
            "-vframes", "1",
            "-vf", "scale=-2:720",
            str(dst),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise FfmpegError(f"ffmpeg cover extraction failed ({result.returncode}): {result.stderr[-2000:]}")
    if not dst.exists():
        raise FfmpegError(f"ffmpeg reported success but {dst} is missing")
