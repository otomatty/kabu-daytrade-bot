from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from daytrade_bot.backtest.analytics import max_drawdown, sharpe_ratio, summarize
from daytrade_bot.backtest.runner import BacktestRunner, NoDataError
from daytrade_bot.core.events import EventBus
from daytrade_bot.core.models import (
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
)
from daytrade_bot.core.portfolio import RoundTrip
from daytrade_bot.notify import NullNotifier, WebhookNotifier
from daytrade_bot.storage.db import SCHEMA_VERSION, Repository, parse_dt
from tests.conftest import TRADING_DAY, D, at, make_config, tick
from tests.helpers import flat_bars, orb_day


class TestRepository:
    def test_ticks_roundtrip_and_days(self, repo: Repository) -> None:
        repo.buffer_tick(
            tick("7203", at(9, 0, 1, us=5), "1234.5", 100, bid=D("1234"), ask=D("1234.5"))
        )
        repo.buffer_tick(tick("6758", at(9, 0, 2), 3000))
        assert repo.flush_ticks() == 2
        assert repo.flush_ticks() == 0
        (t,) = repo.iter_ticks(["7203"], at(9), at(10))
        assert t.price == D("1234.5") and t.bid == D("1234") and t.time == at(9, 0, 1, us=5)
        assert len(repo.iter_ticks(["7203", "6758"], at(9), at(10))) == 2
        assert repo.tick_days() == [TRADING_DAY]
        assert repo.tick_days("6758") == [TRADING_DAY]

    def test_tick_buffer_autoflush(self, repo: Repository) -> None:
        for i in range(500):
            repo.buffer_tick(tick("7203", at(9, 0, 0, us=i), 1000))
        assert len(repo.iter_ticks(["7203"], at(9), at(10))) == 500

    def test_bars(self, repo: Repository) -> None:
        bars = flat_bars("7203", at(9), at(9, 3), 1000)
        assert repo.save_bars(bars) == 3
        repo.save_bars(bars[:1])  # 上書きしても重複しない
        assert repo.load_bars("7203", at(9), at(10)) == bars
        assert repo.bar_days("7203") == [TRADING_DAY]

    def test_orders_fills_events(self, repo: Repository) -> None:
        req = OrderRequest(
            "c1",
            "7203",
            Side.BUY,
            100,
            OrderType.LIMIT,
            PositionEffect.OPEN,
            at(9),
            limit_price=D(1000),
            tag="t",
        )
        order = Order(req, OrderStatus.SUBMITTED, at(9), broker_order_id="B1")
        repo.save_order("r", "paper", order)
        order.status = OrderStatus.FILLED
        order.filled_qty = 100
        order.avg_fill_price = D(999)
        repo.save_order("r", "paper", order)
        (loaded,) = repo.load_orders("paper", TRADING_DAY)
        assert loaded.status is OrderStatus.FILLED and loaded.avg_fill_price == D(999)
        assert loaded.request == req
        assert repo.load_orders("live", TRADING_DAY) == []
        fill = Fill("f1", "c1", "7203", Side.BUY, 100, D(999), D(10), at(9, 1), PositionEffect.OPEN)
        repo.save_fill("r", "paper", fill)
        repo.save_fill("r", "paper", fill)  # 重複は無視
        assert repo.load_fills("paper", TRADING_DAY) == [fill]
        assert repo.load_fills("paper", TRADING_DAY + timedelta(days=1)) == []
        repo.save_event("r", at(9), "info", "k", "メッセージ", data={"a": D(1)})
        repo.save_event("other", at(9), "info", "k", "別")
        (e,) = repo.recent_events(run_id="r")
        assert e.message == "メッセージ" and e.data == {"a": "1"}
        assert len(repo.recent_events()) == 2

    def test_runs(self, repo: Repository) -> None:
        repo.start_run("r", "paper", "orb", at(9), {"x": 1})
        repo.end_run("r", at(15))

    def test_schema_version_guard(self, tmp_path: Path) -> None:
        path = tmp_path / "v.sqlite3"
        r = Repository(path)
        r._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        r.close()
        with pytest.raises(RuntimeError, match="バージョン"):
            Repository(path)

    def test_transaction_rollback(self, repo: Repository) -> None:
        with pytest.raises(ValueError, match="boom"), repo._tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, mode, strategy, started_at, config) VALUES ('x','m','s','t','{}')"
            )
            raise ValueError("boom")
        assert repo._query("SELECT * FROM runs") == []

    def test_parse_dt_assumes_jst(self) -> None:
        assert parse_dt("2026-11-09T09:00:00") == at(9)


