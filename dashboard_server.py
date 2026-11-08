"""
dashboard_server.py

A separate, read-only HTTP + WebSocket server that renders the live state of
the trading bot. It runs beside `python app.py` (or `python bot.py --run`) and
never starts, stops, or trades anything.

    python dashboard_server.py            # http://127.0.0.1:8765
    python dashboard_server.py --port 9000

Endpoints
    GET  /                    dashboard UI
    GET  /api/overview        account + positions + attention items
    GET  /api/account         equity, cash, daily P&L, drawdown
    GET  /api/positions       open positions with stops/targets
    GET  /api/trades          journal summary + recent trades
    GET  /api/analytics       equity curve, per-symbol attribution, R buckets
    GET  /api/scan            live watchlist scan (prices, regimes, signals)
    GET  /api/optimizer       tuned params + walk-forward performance
    GET  /api/workflow        pipeline topology + per-stage live status
    GET  /api/liveness        heartbeat, scan counts, duplicate-process check
    GET  /api/logs?after=N    incremental log tail (byte cursor)
    GET  /api/config          non-secret bot configuration
    GET  /healthz             liveness probe
    WS   /ws                  push feed (overview every 2s, workflow every 10s)
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import dashboard.data_fetcher as df  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Dashboard")

UI_PATH = BASE_DIR / "dashboard_ui.html"


def _clean(obj: Any) -> Any:
    """Recursively replace NaN/Infinity with None.

    Indicator warm-up windows and empty trade sets produce NaN. Neither
    `JSON.parse` nor Starlette's encoder accepts it, so a single NaN would
    blank an entire panel. Converting at the boundary keeps every other
    number in the payload intact.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


class SafeJSONResponse(JSONResponse):
    """JSON response that degrades NaN/Infinity to null instead of failing."""

    def render(self, content: Any) -> bytes:
        return super().render(_clean(content))

# How often each payload is refreshed. The bot scans every 60s; anything
# faster than this is noise, and Alpaca rate limits are finite.
PUSH_INTERVAL = 2.0
WORKFLOW_INTERVAL = 10.0


