from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from daytrade_bot.brokers.base import BrokerError, OrderRejected, OrderStateUnknown
from daytrade_bot.core.engine import STALE_FEED_REASON, Command
from daytrade_bot.core.events import EventBus
from daytrade_bot.core.models import (
    Bar,
    BrokerOrderSnapshot,
    BrokerPosition,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from daytrade_bot.core.risk import RiskState
from daytrade_bot.core.strategy import Strategy, StrategyContext, StrategyParams
from daytrade_bot.storage.db import Repository
from tests.conftest import TRADING_DAY, D, at, make_config, tick
from tests.helpers import build_engine, flat_bars, orb_day, replay, ticks_from


async def test_orb_long_trade_hits_target() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    engine, clock, _ = build_engine(cfg)
    await replay(engine, clock, ticks_from(orb_day()), until=at(10, 0))
    p = engine.portfolio
    assert p.qty("7203") == 0
    (trip,) = p.round_trips
    assert trip.side is Side.BUY and trip.qty == 100
    assert trip.pnl > 0
    # 利食い = 約定値 + レンジ幅(20円)×2。利食いの判定の次のティックで約定する
    assert trip.entry_price == D(2015)
    assert trip.exit_price >= trip.entry_price + 38
    kinds = [e.kind for e in engine.events_log]
    assert "fill" in kinds and "order" in kinds
    assert any("レンジ確定" in e.message for e in engine.events_log)


async def test_orb_short_trade() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    engine, clock, _ = build_engine(cfg)
    await replay(engine, clock, ticks_from(orb_day(breakout="down")), until=at(10, 0))
    (trip,) = engine.portfolio.round_trips
    assert trip.side is Side.SELL and trip.pnl > 0


async def test_stop_loss() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2018, 2010, 2000, 1990, 1985, 1980])
    await replay(engine, clock, ticks_from(bars), until=at(10, 0))
    (trip,) = engine.portfolio.round_trips
    assert trip.pnl < 0
    # 損切り = 約定値 - 20円。足の中の値動きで少し下まで滑る
    assert trip.entry_price - trip.exit_price <= 30


async def test_forced_flatten_at_1520_and_no_entries_after_cutoff() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], strategy={"name": "orb", "params": {"take_profit_r": 20}}
    )
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2020] * 5) + flat_bars("7203", at(9, 21), at(15, 25), 2025)
    await replay(engine, clock, ticks_from(bars), until=at(15, 31))
    assert engine.portfolio.qty("7203") == 0
    (trip,) = engine.portfolio.round_trips
    assert at(15, 20) <= trip.closed_at < at(15, 22)
    assert any("強制手仕舞い" in e.message for e in engine.events_log)


async def test_daily_loss_limit_halts_and_flattens() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"max_daily_loss": 1500})
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2016, 2014, 2012, 2008, 2004, 2000])
    # 損切り(約定値-20円)より先に、損失上限(100株×15円)に届く
    await replay(engine, clock, ticks_from(bars), until=at(10, 0))
    assert engine.risk.state is RiskState.HALTED
    assert "日次損失上限" in engine.risk.reason
    assert engine.portfolio.qty("7203") == 0


async def test_sizing_by_notional_rounds_to_lot() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], sizing={"method": "fixed_notional", "notional": 700_000}
    )
    engine, clock, _ = build_engine(cfg)
    await replay(engine, clock, ticks_from(orb_day()), until=at(9, 30))
    assert engine.portfolio.round_trips[0].qty == 300  # 700,000 / 2015 = 347株 → 300株


async def test_notional_too_small_is_skipped() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], sizing={"method": "fixed_notional", "notional": 100_000}
    )
    engine, clock, _ = build_engine(cfg)
    await replay(engine, clock, ticks_from(orb_day()), until=at(9, 30))
    assert engine.portfolio.round_trips == []
    assert any("1単元" in e.message for e in engine.events_log)


