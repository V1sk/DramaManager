"""Unit tests for app.publish.rewrite_playlist.

Covers spec scenarios:
  - EXT-X-MAP 行内层 URI 被替换
  - EXT-X-KEY 行不被改动
  - segment 行被前缀
  - 元数据行透传
  - 已改写的 m3u8 再次改写是 no-op
  - 真实 ffmpeg 产出 m3u8 fixture 跑通
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.publish import rewrite_playlist  # noqa: E402

OSS_BASE = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p"


def test_ext_x_map_uri_rewritten():
    text = '#EXT-X-MAP:URI="init-720p.mp4"\n'
    out = rewrite_playlist(text, OSS_BASE)
    assert out == f'#EXT-X-MAP:URI="{OSS_BASE}/init-720p.mp4"\n'


def test_ext_x_key_line_untouched():
    line = '#EXT-X-KEY:METHOD=AES-128,URI="/drm/zhetian/ep-1/key",IV=0xabcd\n'
    out = rewrite_playlist(line, OSS_BASE)
    assert out == line


def test_segment_line_prefixed():
    text = "seg-720p-3.m4s\n"
    out = rewrite_playlist(text, OSS_BASE)
    assert out == f"{OSS_BASE}/seg-720p-3.m4s\n"


def test_metadata_lines_passthrough():
    text = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        "#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXTINF:2.000000,\n"
        "#EXT-X-ENDLIST\n"
    )
    out = rewrite_playlist(text, OSS_BASE)
    assert out == text


def test_idempotent_second_rewrite_is_noop():
    raw = (REPO_ROOT / "tests/fixtures/sample-720p.m3u8").read_text()
    once = rewrite_playlist(raw, OSS_BASE)
    twice = rewrite_playlist(once, OSS_BASE)
    assert twice == once


def test_real_ffmpeg_fixture():
    raw = (REPO_ROOT / "tests/fixtures/sample-720p.m3u8").read_text()
    out = rewrite_playlist(raw, OSS_BASE)

    # #EXT-X-KEY 行字节不变
    key_line = '#EXT-X-KEY:METHOD=AES-128,URI="/drm/zhetian/ep-1/key",IV=0xabcdef0123456789abcdef0123456789\n'
    assert key_line in out
    assert key_line in raw

    # init 在 #EXT-X-MAP:URI= 里被替换
    assert f'#EXT-X-MAP:URI="{OSS_BASE}/init-720p.mp4"\n' in out
    assert '#EXT-X-MAP:URI="init-720p.mp4"' not in out

    # 所有 seg-720p-N.m4s 都被替换
    for i in range(4):
        original = f"seg-720p-{i}.m4s"
        assert f"{OSS_BASE}/{original}\n" in out
        # 原始裸文件名行不应再出现（注意区分：URL 里的尾段是允许保留的）
        assert f"\n{original}\n" not in out
        assert not out.startswith(f"{original}\n")


def test_blank_lines_passthrough():
    text = "\n#EXTM3U\n\nseg-720p-0.m4s\n\n"
    out = rewrite_playlist(text, OSS_BASE)
    assert out == f"\n#EXTM3U\n\n{OSS_BASE}/seg-720p-0.m4s\n\n"


def test_oss_base_with_trailing_slash_trimmed():
    text = "seg-720p-0.m4s\n"
    out = rewrite_playlist(text, OSS_BASE + "/")
    assert "//seg-720p-0.m4s" not in out
    assert out == f"{OSS_BASE}/seg-720p-0.m4s\n"


def test_already_absolute_segment_url_passthrough():
    # 幂等：已经是绝对 URL 的 segment 行不再叠前缀
    line = f"{OSS_BASE}/seg-720p-0.m4s\n"
    out = rewrite_playlist(line, OSS_BASE)
    assert out == line


def test_already_absolute_map_uri_passthrough():
    line = f'#EXT-X-MAP:URI="{OSS_BASE}/init-720p.mp4"\n'
    out = rewrite_playlist(line, OSS_BASE)
    assert out == line


if __name__ == "__main__":
    # 简易跑法：直接 python tests/test_publish.py
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
