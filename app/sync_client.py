"""HTTP 客户端：把请求打到业务服务器的 `/sync/*` 端点。

这一层只负责"发请求 / 解析返回 / 翻译错误"，不知道 sync 状态机的细节。
worker (`app.sync`) 调用 `call_business`，再根据结果写 DB。

启动 / 关闭由 `app.main` 的 lifespan hook 管理：
  - `await sync_client.startup()` 在 worker 起来前执行（创建 module-level
    AsyncClient，注入 `X-API-Key`）。
  - `await sync_client.shutdown()` 在 worker 取消之后执行（关闭连接池）。

`BUSINESS_SYNC_BASE_URL` 未设时不应被调用 —— 调用方（admin 路由 / sync worker）
通过 `settings.business_sync_base_url` 决定是否进入这条路径。
"""
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("hls.sync_client")


class SyncError(Exception):
    """业务服务器返回非 2xx 时抛出。`status_code` + 截断后的 body 用于落库。"""

    _BODY_LIMIT = 512

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body[: self._BODY_LIMIT]
        super().__init__(f"business sync HTTP {status_code}: {self.body}")


_client: httpx.AsyncClient | None = None


async def startup() -> None:
    """Create the module-level AsyncClient. Idempotent: a second call is a no-op."""
    global _client
    if _client is not None:
        return
    if not settings.business_sync_base_url or not settings.business_sync_api_key:
        # Should be unreachable: lifespan only calls startup() when both are set.
        raise RuntimeError(
            "sync_client.startup() called without BUSINESS_SYNC_BASE_URL / "
            "BUSINESS_SYNC_API_KEY in settings"
        )
    _client = httpx.AsyncClient(
        base_url=settings.business_sync_base_url,
        timeout=settings.business_sync_timeout,
        headers={"X-API-Key": settings.business_sync_api_key},
    )
    log.info(
        "sync_client up: base=%s timeout=%ds",
        settings.business_sync_base_url, settings.business_sync_timeout,
    )


async def shutdown() -> None:
    global _client
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception:  # noqa: BLE001 — shutdown best-effort
        log.exception("sync_client shutdown raised; ignoring")
    _client = None


def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError(
            "sync_client is not started; call startup() in lifespan first"
        )
    return _client


async def call_business(
    method: str,
    path: str,
    *,
    json: Any = None,
) -> dict[str, Any] | None:
    """Wrap a single `/sync/*` HTTP call. `path` is relative to base_url, e.g. `/sync/dramas`.

    On 2xx with a JSON body → returns the parsed dict (or None for empty body).
    On 2xx with no body (e.g. 204 from DELETE) → returns None.
    On non-2xx → raises `SyncError(status_code, truncated_body)`.
    Network / timeout failures bubble up as their httpx exceptions; the worker
    catches them and writes `sync_failed`.
    """
    client = _require_client()
    response = await client.request(method, path, json=json)
    if response.status_code >= 400:
        # body might be JSON-encoded error; we keep it as text for the badge tooltip.
        text = response.text or ""
        raise SyncError(response.status_code, text)
    if not response.content:
        return None
    ctype = response.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None
    try:
        return response.json()
    except ValueError:
        return None
