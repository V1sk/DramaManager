"""Volcengine TOS implementation of `StorageProvider`.

Mirrors the OSS provider's API surface so `publish.py` stays provider-agnostic.
The bucket layout (`Drama/staging/...` + `Drama/prod/...`) is identical to the
OSS side, just inside a different bucket / endpoint.
"""

from __future__ import annotations

import tos
from tos.models2 import ObjectTobeDeleted

try:
    from .credentials import TOS_ACCESS_KEY, TOS_SECRET_KEY
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "app/storage/credentials.py not found — copy "
        "app/storage/credentials.example.py to credentials.py and fill in "
        "your TOS AccessKey/SecretKey."
    ) from exc


# --- bucket-level config (not secret; safe to keep in source) --------------
_ENDPOINT = "tos-ap-southeast-1.volces.com"
_REGION = "ap-southeast-1"
_BUCKET_NAME = "coocent-drama"
_BASE_DIR = "Drama"


class TOSProvider:
    """Stateful wrapper around `tos.TosClientV2`. One instance per process,
    created once by `app/storage/__init__.py` when `STORAGE_PROVIDER=tos`.
    """

    def __init__(self) -> None:
        self._client = tos.TosClientV2(TOS_ACCESS_KEY, TOS_SECRET_KEY, _ENDPOINT, _REGION)
        self._bucket_name = _BUCKET_NAME

        # Virtual-hosted-style public URL: https://{bucket}.{endpoint}/{key}
        public_base = f"https://{_BUCKET_NAME}.{_ENDPOINT}/{_BASE_DIR}"

        self.staging_prefix: str = f"{_BASE_DIR}/staging"
        self.prod_prefix: str = f"{_BASE_DIR}/prod"
        self.staging_base_url: str = f"{public_base}/staging"
        self.prod_base_url: str = f"{public_base}/prod"

    # --- primitives ---------------------------------------------------------
    def upload_file(self, remote_key: str, local_file_path: str) -> dict:
        # TOS SDK raises TosClientError / TosServerError on failure; if
        # put_object_from_file returns successfully, the upload was 2xx. We
        # synthesize the OSS-shaped dict so publish.py's truthy check still
        # works without per-provider branches.
        resp = self._client.put_object_from_file(
            self._bucket_name, remote_key, local_file_path,
        )
        return {
            "result": True,
            "code": int(getattr(resp, "status_code", 200) or 200),
            "msg": getattr(resp, "request_id", "") or "",
        }

    def copy_object(self, src_key: str, dst_key: str) -> None:
        # TOS copy_object signature: (dst_bucket, dst_key, src_bucket, src_key).
        # Same bucket on both sides for our staging→prod use case.
        self._client.copy_object(
            self._bucket_name, dst_key, self._bucket_name, src_key,
        )

    def list_with_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        # `list_objects_type2` is the modern paginator (continuation token).
        # `list_only_once=False` lets the SDK auto-paginate on the wire when we
        # leave `continuation_token` unset, but we still loop explicitly to
        # cap memory and stay symmetric with the OSS provider.
        token: str | None = None
        while True:
            result = self._client.list_objects_type2(
                self._bucket_name,
                prefix=prefix,
                max_keys=1000,
                continuation_token=token,
                list_only_once=True,
            )
            keys.extend(o.key for o in (result.contents or []))
            if not result.is_truncated:
                break
            token = result.next_continuation_token
            if not token:
                break
        return keys

    def batch_delete(self, keys: list[str]) -> None:
        if not keys:
            return
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            self._client.delete_multi_objects(
                self._bucket_name,
                [ObjectTobeDeleted(key=k) for k in chunk],
                quiet=True,
            )
