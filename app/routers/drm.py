from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import Response

from ..config import settings

router = APIRouter(prefix="/drm")


@router.get("/{drama_slug}/{episode_id}/key")
async def get_key(
    drama_slug: str = Path(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
    # 放宽以兼容两种历史格式：
    #   短形式   `ep-{n}`               — 新代码 (sdk-drama-listing D8 之后)
    #   完整形式 `{slug}-ep-{n}`        — 旧代码 (hls-management-server 初版)
    # 文件名直接等于 URL 段，两种布局的磁盘产物都能命中对应 key 文件；
    # pattern 仍然保持 slug-like ASCII，阻止任何 `/` 或 `..` 的路径穿越。
    episode_id: str = Path(..., pattern=r"^[a-z0-9][a-z0-9-]*$"),
) -> Response:
    key_path = settings.out_dir / drama_slug / "keys" / f"{episode_id}.key"
    if not key_path.is_file():
        raise HTTPException(status_code=404, detail="key not found")
    data = key_path.read_bytes()
    if len(data) != 16:
        raise HTTPException(status_code=500, detail="key file must be exactly 16 bytes")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )
