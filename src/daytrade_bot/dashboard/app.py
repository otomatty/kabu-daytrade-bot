"""ローカルで動くダッシュボード(FastAPI + WebSocket)。

安全のための決まり:
- 既定では 127.0.0.1 だけで待ち受ける。Host ヘッダが許可した名前でなければ拒否する
  (DNS リバインディングで外部のサイトから操作されるのを防ぐ)
- API はトークン(起動時にコンソールに表示される URL の ``#token=...``)が必要。
  ブラウザは別のサイトから独自ヘッダ付きのリクエストを送れないので、CSRF も防げる
- WebSocket は Origin が同じホストであることも確認する
- 緊急停止・全決済などの操作は ``confirm: true`` が無ければ受け付けない
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from daytrade_bot import __version__
from daytrade_bot.core.engine import Command, Engine, bar_json, fill_json
from daytrade_bot.core.events import EventBus
from daytrade_bot.storage.db import Repository, iso

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-DTBot-Token"  # noqa: S105 - ヘッダの名前
DANGEROUS = frozenset({Command.HALT, Command.FLATTEN, Command.CLEAR_HALT, Command.STOP})
STATIC_DIR = resources.files("daytrade_bot.dashboard") / "static"


class ControlRequest(BaseModel):
    confirm: bool = False
    reason: str = Field(default="ダッシュボードからの操作", max_length=200)


def _host_of(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def create_app(  # noqa: PLR0915 - ルートの定義をまとめて置く
    *,
    repo: Repository | None,
    token: str,
    engine: Engine | None = None,
    bus: EventBus | None = None,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]"),
) -> FastAPI:
    if len(token) < 16:
        raise ValueError("ダッシュボードのトークンは16文字以上にしてください")
    allowed = {h.lower() for h in allowed_hosts}
    event_bus = bus or (engine.bus if engine is not None else None)
    app = FastAPI(title="dtbot ダッシュボード", version=__version__, docs_url=None, redoc_url=None)

    def check_token(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="トークンが正しくありません")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if _host_of(request.headers.get("host", "")) not in allowed:
            return JSONResponse({"detail": "許可されていないホストです"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        # スクリプトは自分のファイルだけを許す。スタイルは、チャートのライブラリが
        # 帰属表示のロゴ用に <style> を差し込むため、インラインも許す(データはHTMLとして扱わない)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "mode": engine.mode if engine else "viewer"}

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        check_token(request)
        if engine is None:
            return {"mode": "viewer", "version": __version__}
        return {**engine.snapshot(), "version": __version__}

    @app.get("/api/bars")
    async def bars(
        request: Request, symbol: str = Query(pattern=r"^[0-9A-Z]{4,5}$")
    ) -> dict[str, Any]:
        check_token(request)
        if engine is None or symbol not in engine.bars_history:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        history = [bar_json(b) for b in engine.bars_history[symbol]]
        current = engine.builders[symbol].current
        fills = [fill_json(f) for f in engine.fills_today if f.symbol == symbol]
        return {
            "symbol": symbol,
            "bars": history,
            "current": bar_json(current) if current else None,
            "fills": fills,
        }

    @app.post("/api/control/{command}")
    async def control(request: Request, command: str, body: ControlRequest) -> dict[str, Any]:
        check_token(request)
        if engine is None:
            raise HTTPException(status_code=409, detail="売買エンジンが動いていません(閲覧モード)")
        try:
            cmd = Command(command)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="不明な操作です") from exc
        if cmd in DANGEROUS and not body.confirm:
            raise HTTPException(status_code=400, detail="確認(confirm: true)が必要な操作です")
        log.warning("ダッシュボードからの操作: %s(%s)", cmd.value, body.reason)
        message = await engine.command(cmd, body.reason)
        return {"ok": True, "message": message, "state": engine.snapshot()}

    @app.get("/api/backtests")
    async def backtests(request: Request) -> list[dict[str, Any]]:
        check_token(request)
        return repo.list_backtests() if repo is not None else []

    @app.get("/api/backtests/{run_id}")
    async def backtest(request: Request, run_id: str) -> dict[str, Any]:
        check_token(request)
        record = repo.get_backtest(run_id) if repo is not None else None
        if record is None:
            raise HTTPException(status_code=404, detail="バックテストが見つかりません")
        return {
            "run_id": record.run_id,
            "created_at": iso(record.created_at),
            "strategy": record.strategy,
            "params": record.params,
            "start_date": record.start_date.isoformat(),
            "end_date": record.end_date.isoformat(),
            "symbols": record.symbols,
            "summary": record.summary,
            "equity": record.equity,
            "trades": record.trades,
            "daily": record.daily,
        }

    @app.get("/api/events")
    async def events(
        request: Request, limit: int = Query(default=200, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        check_token(request)
        if repo is None:
            return []
        return [
            {
                "time": iso(e.time),
                "level": e.level,
                "kind": e.kind,
                "message": e.message,
                "run_id": e.run_id,
            }
            for e in repo.recent_events(limit)
        ]

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token_q: str = Query(default="", alias="token")) -> None:
        host_header = websocket.headers.get("host", "").lower()
        origin = websocket.headers.get("origin")
        # Origin はポートまで含めて一致させる(同じパソコンの別のポートのページからの接続も拒否する)
        same_origin = origin is None or urlsplit(origin).netloc.lower() == host_header
        if (
            _host_of(host_header) not in allowed
            or not same_origin
            or not hmac.compare_digest(token_q, token)
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if engine is None or event_bus is None:
            await websocket.send_json({"type": "hello", "mode": "viewer"})
            await websocket.close()
            return
        sub = event_bus.subscribe(maxsize=2000)
        receiver = asyncio.create_task(_drain_client(websocket))
        try:
            await websocket.send_json({"type": "snapshot", "snapshot": engine.snapshot()})
            while not receiver.done():
                getter = asyncio.create_task(sub.get())
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter not in done:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
                    break
                await websocket.send_json(getter.result())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sub.close()
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await receiver

    return app


async def _drain_client(websocket: WebSocket) -> None:
    """クライアントからの受信を読み捨てる(切断を検知するため)。"""
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await websocket.receive_text()
