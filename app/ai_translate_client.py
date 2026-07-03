"""HTTP 客户端 + 提示词封装：调用 kie.ai 风格的 OpenAI 兼容 chat-completions
端点，对短文本（剧名 / 简介、标签 label、演员 name）做多语言翻译。

这一层只负责"拼 prompt / 发请求 / 解析 JSON / 翻译错误"，**不碰 DB**。调用方
（`app.routers.ai_translate`）拿到 `{lang_code: {field: value}}` 后自行 upsert +
mark dirty，落进现有 translations 存储与 staging→同步 状态机。

接口形态（kie.ai `gpt-5-2`，同步 chat completions）：
  POST {base_url}/{model}/v1/chat/completions
  Authorization: Bearer {api_key}
  body: {"messages": [...], "reasoning_effort": "low"}
  返回: choices[0].message.content（字符串或 content-part 数组）

启动 / 关闭由 `app.main` 的 lifespan 管理，仅当 `settings.ai_translate_enabled`
为真（即 `AI_TRANSLATE_API_KEY` 已设）时才创建客户端。
"""
import json
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("hls.ai_translate")


class AITranslateError(Exception):
    """AI 翻译失败（HTTP 非 2xx / 网络错误 / 返回不可解析）。message 给操作员看。

    `retryable=True` 标记瞬时失败（网络 / 超时 / 429 / 网关抖动 / 模型偶发条数不符），
    供 ai-translation-queue 的 worker 决定是否退避重试；鉴权 / 余额 / 结构性错误为
    False（重试也没用）。
    """

    _MSG_LIMIT = 600

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message[: self._MSG_LIMIT])
        self.retryable = retryable


# 系统提示：把模型钉死成"剧集元数据翻译器 + 只输出 JSON"。
_SYSTEM_PROMPT = (
    "You are a professional translator specializing in short-drama (web series) "
    "catalog metadata: drama titles, one-paragraph synopses, genre tags, and "
    "actor names. Translate the provided fields from the source language into "
    "EACH requested target language.\n"
    "Rules:\n"
    "- Preserve proper nouns and character / person names; transliterate a name "
    "only when that is the established convention for the target language.\n"
    "- Keep titles concise and idiomatic for the target locale; never add quotes, "
    "brackets, or explanations.\n"
    "- Translate meaning, not word-for-word, keeping the tone of a streaming "
    "catalog.\n"
    "- Output MUST be a single JSON object and nothing else: no markdown, no code "
    "fences, no commentary. Top-level keys are the EXACT target language code "
    "strings given in `target_languages[].code`. Each value is an object mapping "
    "every field key in `fields` to its translated string. Include every "
    "requested language and every field."
)


_client: httpx.AsyncClient | None = None


async def startup() -> None:
    """Create the module-level AsyncClient. Idempotent: a second call is a no-op."""
    global _client
    if _client is not None:
        return
    if not settings.ai_translate_enabled:
        # Should be unreachable: lifespan only calls startup() when enabled.
        raise RuntimeError(
            "ai_translate_client.startup() called without AI_TRANSLATE_API_KEY"
        )
    # trust_env=False: ignore the operator's HTTP(S)_PROXY / ALL_PROXY. The
    # translate endpoint is the one deliberate outbound hop (kie.ai); routing it
    # through a developer's shell proxy is wrong and pulls in socksio.
    _client = httpx.AsyncClient(
        base_url=settings.ai_translate_base_url,
        timeout=settings.ai_translate_timeout,
        headers={"Authorization": f"Bearer {settings.ai_translate_api_key}"},
        trust_env=False,
    )
    log.info(
        "ai_translate up: base=%s model=%s timeout=%ds",
        settings.ai_translate_base_url,
        settings.ai_translate_model,
        settings.ai_translate_timeout,
    )


async def shutdown() -> None:
    global _client
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception:  # noqa: BLE001 — shutdown best-effort
        log.exception("ai_translate shutdown raised; ignoring")
    _client = None


def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError(
            "ai_translate_client is not started; call startup() in lifespan first"
        )
    return _client