# --------------------------------------------------------------------------
# WebSocket fan-out
# --------------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        logger.info("client connected (%d total)", len(self.clients))

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)
        logger.info("client disconnected (%d total)", len(self.clients))

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        if not self.clients:
            return
        text = json.dumps(_clean(payload), default=str)
        dead: List[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.leave(ws)


hub = Hub()


def _safe(fn, label: str, *args: Any, **kwargs: Any) -> Any:
    """Run a data-source callable without letting it break the caller.

    Extra arguments are forwarded to `fn`. They must not be captured by a
    `default` parameter here -- a call like `_safe(get_recent_trades,
    "recent_trades", 20)` means "limit 20", not "return 20 on failure".
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("%s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"}


async def overview_loop() -> None:
    """Push the fast-moving data (account, positions, alerts)."""
    while True:
        try:
            if hub.clients:
                payload = await asyncio.to_thread(df.fetch_overview)
                payload["type"] = "overview"
                await hub.broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("overview loop error: %s", e)
        await asyncio.sleep(PUSH_INTERVAL)


async def workflow_loop() -> None:
    """Push the workflow diagram + optimizer state on a slower cadence."""
    while True:
        try:
            if hub.clients:
                workflow = await asyncio.to_thread(_safe, df.get_workflow, "workflow")
                optimizer = await asyncio.to_thread(_safe, df.get_optimizer_state, "optimizer")
                summary = await asyncio.to_thread(_safe, df.get_trade_summary, "trade_summary")
                analytics = await asyncio.to_thread(_safe, df.get_analytics, "analytics")
                await hub.broadcast({
                    "type": "slow",
                    "ts": payload_ts(),
                    "workflow": workflow,
                    "optimizer": optimizer,
                    "trade_summary": summary,
                    "analytics": analytics,
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("workflow loop error: %s", e)
        await asyncio.sleep(WORKFLOW_INTERVAL)


def payload_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Warm up data fetcher in background so the first UI request is instant
    asyncio.create_task(asyncio.to_thread(_safe, df.fetch_overview, "initial_overview_warmup"))
    asyncio.create_task(asyncio.to_thread(_safe, df.get_workflow, "initial_workflow_warmup"))
    tasks = [asyncio.create_task(overview_loop()), asyncio.create_task(workflow_loop())]
    logger.info("dashboard ready")
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class LoopbackHostMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        server = scope.get("server")
        host_headers = [value for name, value in scope.get("headers", ()) if name.lower() == b"host"]
        host = None
        if len(host_headers) == 1 and server and server[1] is not None:
            try:
                parsed = urlsplit("//" + host_headers[0].decode("ascii"))
                if (parsed.username is None and parsed.password is None and not parsed.path
                        and not parsed.query and not parsed.fragment and parsed.port == server[1]):
                    host = parsed.hostname
            except (UnicodeDecodeError, ValueError):
                pass

        if host is None or not _is_loopback_host(host):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                body = b"Invalid Host header"
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode("ascii"))],
                })
                await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


app = FastAPI(
    title="Trading Bot Dashboard",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
)
app.add_middleware(LoopbackHostMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://{host}:{port}"
        for host in ("127.0.0.1", "localhost")
        for port in (8765, 8766)
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not UI_PATH.exists():
        return HTMLResponse(
            "<h1>dashboard_ui.html not found</h1>"
            f"<p>Expected at {UI_PATH}</p>",
            status_code=500,
        )
    return HTMLResponse(
        UI_PATH.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------

@app.get("/api/profiles")
async def api_profiles():
    secondary_available = os.getenv("BOT_SECONDARY_AVAILABLE", "false").lower() == "true"
    return {
        "profiles": [
            {"id": "primary", "label": "Primary", "origin": "http://127.0.0.1:8765", "available": True},
            {"id": "secondary", "label": "Secondary", "origin": "http://127.0.0.1:8766", "available": secondary_available},
        ]
    }


@app.get("/api/overview")
async def api_overview():
    return await asyncio.to_thread(df.fetch_overview)


@app.get("/api/account")
async def api_account():
    return await asyncio.to_thread(_safe, df.get_account, "account")


@app.get("/api/positions")
async def api_positions():
    return await asyncio.to_thread(_safe, df.get_positions, "positions")


@app.get("/api/trades")
async def api_trades(limit: int = Query(default=40, ge=1, le=200)):
    return {
        "summary": await asyncio.to_thread(_safe, df.get_trade_summary, "trade_summary"),
        "recent": await asyncio.to_thread(_safe, df.get_recent_trades, "recent_trades", limit),
    }


@app.get("/api/analytics")
async def api_analytics():
    return await asyncio.to_thread(_safe, df.get_analytics, "analytics")


@app.get("/api/portfolio-history")
async def api_portfolio_history(period: str = "1M", timeframe: str = "1D"):
    try:
        df.validate_portfolio_history_request(period, timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await asyncio.to_thread(
        _safe, df.get_portfolio_history, "portfolio_history", period, timeframe
    )


@app.get("/api/scan")
async def api_scan(force: bool = False):
    return await asyncio.to_thread(_safe, lambda: df.get_scan_snapshot(force=force), "scan")


@app.get("/api/optimizer")
async def api_optimizer():
    return await asyncio.to_thread(_safe, df.get_optimizer_state, "optimizer")


@app.get("/api/workflow")
async def api_workflow():
    return await asyncio.to_thread(_safe, df.get_workflow, "workflow")


@app.get("/api/liveness")
async def api_liveness():
    return await asyncio.to_thread(_safe, df.get_liveness, "liveness")


@app.get("/api/logs")
async def api_logs(after: int = Query(default=0, ge=0)):
    return await asyncio.to_thread(_safe, lambda: df.get_logs(after=after), "logs")


@app.get("/api/config")
async def api_config():
    return await asyncio.to_thread(_safe, df.get_config, "config")


@app.get("/healthz")
async def healthz():
    ok = df.orchestrator_status()
    return JSONResponse(
        {"status": "ok" if ok["ready"] else "degraded", "broker": ok},
        status_code=200 if ok["ready"] else 503,
    )


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin")
    allowed_origins = {
        f"http://{host}:{port}"
        for host in ("127.0.0.1", "localhost")
        for port in (8765, 8766)
    }
    if origin and origin not in allowed_origins:
        await ws.close(code=1008)
        return
    await hub.join(ws)
    try:
        # Immediate snapshot so the UI is never empty on first load
        payload = await asyncio.to_thread(df.fetch_overview)
        payload["type"] = "overview"
        await ws.send_text(json.dumps(_clean(payload), default=str))

        slow = {
            "type": "slow",
            "ts": payload_ts(),
            "workflow": await asyncio.to_thread(_safe, df.get_workflow, "workflow"),
            "optimizer": await asyncio.to_thread(_safe, df.get_optimizer_state, "optimizer"),
            "trade_summary": await asyncio.to_thread(_safe, df.get_trade_summary, "trade_summary"),
            "analytics": await asyncio.to_thread(_safe, df.get_analytics, "analytics"),
        }
        await ws.send_text(json.dumps(_clean(slow), default=str))

        while True:
            # Drain client pings; disconnect surfaces as WebSocketDisconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.leave(ws)
    except Exception as e:
        logger.info("websocket closed: %s", e)
        await hub.leave(ws)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only trading bot dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        parser.error("dashboard host must be a loopback address")

    import uvicorn
    logger.info("Dashboard on http://%s:%d  (bot UI unchanged on :7860)", args.host, args.port)
    uvicorn.run(
        "dashboard_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
