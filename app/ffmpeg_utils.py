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


def probe_video_dimensions(src: Path) -> tuple[int, int]:
    """探测源视频第一条 video stream 的 codec width / height（像素单位，非 SAR
    校正后的 display dimension）。语义对齐 ExoPlayer Format.width/height，方便客户端
    在拉到 EpisodeInfo 时直接预定渲染容器尺寸、避免首帧后回弹抖动。
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=,:p=0",
            str(src),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise FfmpegError(f"ffprobe failed ({result.returncode}): {result.stderr.strip()[-1000:]}")
    out = result.stdout.strip()
    if not out:
        raise FfmpegError("ffprobe returned empty width/height")
    parts = out.split(",")
    if len(parts) != 2:
        raise FfmpegError(f"ffprobe returned unparseable width/height: {out!r}")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as e:
        raise FfmpegError(f"ffprobe returned non-integer width/height: {out!r}") from e
    if width <= 0 or height <= 0:
        raise FfmpegError(f"ffprobe returned non-positive width/height: {width}x{height}")
    return width, height


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
