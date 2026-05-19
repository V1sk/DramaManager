"""Storage-provider selector.

At import time this module reads `settings.storage_provider` and instantiates
the matching provider class. Other modules import the singleton:

    from .storage import provider
    provider.upload_file(key, path)
    base = provider.staging_base_url

When storage is disabled (`storage_provider == "none"`), `provider` is `None`
and consumers must gate on `settings.storage_enabled` before touching it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:
    from .base import StorageProvider


def _build_provider() -> "StorageProvider | None":
    name = settings.storage_provider
    if name == "none":
        return None
    if name == "oss":
        # Import lazily so deployments that don't use OSS don't pay the oss2
        # import cost (and don't crash if oss2 isn't installed).
        from .oss_provider import OSSProvider
        return OSSProvider()
    if name == "tos":
        from .tos_provider import TOSProvider
        return TOSProvider()
    # config.load_settings() validates the value before we get here, so this
    # branch is defensive — surfacing it as RuntimeError flags a programming
    # error rather than silently disabling storage.
    raise RuntimeError(f"unknown storage_provider: {name!r}")


provider: "StorageProvider | None" = _build_provider()
