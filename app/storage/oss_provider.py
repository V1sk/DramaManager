"""Aliyun OSS implementation of `StorageProvider`.

Ported from the original `app/oss_upload.py`. Endpoint / bucket are not
secrets and stay in source; the AccessKey id/secret pair is per-developer
and lives in the gitignored `credentials.py` — copy `credentials.example.py`
to create it (see CLAUDE.md OSS section).
"""

from __future__ import annotations

import oss2

try:
    from .credentials import OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "app/storage/credentials.py not found — copy "
        "app/storage/credentials.example.py to credentials.py and fill in "
        "your OSS AccessKey id/secret."
    ) from exc


# --- bucket-level config (not secret; safe to keep in source) --------------
_ENDPOINT = "https://oss-ap-southeast-1.aliyuncs.com"
_BUCKET_NAME = "photobundle"
# Bucket-level top-level directory; staging/prod are siblings under it.
_BASE_DIR = "Drama"


class OSSProvider:
    """Stateful wrapper around `oss2.Bucket`. One instance per process, created
    once by `app/storage/__init__.py` when `STORAGE_PROVIDER=oss`.
    """

    def __init__(self) -> None:
        auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
        self._bucket = oss2.Bucket(auth, _ENDPOINT, _BUCKET_NAME)
        self._bucket_name = _BUCKET_NAME

        # Public base URL for the bucket root. Derived once; consumers read the
        # staging/prod variants below.
        public_base = f"https://{_BUCKET_NAME}.oss-ap-southeast-1.aliyuncs.com/{_BASE_DIR}"

        self.staging_prefix: str = f"{_BASE_DIR}/staging"
        self.prod_prefix: str = f"{_BASE_DIR}/prod"
        self.staging_base_url: str = f"{public_base}/staging"
        self.prod_base_url: str = f"{public_base}/prod"

    # --- primitives ---------------------------------------------------------
    def upload_file(self, remote_key: str, local_file_path: str) -> dict:
        result = self._bucket.put_object_from_file(remote_key, local_file_path)
        response = result.resp.response
        return {
            "result": response.ok,
            "code": response.status_code,
            "msg": response.reason,
        }

    def copy_object(self, src_key: str, dst_key: str) -> None:
        # Same-bucket server-side copy. oss2 raises oss2.exceptions.OssError on
        # non-2xx; publish.py catches as bare Exception and rewraps.
        self._bucket.copy_object(self._bucket_name, src_key, dst_key)

    def list_with_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        marker = ""
        while True:
            result = self._bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
            keys.extend(o.key for o in result.object_list)
            if not result.is_truncated:
                break
            marker = result.next_marker
        return keys

    def batch_delete(self, keys: list[str]) -> None:
        if not keys:
            return
        for i in range(0, len(keys), 1000):
            self._bucket.batch_delete_objects(keys[i:i + 1000])
