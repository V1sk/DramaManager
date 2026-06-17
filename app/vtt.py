"""WebVTT 解析 / 重组工具：只动 cue 文本，时间轴 / cue 标识 / 头部 / NOTE / STYLE
原样保留。供 AI 字幕翻译用——抽出每个 cue 的文本去翻译，再按相同结构塞回译文。

约束（与 encrypt-segments.sh / SDK 无关，纯文本处理）：
  - 行尾统一归一化成 `\n`。
  - 以空行分块，这正是 WebVTT 分隔 cue 的方式，所以一个 cue 自身的（多行）文本
    永远不含空行；据此抽取/还原 cue 文本不会越界吃到下一个 cue。
"""
from __future__ import annotations


def parse_cues(vtt_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """把 WebVTT 文档切成有序块并抽出 cue 文本。

    返回 `(blocks, texts)`：
      - `blocks`: `(kind, payload)` 列表。`kind='raw'` → `payload` 是逐字保留的块
        （WEBVTT 头 / NOTE / STYLE / 空块）；`kind='cue'` → `payload` 是该 cue 的
        前缀（可选的标识行 + `-->` 时间轴行，逐字保留），其文本是 `texts` 中对应项。
      - `texts`: 按文档顺序的 cue 文本字符串，每个 `cue` 块一条。
    """
    # rstrip trailing newlines so a final EOF newline isn't absorbed into the
    # last cue's text (which would send a stray blank line to the translator).
    norm = vtt_text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    blocks: list[tuple[str, str]] = []
    texts: list[str] = []
    for raw in norm.split("\n\n"):
        lines = raw.split("\n")
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            blocks.append(("raw", raw))
        else:
            prefix = "\n".join(lines[: timing_idx + 1])
            text = "\n".join(lines[timing_idx + 1 :])
            blocks.append(("cue", prefix))
            texts.append(text)
    return blocks, texts


def rebuild(blocks: list[tuple[str, str]], translated: list[str]) -> str:
    """用 `parse_cues` 的 blocks + 等长 `translated`（顺序/数量与 `texts` 一致）
    重组 WebVTT 文档：raw 块逐字输出，cue 块用译文替换原文本。"""
    out: list[str] = []
    ti = 0
    for kind, payload in blocks:
        if kind == "raw":
            out.append(payload)
        else:
            text = translated[ti] if ti < len(translated) else ""
            ti += 1
            out.append(payload + ("\n" + text if text else ""))
    return "\n\n".join(out)


def cue_count(blocks: list[tuple[str, str]]) -> int:
    return sum(1 for kind, _ in blocks if kind == "cue")