async def test_commands_in_backtest_mode() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], strategy={"name": "orb", "params": {"take_profit_r": 20}}
    )
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2020] * 10)
    await replay(engine, clock, ticks_from(bars[:18]))
    assert engine.portfolio.qty("7203") == 100
    assert "停止" in await engine.command("pause")
    assert engine.risk.state is RiskState.PAUSED
    assert "停止しました" in await engine.command(Command.PAUSE, "理由を更新")
    assert engine.risk.reason == "理由を更新"
    assert "再開" in await engine.command("resume")
    assert "再開できません" in await engine.command("resume")
    assert "全決済" in await engine.command("flatten")
    # 次のティックで約定する
    clock.advance(1)
    engine.push_tick(tick("7203", clock.now(), 2021))
    await engine.drain()
    assert engine.portfolio.qty("7203") == 0
    assert engine.risk.state is RiskState.PAUSED
    assert "緊急停止しました" in await engine.command("halt", "テスト")
    assert engine.risk.state is RiskState.HALTED
    assert "解除" in await engine.command("clear_halt")
    assert "ではありません" in await engine.command("clear_halt")
    assert engine.risk.state is RiskState.PAUSED
    await engine.command("resume")
    assert engine.risk.state is RiskState.ACTIVE
    assert not engine._flatten_requested  # 再開後に新しい建玉がすぐ決済されることはない
    with pytest.raises(ValueError, match="bogus"):
        await engine.command("bogus")


async def test_halt_during_lunch_flattens_at_afternoon_open() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], strategy={"name": "orb", "params": {"take_profit_r": 20}}
    )
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2020] * 5) + flat_bars("7203", at(9, 21), at(11, 30), 2022)
    await replay(engine, clock, ticks_from(bars), until=at(11, 45))
    assert engine.portfolio.qty("7203") == 100
    await engine.command("halt")
    assert engine.portfolio.qty("7203") == 100  # 昼休みは発注しない
    pm = flat_bars("7203", at(12, 30), at(12, 35), 2022)
    await replay(engine, clock, ticks_from(pm))
    assert engine.portfolio.qty("7203") == 0


async def test_events_are_published_to_subscribers() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    bus = EventBus()
    engine, clock, _ = build_engine(cfg, bus=bus)
    sub = bus.subscribe(maxsize=100_000)
    await replay(engine, clock, ticks_from(orb_day()), until=at(9, 40))
    types = set()
    while (e := sub.get_nowait()) is not None:
        types.add(e["type"])
    assert {"bar", "tick", "order", "fill", "event", "snapshot"} <= types
    snap = engine.snapshot()
    assert snap["mode"] == "backtest" and snap["pnl"]["trades"] == 1
    assert snap["risk"]["limits"]["flatten_at"] == "15:20"
    assert snap["phase"]["code"] == "morning"
    assert snap["orders"] and snap["round_trips"] and snap["equity"]
    sub.close()
    assert bus.subscriber_count == 0


class ScriptedStrategy(Strategy[StrategyParams]):
    """テスト用: 指定した時刻にシグナルを出す。"""

    name = "scripted"
    Params = StrategyParams

    def __init__(self, symbols: Sequence[str], script: dict[datetime, list[Signal]]) -> None:
        super().__init__(symbols)
        self.script = script
        self.fills: list[Any] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        return self.script.pop(bar.end, [])

    def on_fill(self, fill: Any, ctx: StrategyContext) -> None:
        self.fills.append(fill)


