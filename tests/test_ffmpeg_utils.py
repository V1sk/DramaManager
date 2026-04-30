"""Unit tests for app.ffmpeg_utils.probe_video_dimensions.

Covers spec scenarios:
  - 上传成功后宽高写入 DB（取真实样本，断言返回值）
  - ffprobe 探测失败拒收（坏文件 → FfmpegError）
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.ffmpeg_utils import FfmpegError, probe_video_dimensions  # noqa: E402

FIXTURE = REPO_ROOT / "tests/fixtures/sample-720x1280.mp4"


def test_probe_dimensions_real_fixture():
    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"
    w, h = probe_video_dimensions(FIXTURE)
    assert (w, h) == (720, 1280), f"expected (720, 1280), got ({w}, {h})"


def test_probe_dimensions_broken_file_raises():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"this is not a valid video")
        bad = Path(f.name)
    try:
        try:
            probe_video_dimensions(bad)
        except FfmpegError as e:
            assert "ffprobe" in str(e).lower() or "width" in str(e).lower()
            return
        raise AssertionError("expected FfmpegError on broken file")
    finally:
        bad.unlink(missing_ok=True)


def test_probe_dimensions_missing_file_raises():
    try:
        probe_video_dimensions(Path("/tmp/this-does-not-exist-xyz.mp4"))
    except FfmpegError:
        return
    raise AssertionError("expected FfmpegError on missing file")


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"OK {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    if fails:
        sys.exit(1)
    print(f"\n{len(fns)} passed")
