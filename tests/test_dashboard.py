"""ダッシュボードの API・安全のための決まり・WebSocket・画面(実際のブラウザ)を検証する。"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from daytrade_bot.core.engine import Engine
from daytrade_bot.dashboard.app import _host_of, create_app
from daytrade_bot.storage.db import Repository
from tests.conftest import TRADING_DAY, at, make_config
from tests.helpers import build_engine, orb_day, replay, ticks_from
from tests.test_kabu import free_port

TOKEN = "test-token-0123456789"
CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@dataclass
class Dash:
    base: str
    port: int
    engine: Engine | None
    repo: Repository

    def client(self, token: str | None = TOKEN, host: str | None = None) -> httpx.AsyncClient:
        headers = {}
        if token is not None:
            headers["X-DTBot-Token"] = token
        if host is not None:
            headers["Host"] = host
        return httpx.AsyncClient(base_url=self.base, headers=headers, trust_env=False)


async def _serve(app, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            ws="websockets-sansio",
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    return server, task


@pytest.fixture
async def dash(tmp_path: Path) -> AsyncIterator[Dash]:
    repo = Repository(tmp_path / "d.sqlite3")
    cfg = make_config(universe=[{"symbol": "7203", "name": "トヨタ自動車"}])
    engine, clock, _ = build_engine(cfg, mode="paper", repo=repo)
    await replay(engine, clock, ticks_from(orb_day()), until=at(9, 40))
    port = free_port()
    app = create_app(repo=repo, token=TOKEN, engine=engine)
    server, task = await _serve(app, port)
    yield Dash(f"http://127.0.0.1:{port}", port, engine, repo)
    server.should_exit = True
    await asyncio.wait_for(task, 5)
    repo.close()


async def test_index_and_security_headers(dash: Dash) -> None:
    async with dash.client(token=None) as c:
        r = await c.get("/")
        assert r.status_code == 200 and "dtbot ダッシュボード" in r.text
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"
        assert (await c.get("/static/app.js")).status_code == 200
        assert (
            await c.get("/static/vendor/lightweight-charts.standalone.production.js")
        ).status_code == 200
        health = (await c.get("/api/health")).json()
        assert health["ok"] and health["mode"] == "paper"


async def test_token_required(dash: Dash) -> None:
    async with dash.client(token=None) as c:
        assert (await c.get("/api/state")).status_code == 401
    async with dash.client(token="wrong-token-000000000") as c:
        assert (await c.get("/api/state")).status_code == 401
    async with dash.client() as c:
        r = await c.get("/api/state")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        state = r.json()
        assert state["mode"] == "paper" and state["pnl"]["trades"] == 1


async def test_rejects_foreign_host(dash: Dash) -> None:
    """DNS リバインディング(外部のサイトの名前で localhost に届く)を拒否する。"""
    async with dash.client(host="evil.example.com") as c:
        assert (await c.get("/api/state")).status_code == 400
        assert (await c.get("/")).status_code == 400
    async with dash.client(host=f"localhost:{dash.port}") as c:
        assert (await c.get("/api/state")).status_code == 200


async def test_controls_need_confirmation(dash: Dash) -> None:
    async with dash.client() as c:
        r = await c.post("/api/control/halt", json={})
        assert r.status_code == 400 and "確認" in r.json()["detail"]
        assert (await c.post("/api/control/bogus", json={"confirm": True})).status_code == 404
        r = await c.post("/api/control/pause", json={})
        assert r.json()["state"]["risk"]["state"] == "paused"
        r = await c.post("/api/control/halt", json={"confirm": True, "reason": "テスト"})
        body = r.json()
        assert body["ok"] and body["state"]["risk"]["state"] == "halted"
        assert "テスト" in body["state"]["risk"]["reason"]
        r = await c.post("/api/control/pause", json={"reason": "x" * 201})
        assert r.status_code == 422


async def test_bars_backtests_events(dash: Dash) -> None:
    from daytrade_bot.backtest.runner import BacktestRunner

    dash.repo.save_bars(orb_day())
    cfg = make_config(universe=[{"symbol": "7203"}])
    result = await BacktestRunner(cfg, dash.repo, TRADING_DAY, TRADING_DAY).run()
    dash.repo.save_backtest(result.to_record(at(16)))
    async with dash.client() as c:
        bars = (await c.get("/api/bars", params={"symbol": "7203"})).json()
        assert bars["bars"] and bars["fills"] and bars["symbol"] == "7203"
        assert (await c.get("/api/bars", params={"symbol": "6758"})).status_code == 404
        assert (await c.get("/api/bars", params={"symbol": "../x"})).status_code == 422
        runs = (await c.get("/api/backtests")).json()
        assert runs[0]["run_id"] == result.run_id
        detail = (await c.get(f"/api/backtests/{result.run_id}")).json()
        assert detail["summary"]["trades"] == 1 and detail["trades"]
        assert (await c.get("/api/backtests/none")).status_code == 404
        events = (await c.get("/api/events", params={"limit": 5})).json()
        assert 0 < len(events) <= 5


async def test_websocket_auth_and_stream(dash: Dash) -> None:
    url = f"ws://127.0.0.1:{dash.port}/ws"
    # トークンが違う・無い、または別のサイトのページから開かれた接続は、握手の段階で拒否する
    for bad, origin in (
        (f"{url}?token=wrong-token-000000000", None),
        (url, None),
        (f"{url}?token={TOKEN}", "http://evil.example.com"),
        (f"{url}?token={TOKEN}", "http://127.0.0.1:1"),  # 同じパソコンの別のポートのページ
    ):
        with pytest.raises(InvalidStatus) as info:
            async with connect(bad, origin=origin):  # type: ignore[arg-type]
                pass
        assert info.value.response.status_code == 403
    async with connect(f"{url}?token={TOKEN}", origin=f"http://127.0.0.1:{dash.port}") as ws:  # type: ignore[arg-type]
        first = json.loads(await ws.recv())
        assert first["type"] == "snapshot" and first["snapshot"]["mode"] == "paper"
        assert dash.engine is not None
        dash.engine.log("warning", "test", "テストのイベント")
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if msg["type"] == "event":
                assert msg["event"]["message"] == "テストのイベント"
                break
    await asyncio.sleep(0.1)
    assert dash.engine.bus.subscriber_count == 0  # 切断したら購読をやめる


async def test_viewer_mode(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "v.sqlite3")
    port = free_port()
    server, task = await _serve(create_app(repo=repo, token=TOKEN), port)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"X-DTBot-Token": TOKEN}, trust_env=False
        ) as c:
            assert (await c.get("/api/state")).json()["mode"] == "viewer"
            assert (await c.post("/api/control/pause", json={})).status_code == 409
            assert (await c.get("/api/bars", params={"symbol": "7203"})).status_code == 404
            assert (await c.get("/api/backtests")).json() == []
        async with connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}") as ws:
            assert json.loads(await ws.recv()) == {"type": "hello", "mode": "viewer"}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        repo.close()


async def test_no_repo_endpoints() -> None:
    app = create_app(repo=None, token=TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers={"X-DTBot-Token": TOKEN}
    ) as c:
        assert (await c.get("/api/events")).json() == []
        assert (await c.get("/api/backtests")).json() == []
        assert (await c.get("/api/backtests/x")).status_code == 404


def test_token_length_and_host_parsing() -> None:
    with pytest.raises(ValueError, match="16文字"):
        create_app(repo=None, token="short")
    assert _host_of("127.0.0.1:8765") == "127.0.0.1"
    assert _host_of("[::1]:8765") == "[::1]"
    assert _host_of("LOCALHOST") == "localhost"
    assert _host_of("[::1") == "[::1"


@pytest.mark.e2e
@pytest.mark.skipif(CHROME is None, reason="Chrome がありません")
async def test_browser_end_to_end(dash: Dash) -> None:
    """実際のブラウザで画面を開き、表示と操作(緊急停止と解除)を確かめる。"""
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto(f"{dash.base}/#token={TOKEN}")
        await page.wait_for_selector("#btn-halt:not([hidden])")
        assert "#token" not in page.url  # トークンはアドレス欄から消す
        assert await page.text_content("#mode") == "ペーパー"
        assert "円" in (await page.text_content("#pnl-net") or "")
        assert await page.locator("#trips tbody tr").count() == 1
        assert await page.locator("#symbols button").count() == 1
        assert await page.is_hidden("#view-backtest")
        await page.click("#btn-halt")
        await page.click("#confirm-ok")
        await page.wait_for_selector('#statebar[data-state="halted"]')
        await page.click("#btn-clear")
        await page.click("#confirm-ok")
        await page.wait_for_selector('#statebar[data-state="paused"]')
        await page.click("#tab-backtest")
        await page.wait_for_selector("#runs-empty:not([hidden])")
        # トークンが無いときは、開き方を案内する
        page2 = await browser.new_page()
        await page2.goto(f"{dash.base}/")
        await page2.wait_for_selector("#notice:not([hidden])")
        assert "#token=" in (await page2.text_content("#notice") or "")
        await browser.close()
    assert errors == []