def _content_to_text(content: Any) -> str:
    """Flatten an OpenAI-style message content (str OR list of content parts)
    into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return "".join(parts)
    return str(content)


def _parse_json_object(text: str) -> dict:
    """Parse the model output into a dict. Tolerates markdown code fences or
    surrounding prose by falling back to the outermost `{...}` span."""
    s = (text or "").strip()
    try:
        obj = json.loads(s)
    except ValueError:
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise AITranslateError("AI 翻译返回的不是有效 JSON", retryable=True)
        try:
            obj = json.loads(s[start : end + 1])
        except ValueError as e:
            raise AITranslateError(
                f"AI 翻译返回的 JSON 无法解析：{e}",
                retryable=True,
            ) from e
    if not isinstance(obj, dict):
        raise AITranslateError("AI 翻译返回的 JSON 顶层不是对象", retryable=True)
    return obj


def _parse_json_array(text: str) -> list:
    """Parse the model output into a JSON array. Tolerates code fences / prose by
    falling back to the outermost `[...]`, and unwraps a single wrapping key
    (e.g. `{"translations": [...]}`)."""
    s = (text or "").strip()
    try:
        obj = json.loads(s)
    except ValueError:
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise AITranslateError("字幕翻译返回的不是 JSON 数组", retryable=True)
        try:
            obj = json.loads(s[start : end + 1])
        except ValueError as e:
            raise AITranslateError(
                f"字幕翻译返回的 JSON 数组无法解析：{e}",
                retryable=True,
            ) from e
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                obj = v
                break
    if not isinstance(obj, list):
        raise AITranslateError("字幕翻译返回的 JSON 不是数组", retryable=True)
    return obj


async def _chat(messages: list) -> str:
    """POST one chat-completion and return the assistant message text.

    Centralizes the kie.ai quirk handling (HTTP-200 `{code,msg}` error envelope,
    missing `choices`, transport errors) so every caller surfaces the real
    reason as an AITranslateError instead of an opaque structural error."""
    client = _require_client()
    body = {"messages": messages, "reasoning_effort": "low"}
    path = f"/{settings.ai_translate_model}/v1/chat/completions"
    try:
        resp = await client.post(path, json=body)
    except httpx.HTTPError as e:
        # transport / timeout → transient, worth a backoff retry
        raise AITranslateError(f"AI 翻译请求失败（网络 / 超时）：{e}", retryable=True) from e

    try:
        data = resp.json()
    except ValueError as e:
        # non-JSON body (often a transient gateway blip) → retryable
        raise AITranslateError(
            f"AI 翻译响应不是 JSON (HTTP {resp.status_code}): {(resp.text or '')[:300]}",
            retryable=True,
        ) from e

    # kie.ai 即使出错也常返回 HTTP 200，把真实状态塞进 {"code": <非2xx>, "msg": "..."}
    # 信封里、且没有 "choices"。先识别这种错误信封，把鉴权 / 余额 / 限流 / 长度超限的
    # 真实原因透出，否则下面取 choices 会抛含糊异常 → 路由 502 无信息。
    code = data.get("code") if isinstance(data, dict) else None
    msg = data.get("msg") if isinstance(data, dict) else None
    if resp.status_code >= 400:
        raise AITranslateError(
            f"AI 翻译服务返回 HTTP {resp.status_code}"
            + (f" (code={code}): {msg}" if msg else f": {(resp.text or '')[:300]}"),
            retryable=(resp.status_code == 429 or resp.status_code >= 500),
        )
    if not isinstance(data, dict) or "choices" not in data:
        if msg:
            raise AITranslateError(
                f"AI 翻译服务返回错误 (code={code}): {msg}",
                retryable=(code == 429),
            )
        snippet = json.dumps(data, ensure_ascii=False)[:300] if isinstance(data, dict) else str(data)[:300]
        raise AITranslateError(f"AI 翻译响应缺少 choices：{snippet}", retryable=True)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise AITranslateError(
            f"AI 翻译响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}",
            retryable=True,
        ) from e
    return _content_to_text(content)


async def translate_texts(
    *,
    source_lang_label: str,
    targets: list[dict[str, str]],
    fields: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Translate `fields` from the source language into every target language.

    Args:
      source_lang_label: human-readable source language (e.g. "简体中文 (zh-rCN)").
      targets: `[{"code": "<lang_code>", "label": "<display_label>"}, ...]` — the
        languages to translate into. `code` strings become the result keys.
      fields: `{field_key: source_text}` — non-empty source strings to translate.

    Returns `{lang_code: {field_key: translated_text}}`, containing only the
    languages and fields the model actually returned as non-empty strings.

    Raises AITranslateError on transport / HTTP / parse failure, or when nothing
    usable came back.
    """
    if not targets or not fields:
        return {}
    user_payload = {
        "source_language": source_lang_label,
        "target_languages": targets,
        "fields": fields,
        "instructions": (
            "Return ONE JSON object keyed by each target_languages[].code, whose "
            "value maps every key in `fields` to its translation."
        ),
    }
    messages = [
        {"role": "system", "content": [{"type": "text", "text": _SYSTEM_PROMPT}]},
        {"role": "user", "content": [{"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)}]},
    ]
    parsed = _parse_json_object(await _chat(messages))
    wanted = {t["code"] for t in targets}
    out: dict[str, dict[str, str]] = {}
    for code, fieldmap in parsed.items():
        if code not in wanted or not isinstance(fieldmap, dict):
            continue
        clean: dict[str, str] = {}
        for fk in fields:
            v = fieldmap.get(fk)
            if isinstance(v, str) and v.strip():
                clean[fk] = v.strip()
        if clean:
            out[code] = clean
    if not out:
        raise AITranslateError(
            "AI 翻译未返回任何可用的目标语言译文（格式不符或为空）",
            retryable=True,
        )
    return out


# 字幕翻译：把模型钉死成"逐 cue、等长数组、只翻文本"。等长是硬约束——调用方拿到
# 数量不符就判失败、绝不写半成品 VTT（否则字幕与时间轴会整体错位）。
_SUBTITLE_SYSTEM_PROMPT = (
    "You are a professional subtitle translator. You receive a JSON array of "
    "subtitle cue texts in the source language. Translate EACH element into the "
    "target language and return a JSON array of the SAME length and SAME order, "
    "where element i is the translation of input element i.\n"
    "Strict rules:\n"
    "- Output ONLY the JSON array — no markdown, no code fences, no commentary.\n"
    "- The output array length MUST equal the input array length. Never merge, "
    "split, add, drop, or reorder entries.\n"
    "- Translate each cue independently; keep it concise and natural for on-screen "
    "subtitles. Preserve a cue's internal line breaks (\\n) where present.\n"
    "- Keep proper nouns / names consistent. If an element has no translatable "
    "text (e.g. a musical note or a number), return it unchanged."
)


async def translate_lines(
    *,
    source_lang_label: str,
    target_lang_label: str,
    lines: list[str],
) -> list[str]:
    """Translate a list of subtitle cue texts into one target language, returning
    a list of the SAME length / order. Raises AITranslateError when the model
    returns a different number of items, so the caller never writes a misaligned
    VTT."""
    if not lines:
        return []
    user_payload = {
        "source_language": source_lang_label,
        "target_language": target_lang_label,
        "cues": lines,
        "instructions": "Return a JSON array of translations, one per input cue, same order and length.",
    }
    messages = [
        {"role": "system", "content": [{"type": "text", "text": _SUBTITLE_SYSTEM_PROMPT}]},
        {"role": "user", "content": [{"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)}]},
    ]
    arr = _parse_json_array(await _chat(messages))
    if len(arr) != len(lines):
        # model nondeterminism — a retry may yield the right count
        raise AITranslateError(
            f"字幕翻译条数不匹配：输入 {len(lines)} 条，返回 {len(arr)} 条",
            retryable=True,
        )
    return ["" if x is None else str(x) for x in arr]