class TestEventBus:
    async def test_drops_oldest_when_full(self) -> None:
        bus = EventBus()
        with bus.subscribe(maxsize=2) as sub:
            for i in range(3):
                bus.publish({"i": i})
            assert sub.dropped == 1
            assert (await sub.get())["i"] == 1
            assert sub.get_nowait() == {"i": 2}
            assert sub.get_nowait() is None
        assert bus.subscriber_count == 0
        sub.close()  # 2回閉じても問題ない


class TestNotify:
    async def test_webhook_payloads_and_levels(self) -> None:
        sent: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200 if len(sent) == 1 else 500)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        n = WebhookNotifier(
            "https://example.invalid/hook",
            "slack",
            min_level="warning",
            client=client,
            min_interval_sec=0,
        )
        n.post("info", "無視される", "x")
        n.post("warning", "注意", "本文")
        n.post("error", "異常", "本文2")
        await asyncio.sleep(0.05)
        await n.aclose()
        assert [s["text"].splitlines()[0] for s in sent] == ["⚠️ [dtbot] 注意", "🚨 [dtbot] 異常"]
        assert n.sent == 1 and n.failed == 1
        await client.aclose()

    async def test_discord_and_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        n = WebhookNotifier(
            "https://example.invalid/hook",
            "discord",
            min_level="info",
            client=client,
            min_interval_sec=0,
        )
        assert n.payload(type("M", (), {"level": "info", "title": "t", "message": "m"})()) == {
            "content": "ℹ️ [dtbot] t\nm"
        }  # type: ignore[arg-type]
        n.post("info", "t", "m")
        await asyncio.sleep(0.05)
        await n.aclose()
        assert n.failed == 1
        await client.aclose()

    async def test_owned_client_closed_and_null(self) -> None:
        n = WebhookNotifier("https://example.invalid/hook", "slack")
        await n.aclose()
        null = NullNotifier()
        null.post("error", "a", "b")
        await null.aclose()


def trip(pnl: int, side: Side = Side.BUY, minutes: int = 10, day: date = TRADING_DAY) -> RoundTrip:
    return RoundTrip(
        "7203", side, 100, D(1000), D(1000), at(9, day=day), at(9, minutes, day=day), D(pnl), D(0)
    )


class TestAnalytics:
    def test_max_drawdown(self) -> None:
        assert max_drawdown([]) == (0.0, None)
        dd, pct = max_drawdown([100, 50, 200, 120, 300], base=1000)
        assert dd == -80 and pct == pytest.approx(-80 / 1200)
        assert max_drawdown([-10, -20])[0] == -20

    def test_sharpe(self) -> None:
        assert sharpe_ratio([100], 1000) is None
        assert sharpe_ratio([100, 100], 1000) is None
        assert sharpe_ratio([100, 200], 0) is None
        assert sharpe_ratio([100, -50, 80], 10_000) == pytest.approx(
            (130 / 3 / 10_000) / (__import__("statistics").stdev([0.01, -0.005, 0.008])) * 245**0.5
        )

    def test_summary(self) -> None:
        trips = [trip(1000), trip(-500, Side.SELL, 20), trip(2000, minutes=30)]
        daily = [(TRADING_DAY, D(500)), (TRADING_DAY + timedelta(days=1), D(2000))]
        equity = [(at(10), D(500)), (at(11), D(-100)), (at(12), D(2500))]
        s = summarize(trips, daily, equity, D(1_000_000), D(30))
        assert s.trades == 3 and s.wins == 2 and s.losses == 1
        assert s.win_rate == pytest.approx(2 / 3)
        assert s.profit_factor == pytest.approx(3000 / 500)
        assert s.payoff_ratio == pytest.approx(1500 / 500)
        assert s.net_pnl == 2500 and s.return_pct == pytest.approx(0.0025)
        assert s.max_drawdown == -600
        assert s.long_trades == 2 and s.short_pnl == -500
        assert s.avg_holding_minutes == pytest.approx(20)
        assert s.best_day == 2000 and s.positive_days == 2
        assert s.to_json()["trades"] == 3

    def test_empty_summary(self) -> None:
        s = summarize([], [], [], D(1), D(0))
        assert s.trades == 0 and s.win_rate is None and s.profit_factor is None and s.sharpe is None


