"""Object-storage provider interface.

`publish.py` and friends speak this Protocol and stay provider-agnostic. Each
concrete provider (Aliyun OSS, Volcengine TOS, …) implements the four
primitives + four constants below; the selector in `app/storage/__init__.py`
picks one at import time based on `settings.storage_provider`.

Conventions every provider must honor:
  * Keys are full object keys including the bucket-level prefix
    (e.g. `"Drama/staging/zhetian/ep-1/720p/init-720p.mp4"`), NOT relative to
    `staging_prefix`. Callers (publish.py) assemble the full key by joining
    `staging_prefix` / `prod_prefix` with the asset path.
  * `staging_prefix` and `prod_prefix` are paths WITHOUT trailing slash
    (`"Drama/staging"`, `"Drama/prod"`).
  * `staging_base_url` and `prod_base_url` are public-internet URLs WITHOUT
    trailing slash (`"https://photobundle.oss-../Drama/staging"`). Adding `/`
    is the caller's job.
  * `upload_file` returns a dict shaped `{"result": bool, "code": int, "msg": str}`
    so consumers can keep the existing OSS-shaped truthy check
    (`if not res.get("result")`).
  * All methods are synchronous (the underlying SDKs are sync); callers wrap
    in `await asyncio.to_thread(...)` when running from async code.
"""

from __future__ import annotations

from typing import Protocol


class StorageProvider(Protocol):
    # --- bucket-level constants ---------------------------------------------
    staging_prefix: str       # e.g. "Drama/staging"
    prod_prefix: str          # e.g. "Drama/prod"
    staging_base_url: str     # e.g. "https://bucket.example.com/Drama/staging"
    prod_base_url: str        # e.g. "https://bucket.example.com/Drama/prod"

    # --- four primitives ----------------------------------------------------
    def upload_file(self, remote_key: str, local_file_path: str) -> dict:
        """Upload local file → object at `remote_key`. Returns
        `{"result": bool, "code": int, "msg": str}`. SDK exceptions propagate.
        """
        ...

    def copy_object(self, src_key: str, dst_key: str) -> None:
        """Server-side copy within the same bucket. Raises on non-2xx."""
        ...

    def list_with_prefix(self, prefix: str) -> list[str]:
        """Paginate over every object whose key starts with `prefix`. Returns
        the full list of keys (may be empty).
        """
        ...

    def batch_delete(self, keys: list[str]) -> None:
        """Delete every key in batches (≤1000 per request). Empty list = no-op."""
        ...
