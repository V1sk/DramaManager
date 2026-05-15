"""Standalone demo "business server" for testing the HLS staging-side sync flow.

Run:
    ./venv/bin/python scripts/demo_business_server.py
        --host 0.0.0.0 --port 9000 --api-key demo-secret-key

Then on the HLS staging server, point sync at it:

    export BUSINESS_SYNC_BASE_URL=http://127.0.0.1:9000
    export BUSINESS_SYNC_API_KEY=demo-secret-key
    ./venv/bin/uvicorn app.main:app --port 8000

The 4 endpoints below mirror the wire protocol documented in CLAUDE.md
("业务服务器线协议") and the archived OpenSpec design at
`openspec/changes/archive/2026-05-07-business-server-sync/design.md`:

    POST   /sync/dramas              upsert drama + translations + tags + actors + languages
    DELETE /sync/dramas/{slug}       remove drama
    POST   /sync/episodes            upsert one episode (incl. m3u8 text + DRM)
    DELETE /sync/episodes/{slug}/{ep}  remove one episode

Behavior:
- Every request validates `X-API-Key`. Missing / wrong → 401.
- Every payload is pretty-printed to stdout for easy inspection.
- State lives in memory (no DB). `GET /` is a small HTML dashboard;
  `GET /_state` returns the same data as JSON.
- Stale-payload guard (409 when an incoming `client_updated_at` is older than
  the one already stored) is implemented to mirror the design doc's
  recommendation. Disable with `--no-stale-check` if it gets in the way.
- This server does NOT pull poster / cover / subtitle bytes from the staging
  host the way a real business server would; it just records the URLs. That
  keeps the demo dependency-free.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("demo-business")


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# slug -> drama payload (latest)
_dramas: dict[str, dict[str, Any]] = {}
# (slug, ep_number) -> episode payload (latest)
_episodes: dict[tuple[str, int], dict[str, Any]] = {}
# audit log of every request, newest first
_events: list[dict[str, Any]] = []
_EVENTS_CAP = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_event(kind: str, summary: str, payload: Any | None = None) -> None:
    _events.insert(0, {
        "ts": _now_iso(),
        "kind": kind,
        "summary": summary,
        "payload": payload,
    })
    del _events[_EVENTS_CAP:]


# ---------------------------------------------------------------------------
# App + auth
# ---------------------------------------------------------------------------

app = FastAPI(title="Demo Business Server")


class Config:
    api_key: str = "demo-secret-key"
    stale_check: bool = True


def _require_api_key(x_api_key: str | None) -> None:
    if x_api_key != Config.api_key:
        raise HTTPException(status_code=401, detail="invalid X-API-Key")


def _pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Sync endpoints
# ---------------------------------------------------------------------------


@app.post("/sync/dramas")
async def upsert_drama(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JSONResponse:
    _require_api_key(x_api_key)
    slug = payload.get("slug")
    if not isinstance(slug, str) or not slug:
        raise HTTPException(400, detail="payload.slug is required")

    incoming_updated_at = payload.get("client_updated_at")
    if Config.stale_check and slug in _dramas:
        prev = _dramas[slug].get("client_updated_at")
        if (
            isinstance(prev, str) and isinstance(incoming_updated_at, str)
            and incoming_updated_at < prev
        ):
            log.warning(
                "stale drama payload: slug=%s incoming=%s stored=%s",
                slug, incoming_updated_at, prev,
            )
            raise HTTPException(
                409,
                detail={
                    "error": "stale client_updated_at",
                    "stored": prev,
                    "incoming": incoming_updated_at,
                },
            )

    print("\n" + "=" * 72)
    print(f"[POST /sync/dramas] slug={slug}")
    print("=" * 72)
    print(_pretty(payload))

    _dramas[slug] = payload
    _record_event(
        "drama-upsert",
        f"slug={slug} translations={list(payload.get('translations', {}).keys())} "
        f"tags={[t.get('slug') for t in payload.get('tags', [])]} "
        f"actors={[a.get('slug') for a in payload.get('actors', [])]}",
        payload,
    )
    synced_at = _now_iso()
    log.info("drama upserted slug=%s synced_at=%s", slug, synced_at)
    return JSONResponse(
        {"ok": True, "client_updated_at": incoming_updated_at, "synced_at": synced_at}
    )


@app.delete("/sync/dramas/{slug}")
async def delete_drama(
    slug: str = Path(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    _require_api_key(x_api_key)
    print("\n" + "=" * 72)
    print(f"[DELETE /sync/dramas/{slug}]")
    print("=" * 72)
    drama_existed = _dramas.pop(slug, None) is not None
    removed_eps = [k for k in list(_episodes.keys()) if k[0] == slug]
    for k in removed_eps:
        _episodes.pop(k, None)
    _record_event(
        "drama-delete",
        f"slug={slug} drama_existed={drama_existed} cascaded_eps={len(removed_eps)}",
    )
    log.info(
        "drama deleted slug=%s existed=%s cascaded_eps=%d",
        slug, drama_existed, len(removed_eps),
    )
    # 204 — the HLS sync_client treats any 2xx as success.
    return Response(status_code=204)


@app.post("/sync/episodes")
async def upsert_episode(
    payload: dict[str, Any] = Body(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JSONResponse:
    _require_api_key(x_api_key)
    slug = payload.get("drama_slug")
    ep_number = payload.get("ep_number")
    if not isinstance(slug, str) or not slug:
        raise HTTPException(400, detail="payload.drama_slug is required")
    if not isinstance(ep_number, int):
        raise HTTPException(400, detail="payload.ep_number must be int")

    if slug not in _dramas:
        raise HTTPException(
            409,
            detail={
                "error": "drama not synced",
                "drama_slug": slug,
                "hint": "POST /sync/dramas with this slug first",
            },
        )

    incoming_updated_at = payload.get("client_updated_at")
    key = (slug, ep_number)
    if Config.stale_check and key in _episodes:
        prev = _episodes[key].get("client_updated_at")
        if (
            isinstance(prev, str) and isinstance(incoming_updated_at, str)
            and incoming_updated_at < prev
        ):
            log.warning(
                "stale episode payload: %s/%s incoming=%s stored=%s",
                slug, ep_number, incoming_updated_at, prev,
            )
            raise HTTPException(
                409,
                detail={
                    "error": "stale client_updated_at",
                    "stored": prev,
                    "incoming": incoming_updated_at,
                },
            )

    # Don't dump the whole m3u8 text to stdout; show a structured summary.
    summary = {k: v for k, v in payload.items() if k != "playlists"}
    summary["playlists"] = {
        ladder: {
            "bytes": len(text or ""),
            "lines": len((text or "").splitlines()),
            "preview": (text or "").splitlines()[:6],
        }
        for ladder, text in (payload.get("playlists") or {}).items()
    }
    print("\n" + "=" * 72)
    print(f"[POST /sync/episodes] slug={slug} ep={ep_number}")
    print("=" * 72)
    print(_pretty(summary))

    _episodes[key] = payload
    _record_event(
        "episode-upsert",
        f"slug={slug} ep={ep_number} ladders={list((payload.get('playlists') or {}).keys())} "
        f"subs={[s.get('lang_code') for s in payload.get('subtitles', [])]}",
        summary,
    )
    synced_at = _now_iso()
    log.info(
        "episode upserted slug=%s ep=%s synced_at=%s",
        slug, ep_number, synced_at,
    )
    return JSONResponse(
        {"ok": True, "client_updated_at": incoming_updated_at, "synced_at": synced_at}
    )


@app.delete("/sync/episodes/{slug}/{ep_number}")
async def delete_episode(
    slug: str = Path(...),
    ep_number: int = Path(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    _require_api_key(x_api_key)
    print("\n" + "=" * 72)
    print(f"[DELETE /sync/episodes/{slug}/{ep_number}]")
    print("=" * 72)
    existed = _episodes.pop((slug, ep_number), None) is not None
    _record_event(
        "episode-delete",
        f"slug={slug} ep={ep_number} existed={existed}",
    )
    log.info(
        "episode deleted slug=%s ep=%s existed=%s", slug, ep_number, existed,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Inspection: dashboard + raw state
# ---------------------------------------------------------------------------


@app.get("/_state")
async def get_state() -> dict[str, Any]:
    return {
        "dramas": list(_dramas.values()),
        "episodes": [
            {"_key": list(k), **v} for k, v in sorted(_episodes.items())
        ],
        "events": _events,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    rows_dramas = "".join(
        f"<tr><td>{d['slug']}</td><td>{d.get('default_lang', '')}</td>"
        f"<td>{', '.join((d.get('translations') or {}).keys())}</td>"
        f"<td>{len(d.get('tags') or [])}</td>"
        f"<td>{len(d.get('actors') or [])}</td>"
        f"<td>{d.get('client_updated_at', '')}</td></tr>"
        for d in _dramas.values()
    ) or '<tr><td colspan="6" style="color:#888">— 暂无 —</td></tr>'

    rows_eps = "".join(
        f"<tr><td>{ep['drama_slug']}</td><td>{ep['ep_number']}</td>"
        f"<td>{ep.get('duration_ms', '')}</td>"
        f"<td>{ep.get('width', '')}×{ep.get('height', '')}</td>"
        f"<td>{', '.join((ep.get('playlists') or {}).keys())}</td>"
        f"<td>{len(ep.get('subtitles') or [])}</td>"
        f"<td>{ep.get('client_updated_at', '')}</td></tr>"
        for (_, ep) in sorted(_episodes.items())
    ) or '<tr><td colspan="7" style="color:#888">— 暂无 —</td></tr>'

    rows_events = "".join(
        f"<tr><td>{e['ts']}</td><td>{e['kind']}</td><td>{e['summary']}</td></tr>"
        for e in _events[:50]
    ) or '<tr><td colspan="3" style="color:#888">— 暂无 —</td></tr>'

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Demo Business Server</title>
<meta http-equiv="refresh" content="5">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  h2 {{ font-size: 14px; margin: 18px 0 6px; color: #555; }}
  table {{ border-collapse: collapse; font-size: 12px; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  .meta {{ color: #888; font-size: 12px; }}
  code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>Demo Business Server <span class="meta">— 5s 自动刷新</span></h1>
<div class="meta">
  当前 API Key: <code>{Config.api_key}</code> &nbsp;|&nbsp;
  Stale 检查: <code>{'on' if Config.stale_check else 'off'}</code> &nbsp;|&nbsp;
  原始状态: <a href="/_state">/_state</a>
</div>

<h2>Dramas ({len(_dramas)})</h2>
<table>
  <tr><th>slug</th><th>default_lang</th><th>translations</th><th>tags</th><th>actors</th><th>client_updated_at</th></tr>
  {rows_dramas}
</table>

<h2>Episodes ({len(_episodes)})</h2>
<table>
  <tr><th>slug</th><th>ep</th><th>duration_ms</th><th>WxH</th><th>ladders</th><th>subs</th><th>client_updated_at</th></tr>
  {rows_eps}
</table>

<h2>Events (latest 50)</h2>
<table>
  <tr><th style="width:200px">ts</th><th style="width:140px">kind</th><th>summary</th></tr>
  {rows_events}
</table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="Demo HLS-sync business server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument(
        "--api-key", default="demo-secret-key",
        help="value the HLS server must send as X-API-Key (must match BUSINESS_SYNC_API_KEY)",
    )
    p.add_argument(
        "--no-stale-check", action="store_true",
        help="disable the 409-on-older-client_updated_at guard (default: on)",
    )
    args = p.parse_args()

    Config.api_key = args.api_key
    Config.stale_check = not args.no_stale_check

    log.info(
        "demo-business-server starting on %s:%d (api_key=%s stale_check=%s)",
        args.host, args.port, Config.api_key, Config.stale_check,
    )
    log.info(
        "Set BUSINESS_SYNC_BASE_URL=http://%s:%d and BUSINESS_SYNC_API_KEY=%s "
        "on the HLS staging server to point at this demo.",
        "127.0.0.1" if args.host == "0.0.0.0" else args.host,
        args.port, Config.api_key,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