async def test_signal_handling_rules() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    s = "7203"
    script = {
        at(9, 1): [
            Signal(s, SignalAction.EXIT),  # 建玉がないので無視
            Signal("9999", SignalAction.ENTER_LONG),  # 対象外の銘柄
            Signal(s, SignalAction.ENTER_LONG, qty=150),  # 単元に丸めて100株
            Signal(s, SignalAction.ENTER_LONG),  # 未約定の注文があるので見送り
        ],
        at(9, 2): [Signal(s, SignalAction.ENTER_LONG)],  # 建玉があるので見送り
        at(9, 3): [Signal(s, SignalAction.EXIT, qty=50)],  # 一部返済(単元未満でも返済はそのまま)
        at(9, 4): [
            Signal(s, SignalAction.EXIT),
            Signal(s, SignalAction.EXIT),
        ],  # 2つ目は残数0で無視
        at(9, 5): [
            Signal(
                s,
                SignalAction.ENTER_SHORT,
                order_type=OrderType.LIMIT,
                limit_price=D("2995.4"),
                qty=100,
            )
        ],
    }
    strat = ScriptedStrategy([s], script)
    engine, clock, _ = build_engine(cfg, strategy=strat)
    await replay(engine, clock, ticks_from(flat_bars(s, at(9), at(9, 10), 3000)), until=at(9, 11))
    msgs = [e.message for e in engine.events_log]
    assert any("対象外の銘柄" in m for m in msgs)
    assert any("未約定の注文" in m for m in msgs)
    assert any("すでに建玉" in m for m in msgs)
    orders = engine.orders.all_orders()
    assert [o.request.qty for o in orders[:3]] == [100, 50, 50]
    limit = orders[-1]
    assert limit.request.limit_price == D(2996)  # 売りの指値は呼値に切り上げ
    # 市場価格(3000)より低い売り指値は即座に約定し、約定値は有利な市場価格になる
    assert limit.request.side is Side.SELL and limit.status is OrderStatus.FILLED
    assert limit.avg_fill_price == D(3000)
    assert len(strat.fills) == 4


async def test_rejections_count_toward_halt() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"max_consecutive_errors": 2})
    s = "7203"
    strat = ScriptedStrategy([s], {at(9, m): [Signal(s, SignalAction.ENTER_LONG)] for m in (1, 2)})
    engine, clock, broker = build_engine(cfg, strategy=strat)

    async def reject(req: OrderRequest) -> str:
        raise OrderRejected("余力が不足しています", code=4002001)

    broker.place_order = reject  # type: ignore[method-assign]
    await replay(engine, clock, ticks_from(flat_bars(s, at(9), at(9, 4), 3000)), until=at(9, 5))
    assert engine.risk.state is RiskState.HALTED
    assert any("拒否" in e.message for e in engine.events_log)