class TestBacktestRunner:
    async def test_runs_over_multiple_days(self, repo: Repository) -> None:
        cfg = make_config(
            universe=[{"symbol": "7203"}, {"symbol": "6758"}], simulation={"slippage_ticks": 1}
        )
        day2 = date(2026, 11, 10)
        for d in (TRADING_DAY, day2):
            repo.save_bars(orb_day("7203", d))
            repo.save_bars(orb_day("6758", d, breakout="down"))
        runner = BacktestRunner(cfg, repo, TRADING_DAY, date(2026, 11, 11))
        result = await runner.run()
        s = result.summary
        assert s.trading_days == 2
        assert s.trades == 4 and s.long_trades == 2 and s.short_trades == 2
        assert s.net_pnl > 0
        assert result.sim_stats["fills"] == 8
        assert result.sim_stats["slippage_cost"] > 0  # 1呼値のスリッページ
        assert [d for d, _, _ in result.daily] == [TRADING_DAY, day2]
        assert result.equity[-1][1] == pytest.approx(D(str(s.net_pnl)))
        record = result.to_record(at(16))
        repo.save_backtest(record)
        listed = repo.list_backtests()
        assert listed[0]["run_id"] == result.run_id and listed[0]["summary"]["trades"] == 4
        loaded = repo.get_backtest(result.run_id)
        assert (
            loaded is not None
            and len(loaded.trades) == 4
            and loaded.daily[0]["date"] == "2026-11-09"
        )
        assert repo.get_backtest("missing") is None

    async def test_positions_left_at_close_are_force_closed(self, repo: Repository) -> None:
        cfg = make_config(
            universe=[{"symbol": "7203"}], strategy={"name": "orb", "params": {"take_profit_r": 20}}
        )
        # 15:20 以降に値が付かず、手仕舞いの注文が約定しない
        repo.save_bars(orb_day(after=[2020] * 3) + flat_bars("7203", at(9, 19), at(15, 20), 2020))
        result = await BacktestRunner(cfg, repo, TRADING_DAY, TRADING_DAY).run()
        assert result.summary.trades == 1
        assert result.trades[0].exit_price == D(2020)
        assert result.trades[0].closed_at == at(15, 35)

    async def test_ticks_source(self, repo: Repository) -> None:
        cfg = make_config(universe=[{"symbol": "7203"}])
        from tests.helpers import ticks_from

        for t in ticks_from(orb_day()):
            repo.buffer_tick(t)
        repo.flush_ticks()
        result = await BacktestRunner(cfg, repo, TRADING_DAY, TRADING_DAY, source="ticks").run()
        assert result.summary.trades == 1 and result.sim_stats["source"] == "ticks"

    async def test_no_data(self, repo: Repository) -> None:
        with pytest.raises(NoDataError, match="データがありません"):
            await BacktestRunner(make_config(), repo, TRADING_DAY, TRADING_DAY).run()
        with pytest.raises(ValueError, match="終了日"):
            BacktestRunner(make_config(), repo, TRADING_DAY, TRADING_DAY - timedelta(days=1))
