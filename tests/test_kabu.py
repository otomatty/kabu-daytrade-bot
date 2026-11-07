"""kabuステーションAPI のアダプタを、モックサーバー(実際の HTTP と WebSocket)で検証する。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
import uvicorn

from daytrade_bot.brokers.base import (
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderStateUnknown,
)
from daytrade_bot.brokers.kabu import codes
from daytrade_bot.brokers.kabu.broker import KabuBroker, parse_time
from daytrade_bot.brokers.kabu.client import (
    KabuApiError,
    KabuAuthError,
    KabuRestClient,
    RateLimiter,
)
from daytrade_bot.brokers.kabu.feed import KabuPushFeed, parse_push
from daytrade_bot.config import KabuConfig, TradingConfig
from daytrade_bot.core.clock import OffsetClock
from daytrade_bot.core.engine import Engine
from daytrade_bot.core.models import (
    Bar,
    BrokerOrderUpdate,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from daytrade_bot.core.strategy import Strategy, StrategyContext, StrategyParams
from daytrade_bot.devtools.mock_kabu import MockKabuStation
from tests.conftest import D, at, make_config
from tests.kabu_spec import validate_request, validate_response

PASSWORD = "mock-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Server:
    mock: MockKabuStation
    port: int
    clock: OffsetClock

    @property
    def kabu(self) -> KabuConfig:
        return KabuConfig(
            port=self.port,
            poll_interval_sec=0.2,
            request_timeout_sec=1.0,
            order_rate_per_sec=10,
            info_rate_per_sec=10,
        )

    def client(self, **kw: Any) -> KabuRestClient:
        return KabuRestClient(self.kabu, kw.pop("password", PASSWORD), **kw)

    async def control(self, path: str, body: dict[str, Any] | None = None) -> None:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(f"http://127.0.0.1:{self.port}/__mock/{path}", json=body or {})
            r.raise_for_status()

    def check_spec(self) -> None:
        """モックが受け取ったすべてのリクエストが公式仕様どおりか。"""
        problems: list[str] = []
        for r in self.mock.requests:
            problems += validate_request(r["method"], r["path"], r["body"], r["query"])
        assert problems == []


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    clock = OffsetClock(at(10))
    mock = MockKabuStation(
        password=PASSWORD,
        clock=clock,
        prices={"7203": D(2000), "6758": D(1500)},
        tick_interval_sec=0.05,
    )
    port = free_port()
    config = uvicorn.Config(
        mock.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        ws="websockets-sansio",
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.01)
    yield Server(mock, port, clock)
    srv.should_exit = True
    srv.force_exit = True
    await asyncio.wait_for(task, 10)


async def wait_for(pred: Any, limit: float = 5.0) -> None:
    """条件が満たされるまで待つ(外部のサーバーの状態を見るので、短い間隔で確かめる)。"""
    async with asyncio.timeout(limit):
        while not pred():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def req(
    side: Side = Side.BUY,
    qty: int = 100,
    effect: PositionEffect = PositionEffect.OPEN,
    otype: OrderType = OrderType.MARKET,
    limit: str | None = None,
    symbol: str = "7203",
) -> OrderRequest:
    return OrderRequest(
        f"c-{side}-{effect}-{qty}",
        symbol,
        side,
        qty,
        otype,
        effect,
        at(10),
        limit_price=D(limit) if limit else None,
    )


# ---- REST クライアント ---------------------------------------------------------------------


class TestClient:
    async def test_auth_and_token_refresh(self, server: Server) -> None:
        c = server.client()
        assert not c.has_token
        limit = await c.soft_limit()  # 初回は自動でトークンを発行する
        assert limit["KabuSVersion"] == "mock-5.0.0"
        assert isinstance(limit["Stock"], Decimal)  # 数値は Decimal で読む
        await server.control("expire_token")
        assert (await c.wallet_margin())["MarginAccountWallet"] == Decimal("3000000.0")
        assert [r["path"] for r in server.mock.requests].count("/kabusapi/token") == 2
        await c.aclose()
        server.check_spec()

    async def test_wrong_password(self, server: Server) -> None:
        c = server.client(password="nope")
        with pytest.raises(KabuAuthError, match="APIパスワードが違います"):
            await c.soft_limit()
        await c.aclose()

    async def test_not_running(self) -> None:
        c = KabuRestClient(KabuConfig(port=free_port()), PASSWORD)
        with pytest.raises(KabuAuthError, match="起動しているか"):
            await c.authenticate()
        c._token = "x"
        with pytest.raises(BrokerUnavailable, match="接続できません"):
            await c.send_order({})
        await c.aclose()

    async def test_rate_limit_429_is_retried(self, server: Server) -> None:
        server.mock.rate_limit = 4
        c = server.client()
        results = await asyncio.gather(*(c.wallet_cash() for _ in range(8)))
        assert any(r["path"] == "/kabusapi/wallet/cash" for r in server.mock.requests)
        assert all(r["StockAccountWallet"] for r in results)
        await c.aclose()

    async def test_order_500_is_unknown_but_get_500_retries(self, server: Server) -> None:
        c = server.client()
        await c.authenticate()
        await server.control(
            "fail", {"path": "/kabusapi/sendorder", "mode": "status", "status": 500}
        )
        with pytest.raises(OrderStateUnknown, match="内部エラー"):
            await c.send_order({"Symbol": "7203"})
        await server.control(
            "fail", {"path": "/kabusapi/wallet/cash", "mode": "status", "status": 503}
        )
        assert (await c.wallet_cash())["StockAccountWallet"]
        await c.aclose()

    async def test_lost_response_after_processing_is_unknown(self, server: Server) -> None:
        c = KabuRestClient(KabuConfig(port=server.port, request_timeout_sec=0.3), PASSWORD)
        broker = KabuBroker(c, TradingConfig(), server.clock)
        await server.control(
            "fail",
            {"path": "/kabusapi/sendorder", "mode": "drop", "after_processing": True, "delay": 0.6},
        )
        with pytest.raises(OrderStateUnknown, match="途切れ"):
            await broker.place_order(req())
        # 注文はモック側では受け付けられている
        snaps = await broker.fetch_orders()
        assert len(snaps) == 1 and snaps[0].symbol == "7203" and snaps[0].qty == 100
        await c.aclose()

    async def test_get_timeouts_retry_then_fail(self, server: Server) -> None:
        c = KabuRestClient(
            KabuConfig(port=server.port, request_timeout_sec=0.2), PASSWORD, max_retries=1
        )
        await c.authenticate()
        await server.control(
            "fail", {"path": "/kabusapi/wallet/cash", "mode": "drop", "count": 2, "delay": 0.4}
        )
        with pytest.raises(BrokerError, match="応答がありません"):
            await c.wallet_cash()
        await c.aclose()

    async def test_api_errors(self, server: Server) -> None:
        c = server.client()
        with pytest.raises(OrderRejected, match="必須項目") as info:
            await c.send_order({"Symbol": "7203"})
        assert info.value.code == 4001012
        with pytest.raises(OrderRejected, match="該当注文なし"):
            await c.cancel_order("missing")
        with pytest.raises(ValueError, match="最大50"):
            await c.register([str(1000 + i) for i in range(51)], 1)
        with pytest.raises(KabuApiError):
            await c.request("GET", "/nope", category="info", idempotent=True)
        await c.aclose()

    def test_password_required_and_rate_limiter_validation(self) -> None:
        with pytest.raises(ValueError, match="APIパスワード"):
            KabuRestClient(KabuConfig(), "")
        with pytest.raises(ValueError, match="per_second"):
            RateLimiter(0)

    async def test_rate_limiter_spacing(self) -> None:
        limiter = RateLimiter(20)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(5):
            await limiter.acquire()
        assert loop.time() - t0 >= 0.19


# ---- ブローカー ---------------------------------------------------------------------------


async def start_broker(
    server: Server, trading: TradingConfig | None = None, **kw: Any
) -> tuple[KabuBroker, KabuRestClient, list[BrokerOrderUpdate]]:
    c = server.client()
    broker = KabuBroker(c, trading or TradingConfig(), server.clock, poll_interval_sec=0.1, **kw)
    updates: list[BrokerOrderUpdate] = []
    broker.set_update_handler(updates.append)
    await broker.start()
    return broker, c, updates


class TestBroker:
    async def test_margin_open_and_close(self, server: Server) -> None:
        broker, c, updates = await start_broker(server)
        oid = await broker.place_order(req())
        await wait_for(lambda: any(u.status is OrderStatus.FILLED for u in updates))
        filled = next(u for u in updates if u.status is OrderStatus.FILLED)
        assert filled.broker_order_id == oid and filled.filled_qty == 100
        (fill,) = filled.fills
        assert fill.effect is PositionEffect.OPEN and fill.side is Side.BUY and fill.qty == 100
        assert fill.price == server.mock.orders[oid].details[0]["Price"]
        (pos,) = await broker.fetch_positions()
        assert pos.symbol == "7203" and pos.qty == 100
        # 返済: 建玉IDを指定して、建玉の市場で返済する
        payload = await broker.build_payload(req(Side.SELL, effect=PositionEffect.CLOSE))
        assert payload["CashMargin"] == 3 and payload["DelivType"] == 2
        assert payload["ClosePositions"] == [{"HoldID": fill.fill_id.split(":")[1], "Qty": 100}]
        assert payload["Exchange"] == 27
        close_id = await broker.place_order(req(Side.SELL, effect=PositionEffect.CLOSE))
        await wait_for(
            lambda: any(
                u.broker_order_id == close_id and u.status is OrderStatus.FILLED for u in updates
            )
        )
        close_fill = next(u for u in updates if u.broker_order_id == close_id and u.fills).fills[0]
        assert close_fill.effect is PositionEffect.CLOSE
        assert await broker.fetch_positions() == []
        # 同じ約定が何度も通知されない
        await asyncio.sleep(0.3)
        assert sum(len(u.fills) for u in updates) == 2
        await broker.stop()
        await c.aclose()
        server.check_spec()

    async def test_close_without_position_is_rejected_locally(self, server: Server) -> None:
        broker, c, _ = await start_broker(server)
        with pytest.raises(OrderRejected, match="返済できるボットの建玉が足りません"):
            await broker.place_order(req(Side.SELL, effect=PositionEffect.CLOSE))
        await broker.stop()
        await c.aclose()

    async def test_limit_order_cancel_and_short(self, server: Server) -> None:
        broker, c, updates = await start_broker(server)
        oid = await broker.place_order(
            req(Side.SELL, otype=OrderType.LIMIT, limit="9000")
        )  # 約定しない売り指値(空売り)
        assert server.mock.orders[oid].body["Price"] == 9000.0
        await broker.cancel_order(oid)
        await wait_for(
            lambda: any(
                u.broker_order_id == oid and u.status is OrderStatus.CANCELED for u in updates
            )
        )
        with pytest.raises(OrderRejected, match="取消不可"):
            await broker.cancel_order(oid)
        await broker.stop()
        await c.aclose()
        server.check_spec()

    async def test_ignores_positions_from_previous_days(self, server: Server) -> None:
        from daytrade_bot.devtools.mock_kabu import _Position

        server.mock.positions.append(
            _Position("EOLD", "7203", "2", 300, 0, D(1900), 27, 1, "20261106")
        )
        broker, c, _ = await start_broker(server)
        assert await broker.fetch_positions() == []
        with pytest.raises(OrderRejected, match="ボットの建玉が足りません"):
            await broker.place_order(req(Side.SELL, effect=PositionEffect.CLOSE))
        await broker.stop()
        await c.aclose()

    async def test_cash_mode_excludes_preexisting_holdings(self, server: Server) -> None:
        server.mock.add_cash_holding("7203", 500, D(1800))  # 長期保有の株
        trading = TradingConfig(account_mode="cash", cash_fund_type="02")
        broker, c, updates = await start_broker(server, trading)
        assert await broker.fetch_positions() == []
        payload = await broker.build_payload(req())
        assert (
            payload["CashMargin"] == 1 and payload["FundType"] == "02" and payload["DelivType"] == 2
        )
        await broker.place_order(req())
        await wait_for(lambda: any(u.fills for u in updates))
        (pos,) = await broker.fetch_positions()
        assert pos.qty == 100
        sell = await broker.build_payload(req(Side.SELL, effect=PositionEffect.CLOSE))
        assert sell["FundType"] == "  " and sell["DelivType"] == 0
        await broker.place_order(req(Side.SELL, effect=PositionEffect.CLOSE))
        await wait_for(lambda: sum(len(u.fills) for u in updates) == 2)
        assert await broker.fetch_positions() == []
        with pytest.raises(OrderRejected, match="空売り"):
            await broker.build_payload(req(Side.SELL))
        with pytest.raises(OrderRejected, match="買い戻し"):
            await broker.build_payload(req(Side.BUY, effect=PositionEffect.CLOSE))
        await broker.stop()
        await c.aclose()
        server.check_spec()

    async def test_restart_does_not_replay_old_fills(self, server: Server) -> None:
        broker, c, updates = await start_broker(server)
        await broker.place_order(req())
        await wait_for(lambda: any(u.fills for u in updates))
        await broker.stop()
        broker2, c2, updates2 = await start_broker(server)
        await broker2.poll_once()
        assert updates2 == []
        assert broker2.preexisting_active_orders == []
        await broker2.stop()
        await c.aclose()
        await c2.aclose()

    def test_status_mapping(self) -> None:
        def o(
            state: int, cum: int, qty: int = 100, details: Sequence[dict[str, Any]] = ()
        ) -> dict[str, Any]:
            return {"State": state, "CumQty": cum, "OrderQty": qty, "Details": list(details)}

        assert codes.order_status(o(1, 0))[0] is OrderStatus.SUBMITTED
        assert codes.order_status(o(3, 40))[0] is OrderStatus.PARTIALLY_FILLED
        assert codes.order_status(o(4, 0))[0] is OrderStatus.PENDING_CANCEL
        assert codes.order_status(o(5, 100))[0] is OrderStatus.FILLED
        assert (
            codes.order_status(o(5, 0, details=[{"RecType": 4, "State": 4}]))[0]
            is OrderStatus.REJECTED
        )
        assert codes.order_status(o(5, 0, details=[{"RecType": 7}]))[0] is OrderStatus.EXPIRED
        assert codes.order_status(o(5, 30, details=[{"RecType": 6}])) == (
            OrderStatus.CANCELED,
            "取消済",
        )
        assert parse_time(None) is None and parse_time("bad") is None
        assert parse_time("2026-11-09T10:00:00") == at(10)


# ---- PUSH 配信 ----------------------------------------------------------------------------


class TestFeed:
    def test_parse_push_swaps_bid_ask_names(self) -> None:
        msg = json.dumps(
            {
                "Symbol": "7203",
                "Exchange": 1,
                "CurrentPrice": 2408.0,
                "CurrentPriceTime": "2026-11-09T10:00:00+09:00",
                "TradingVolume": 4571500.0,
                "BidPrice": 2408.5,
                "AskPrice": 2407.5,
            }
        )
        t = parse_push(msg, 1)
        assert t is not None and t.price == D("2408.0") and t.cum_volume == 4571500
        assert t.ask == D("2408.5") and t.bid == D("2407.5")  # Bid=最良売気配 → ask
        assert parse_push(json.dumps({"Symbol": "7203", "CurrentPrice": None}), 1) is None
        assert (
            parse_push(
                json.dumps(
                    {
                        "Symbol": "7203",
                        "Exchange": 3,
                        "CurrentPrice": 1,
                        "CurrentPriceTime": "2026-11-09T10:00:00+09:00",
                    }
                ),
                1,
            )
            is None
        )
        assert parse_push("[]", 1) is None

    async def test_receives_and_reconnects(self, server: Server) -> None:
        c = server.client()
        feed = KabuPushFeed(server.kabu.ws_url, c, server.clock, max_backoff_sec=0.2)
        ticks: list[Tick] = []
        feed.set_tick_handler(ticks.append)
        await feed.start(["7203", "6758"])
        assert server.mock.registered == ["7203", "6758"]
        await wait_for(lambda: {t.symbol for t in ticks} == {"7203", "6758"})
        assert feed.connected and feed.last_message_at is not None
        t = ticks[-1]
        assert t.ask is not None and t.bid is not None and t.ask > t.bid
        # 接続を切られても、つなぎ直して銘柄を登録し直す
        server.mock.registered.clear()
        await server.control("drop_ws")
        await wait_for(lambda: feed.reconnects >= 1 and server.mock.registered == ["7203", "6758"])
        n = len(ticks)
        await wait_for(lambda: len(ticks) > n)
        feed._on_message("not json")
        assert feed.parse_errors == 1
        await feed.stop()
        assert server.mock.registered == []
        assert not feed.connected
        await c.aclose()
        server.check_spec()


# ---- エンジンと組み合わせた結合テスト ---------------------------------------------------


class EnterOnFirstTick(Strategy[StrategyParams]):
    name = "enter_on_first_tick"
    Params = StrategyParams

    def __init__(self, symbols: Sequence[str]) -> None:
        super().__init__(symbols)
        self.sent = False

    def on_tick(self, tick: Tick, ctx: StrategyContext) -> Sequence[Signal]:
        if self.sent or not ctx.entries_allowed():
            return ()
        self.sent = True
        return (Signal(tick.symbol, SignalAction.ENTER_LONG, reason="テスト"),)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        return ()


async def test_engine_live_against_mock(server: Server) -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}],
        risk={"reconcile_interval_sec": 0.5},
        trading={"live_enabled": True},
    )
    c = server.client()
    broker = KabuBroker(c, cfg.trading, server.clock, poll_interval_sec=0.1)
    feed = KabuPushFeed(server.kabu.ws_url, c, server.clock)
    engine = Engine(
        config=cfg,
        mode="live",
        clock=server.clock,
        broker=broker,
        feed=feed,
        strategy=EnterOnFirstTick(cfg.symbols),
    )
    task = asyncio.create_task(engine.run())
    try:
        await wait_for(lambda: engine.portfolio.qty("7203") == 100, limit=10)
        assert len(server.mock.positions) == 1
        # 照合しても不一致にならない
        await asyncio.sleep(1.2)
        assert engine.risk.state.value == "active"
        result = await engine.command("flatten")
        assert "全決済" in result
        await wait_for(lambda: engine.portfolio.qty("7203") == 0, limit=10)
        assert server.mock.positions == []
        (trip,) = engine.portfolio.round_trips
        assert trip.qty == 100
        await engine.command("stop")
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await c.aclose()
    server.check_spec()


async def test_mock_responses_match_official_schema(server: Server) -> None:
    c = server.client()
    broker = KabuBroker(c, TradingConfig(), server.clock)
    await broker.place_order(req())
    await broker.place_order(req(Side.SELL, otype=OrderType.LIMIT, limit="9000", qty=200))
    await asyncio.sleep(0.1)
    async with httpx.AsyncClient(
        base_url=server.kabu.base_url,
        headers={"X-API-KEY": server.mock.token or ""},
        trust_env=False,
    ) as h:
        for method, path, params in [
            ("GET", "/orders", {"product": 0}),
            ("GET", "/positions", {"product": 2}),
            ("GET", "/board/7203@1", {}),
            ("GET", "/apisoftlimit", {}),
            ("GET", "/wallet/margin", {}),
            ("GET", "/wallet/cash", {}),
        ]:
            r = await h.request(method, path, params=params)
            assert r.status_code == 200
            assert validate_response(method, "/kabusapi" + path, r.json()) == []
    await c.aclose()


# ---- 独立したレビューで見つかった不具合の回帰テスト -------------------------------------------


async def test_concurrent_401_issue_only_one_new_token(server: Server) -> None:
    c = server.client()
    await c.authenticate()
    await server.control("expire_token")
    before = [r["path"] for r in server.mock.requests].count("/kabusapi/token")
    results = await asyncio.gather(c.wallet_cash(), c.wallet_margin(), c.orders(), c.soft_limit())
    assert all(r is not None for r in results)
    after = [r["path"] for r in server.mock.requests].count("/kabusapi/token")
    assert after - before == 1  # 互いのトークンを無効にし合わない
    await c.aclose()


async def test_concurrent_first_requests_issue_one_token(server: Server) -> None:
    c = server.client()
    await asyncio.gather(*(c.wallet_cash() for _ in range(4)))
    assert [r["path"] for r in server.mock.requests].count("/kabusapi/token") == 1
    await c.aclose()


async def test_margin_positions_stay_visible_after_midnight(server: Server) -> None:
    broker, c, updates = await start_broker(server)
    await broker.place_order(req())
    await wait_for(lambda: any(u.fills for u in updates))
    # 日付が変わって建玉日が「前日」になっても、起動後にできたボットの建玉として扱い続ける
    for p in server.mock.positions:
        p.day = "20261108"
    (pos,) = await broker.fetch_positions()
    assert pos.qty == 100
    payload = await broker.build_payload(req(Side.SELL, effect=PositionEffect.CLOSE))
    assert payload["ClosePositions"][0]["Qty"] == 100
    await broker.stop()
    await c.aclose()


async def test_cash_fills_during_downtime_are_the_bots(server: Server) -> None:
    """ボットが止まっている間に約定した現物の買いは、ボットの建玉として扱う(売り忘れない)。"""
    server.mock.add_cash_holding("7203", 500, D(1800))  # 長期保有の株
    trading = TradingConfig(account_mode="cash", cash_fund_type="02")
    first, c1, updates = await start_broker(server, trading)
    await first.place_order(req())
    await wait_for(lambda: any(u.fills for u in updates))
    await first.stop()  # ここでボットが落ち、約定は記録されなかった
    second, c2, _ = await start_broker(server, trading, today_net_qty=dict)
    (pos,) = await second.fetch_positions()
    assert pos.qty == 100
    await second.stop()
    await c1.aclose()
    await c2.aclose()


async def test_poll_loop_survives_unexpected_errors(server: Server) -> None:
    broker, c, _ = await start_broker(server)
    calls = {"n": 0}
    original = broker.poll_once

    async def boom() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("想定外のデータ")
        return await original()

    broker.poll_once = boom  # type: ignore[method-assign]
    await wait_for(lambda: calls["n"] >= 3)
    assert broker._task is not None and not broker._task.done()
    await broker.stop()
    await c.aclose()