class LossyBroker:
    """発注の応答を失うブローカー(注文は証券会社側には届いている)。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = "lossy"

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    async def place_order(self, req: OrderRequest) -> str:
        await self.inner.place_order(req)
        raise OrderStateUnknown("応答がありません")


async def test_unknown_order_is_resolved_by_reconcile() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    s = "7203"
    strat = ScriptedStrategy([s], {at(9, 1): [Signal(s, SignalAction.ENTER_LONG)]})
    engine, clock, sim = build_engine(cfg, strategy=strat, mode="paper")
    lossy = LossyBroker(sim)
    engine.broker = lossy
    engine.orders._broker = lossy
    await replay(engine, clock, ticks_from(flat_bars(s, at(9), at(9, 3), 3000)))
    order = engine.orders.all_orders()[0]
    # 発注の結果が不明になった直後に照合し、約定まで反映されている
    assert order.status is OrderStatus.FILLED
    assert order.broker_order_id is not None
    assert engine.portfolio.qty(s) == 100
    assert any("受付を確認" in e.message for e in engine.events_log)


async def test_reconcile_mismatch_halts_after_two_checks() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"reconcile_interval_sec": 1})
    engine, clock, sim = build_engine(cfg, mode="paper", strategy=ScriptedStrategy(["7203"], {}))
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9), at(9, 2), 3000)))
    # 証券会社側に、ボットが知らない建玉ができた(手動の発注など)
    sim._positions["7203"] = type(
        sim._positions.get("x", None)
        or __import__("daytrade_bot.brokers.sim", fromlist=["_SimPosition"])._SimPosition()
    )(qty=-100, avg_price=D(3000))
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9, 2), at(9, 5), 3000)))
    assert engine.risk.state is RiskState.HALTED
    assert "一致しません" in engine.risk.reason
    # 証券会社の建玉に合わせて買い戻している
    assert engine.portfolio.qty("7203") == 0
    assert (await sim.fetch_positions()) == []


async def test_reconcile_fetch_error_is_counted() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"max_consecutive_errors": 1})
    engine, clock, sim = build_engine(cfg, mode="paper", strategy=ScriptedStrategy(["7203"], {}))

    async def boom() -> list[BrokerOrderSnapshot]:
        raise BrokerError("接続できません")

    sim.fetch_orders = boom  # type: ignore[method-assign]
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9), at(9, 2), 3000)))
    assert engine.risk.state is RiskState.HALTED


class FakeFeed:
    name = "fake"

    def __init__(self) -> None:
        self.handler: Any = None
        self.last: datetime | None = None
        self.started: list[str] = []

    def set_tick_handler(self, handler: Any) -> None:
        self.handler = handler

    async def start(self, symbols: Sequence[str]) -> None:
        self.started = list(symbols)

    async def stop(self) -> None:
        self.started = []

    @property
    def connected(self) -> bool:
        return True

    @property
    def last_message_at(self) -> datetime | None:
        return self.last


async def test_stale_feed_pauses_and_resumes() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"stale_data_sec": 10})
    feed = FakeFeed()
    engine, clock, _ = build_engine(
        cfg, mode="paper", feed=feed, start=at(10), strategy=ScriptedStrategy(["7203"], {})
    )
    await engine.start()
    assert feed.started == ["7203"]
    clock.set(at(10, 0, 5))
    engine.push_timer()
    await engine.drain()
    assert engine.risk.state is RiskState.ACTIVE  # 起動直後は猶予
    clock.set(at(10, 1, 15))
    engine.push_timer()
    await engine.drain()
    assert engine.risk.state is RiskState.PAUSED and engine.risk.reason == STALE_FEED_REASON
    feed.last = clock.now()
    engine.push_timer()
    await engine.drain()
    assert engine.risk.state is RiskState.ACTIVE
    snap = engine.snapshot()
    assert snap["feed"]["name"] == "fake" and snap["feed"]["last_message_age_sec"] == 0
    await engine.shutdown()
    assert feed.started == []


async def test_new_day_resets_state(repo: Repository) -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    engine, clock, _ = build_engine(cfg, mode="paper", repo=repo)
    await replay(engine, clock, ticks_from(orb_day()), until=at(10, 0))
    assert engine.portfolio.round_trips
    await engine.command("halt")
    next_day = TRADING_DAY + timedelta(days=1)
    clock.set(at(8, 59, day=next_day))
    engine.push_timer()
    await engine.drain()
    assert engine.day == next_day
    assert engine.risk.state is RiskState.ACTIVE
    assert engine.portfolio.round_trips == []
    assert len(engine.day_summaries) == 1 and engine.day_summaries[0].net > 0
    assert engine.cumulative_pnl == engine.day_summaries[0].net
    # 保存されている
    assert repo.load_fills("paper", TRADING_DAY)
    assert repo.load_orders("paper", TRADING_DAY)
    assert repo.recent_events(10)


async def test_live_restart_restores_from_fills_and_broker(repo: Repository) -> None:
    cfg = make_config(universe=[{"symbol": "7203"}, {"symbol": "6758"}])
    # 1回目: 7203 を買って、決済前に停止
    engine, clock, _ = build_engine(
        cfg,
        mode="live",
        repo=repo,
        strategy=ScriptedStrategy(["7203"], {at(9, 1): [Signal("7203", SignalAction.ENTER_LONG)]}),
    )
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9), at(9, 3), 3000)))
    assert engine.portfolio.qty("7203") == 100
    await engine.shutdown()
    # 2回目: 停止中に 6758 の建玉ができていた(手動の注文など)
    engine2, _, sim2 = build_engine(
        cfg,
        mode="live",
        repo=repo,
        start=at(9, 10),
        run_id="run2",
        strategy=ScriptedStrategy(["7203"], {}),
    )

    async def positions() -> list[BrokerPosition]:
        return [BrokerPosition("7203", 100, D(3000)), BrokerPosition("6758", -100, D(1500))]

    sim2.fetch_positions = positions  # type: ignore[method-assign]
    await engine2.start()
    assert engine2.portfolio.qty("7203") == 100
    assert engine2.portfolio.qty("6758") == -100
    assert any("停止中の約定" in e.message for e in engine2.events_log)
    assert any("起動時点の建玉" in e.message for e in engine2.events_log)


async def test_internal_error_is_contained() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"max_consecutive_errors": 1})

    class Boom(ScriptedStrategy):
        def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
            raise RuntimeError("戦略のバグ")

    engine, clock, _ = build_engine(cfg, strategy=Boom(["7203"], {}))
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9), at(9, 2), 3000)))
    assert engine.risk.state is RiskState.HALTED
    assert any("内部エラー" in e.message for e in engine.events_log)


async def test_run_loop_stops_on_command() -> None:
    import asyncio

    from daytrade_bot.core.clock import AcceleratedClock

    cfg = make_config(universe=[{"symbol": "7203"}])
    clock = AcceleratedClock(at(10), speed=60)
    from daytrade_bot.brokers.sim import SimulatedBroker

    broker = SimulatedBroker(clock, cfg.fees.model(), cfg.tick_table)
    from daytrade_bot.core.engine import Engine

    engine = Engine(
        config=cfg, mode="demo", clock=clock, broker=broker, strategy=ScriptedStrategy(["7203"], {})
    )
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    engine.push_tick(Tick("7203", clock.now(), Decimal(3000)))
    assert "停止" in await engine.command("stop")
    await asyncio.wait_for(task, timeout=2)
    assert any(e.message == "停止しました" for e in engine.events_log)


async def test_limit_order_timeout_cancels() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], trading={"limit_order_timeout_sec": 30})
    s = "7203"
    strat = ScriptedStrategy(
        [s],
        {
            at(9, 1): [
                Signal(s, SignalAction.ENTER_LONG, order_type=OrderType.LIMIT, limit_price=D(2900))
            ]
        },
    )
    engine, clock, _ = build_engine(cfg, strategy=strat, mode="paper")
    await replay(engine, clock, ticks_from(flat_bars(s, at(9), at(9, 3), 3000)))
    (order,) = engine.orders.all_orders()
    assert order.status is OrderStatus.CANCELED
    assert order.request.effect is PositionEffect.OPEN


# ---- 独立したレビューで見つかった不具合の回帰テスト -------------------------------------------


async def test_pending_entries_count_toward_limits_across_symbols() -> None:
    """約定の通知が来る前に複数の銘柄で続けて発注しても、銘柄数と合計の上限を超えない。"""
    cfg = make_config(
        universe=[{"symbol": s} for s in ("7203", "6758", "9984")],
        risk={"max_open_positions": 2, "max_total_position_value": 3_000_000},
    )
    script = {at(9, 1): [Signal(s, SignalAction.ENTER_LONG) for s in ("7203", "6758", "9984")]}
    engine, clock, _ = build_engine(cfg, strategy=ScriptedStrategy(cfg.symbols, script))
    bars = [b for s in cfg.symbols for b in flat_bars(s, at(9), at(9, 2), 2000)]
    await replay(
        engine, clock, ticks_from(bars)[:13]
    )  # 9:01 の足が確定した直後(まだ約定していない)
    assert len(engine.orders.all_orders()) == 2
    assert any("銘柄数の上限" in e.message for e in engine.events_log)


async def test_feed_recovery_does_not_undo_flatten() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}], risk={"stale_data_sec": 10})
    feed = FakeFeed()
    engine, clock, _ = build_engine(
        cfg, mode="paper", feed=feed, start=at(10), strategy=ScriptedStrategy(["7203"], {})
    )
    await engine.start()
    clock.set(at(10, 1))
    engine.push_timer()
    await engine.drain()
    assert engine.risk.reason == STALE_FEED_REASON
    await engine.command("flatten")
    feed.last = clock.now()
    engine.push_timer()
    await engine.drain()
    assert engine.risk.state is RiskState.PAUSED  # 時価が戻っても、利用者の全決済の後は再開しない
    assert engine.risk.reason == "全決済の操作"


async def test_previous_day_tick_does_not_clear_halt() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    engine, clock, _ = build_engine(cfg, strategy=ScriptedStrategy(["7203"], {}))
    await replay(engine, clock, ticks_from(flat_bars("7203", at(9), at(9, 2), 2000)))
    await engine.command("halt")
    old = tick("7203", at(14, 59, day=TRADING_DAY - timedelta(days=3)), 1990)
    engine.push_tick(old)
    await engine.drain()
    assert engine.risk.state is RiskState.HALTED
    assert engine.day == TRADING_DAY and engine.old_ticks == 1
    assert engine.portfolio.last_prices["7203"] == D(2000)


async def test_resume_while_halted_keeps_flatten_pending() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}], strategy={"name": "orb", "params": {"take_profit_r": 20}}
    )
    engine, clock, _ = build_engine(cfg)
    bars = orb_day(after=[2020] * 5) + flat_bars("7203", at(9, 21), at(11, 30), 2022)
    await replay(engine, clock, ticks_from(bars), until=at(11, 45))
    await engine.command("halt")
    assert "再開できません" in await engine.command("resume")
    await replay(engine, clock, ticks_from(flat_bars("7203", at(12, 30), at(12, 33), 2022)))
    assert engine.portfolio.qty("7203") == 0


async def test_reconcile_success_resets_errors_and_quiet_hours_do_not_halt() -> None:
    cfg = make_config(
        universe=[{"symbol": "7203"}],
        risk={"max_consecutive_errors": 2, "reconcile_interval_sec": 1},
    )
    engine, clock, sim = build_engine(
        cfg, mode="paper", start=at(16), strategy=ScriptedStrategy(["7203"], {})
    )
    fail = {"on": True}
    original = sim.fetch_orders

    async def flaky() -> list[BrokerOrderSnapshot]:
        if fail["on"]:
            raise BrokerError("ログアウト中")
        return list(await original())

    sim.fetch_orders = flaky  # type: ignore[method-assign]
    await engine.start()
    for second in range(5):
        clock.set(at(8, 0, second, day=TRADING_DAY + timedelta(days=1)))
        engine.push_timer()
        await engine.drain()
    assert engine.risk.state is RiskState.ACTIVE  # 取引時間外の失敗では止めない
    engine.risk.record_error(clock.now())
    fail["on"] = False
    clock.set(at(9, 0, 10, day=TRADING_DAY + timedelta(days=1)))
    engine.push_timer()
    await engine.drain()
    assert engine.risk.consecutive_errors == 0  # 照合に成功したら数え直す


async def test_flatten_replaces_resting_limit_close_with_market() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    s = "7203"
    script = {
        at(9, 1): [Signal(s, SignalAction.ENTER_LONG)],
        at(9, 2): [Signal(s, SignalAction.EXIT, order_type=OrderType.LIMIT, limit_price=D(2100))],
    }
    engine, clock, _ = build_engine(cfg, mode="backtest", strategy=ScriptedStrategy([s], script))
    await replay(engine, clock, ticks_from(flat_bars(s, at(9), at(9, 3), 2000)))
    assert engine.portfolio.qty(s) == 100
    await engine.command("flatten")
    await replay(engine, clock, ticks_from(flat_bars(s, at(9, 3), at(9, 5), 2000)))
    assert engine.portfolio.qty(s) == 0
    statuses = [o.status for o in engine.orders.all_orders()]
    assert OrderStatus.CANCELED in statuses


def test_order_ids_differ_between_runs() -> None:
    cfg = make_config(universe=[{"symbol": "7203"}])
    e1, _, _ = build_engine(cfg, mode="live", strategy=ScriptedStrategy(["7203"], {}))
    e2, _, _ = build_engine(cfg, mode="live", strategy=ScriptedStrategy(["7203"], {}))
    ids = [
        e.orders.new_request(
            symbol="7203", side=Side.BUY, qty=100, effect=PositionEffect.OPEN, now=at(9)
        ).client_order_id
        for e in (e1, e2)
    ]
    assert ids[0] != ids[1]
