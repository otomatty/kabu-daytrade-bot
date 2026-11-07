"""バックテストの実行。

本番と同じ ``Engine`` を、手動で進める時計とシミュレーションのブローカーで動かす。
データは保存済みのティック(``collect`` で収集したもの)か1分足(CSVから取り込んだもの)を使う。
足を使う場合は、足ごとに4本のティック(始値・高値・安値・終値)を合成して流す。
"""

from __future__ import annotations

import heapq
import secrets
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from daytrade_bot.backtest.analytics import Summary, summarize
from daytrade_bot.brokers.sim import SimulatedBroker
from daytrade_bot.config import AccountMode, AppConfig
from daytrade_bot.core.bars import synthesize_ticks
from daytrade_bot.core.clock import JST, ManualClock
from daytrade_bot.core.engine import Engine, trip_json
from daytrade_bot.core.models import Bar, Tick
from daytrade_bot.core.portfolio import RoundTrip
from daytrade_bot.core.strategy import Strategy, load_strategy_class
from daytrade_bot.storage.db import BacktestRecord, Repository, iso

Source = Literal["bars", "ticks"]


class NoDataError(Exception):
    """指定した期間にデータがない。"""


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    start: date
    end: date
    symbols: tuple[str, ...]
    summary: Summary
    trades: tuple[RoundTrip, ...]
    daily: tuple[tuple[date, Decimal, int], ...]
    equity: tuple[tuple[datetime, Decimal], ...]
    strategy: dict[str, Any]
    sim_stats: dict[str, Any]

    def to_record(self, created_at: datetime) -> BacktestRecord:
        return BacktestRecord(
            run_id=self.run_id,
            created_at=created_at,
            strategy=str(self.strategy["name"]),
            params=dict(self.strategy["params"]),
            start_date=self.start,
            end_date=self.end,
            symbols=list(self.symbols),
            summary={**self.summary.to_json(), "sim": self.sim_stats},
            equity=[[iso(t), float(v)] for t, v in self.equity],
            trades=[trip_json(t) for t in self.trades],
            daily=[{"date": d.isoformat(), "pnl": float(p), "trades": n} for d, p, n in self.daily],
        )


def build_strategy(config: AppConfig) -> Strategy[Any]:
    cls = load_strategy_class(config.strategy.name)
    return cls(config.symbols, config.strategy.params)


class BacktestRunner:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        start: date,
        end: date,
        *,
        source: Source = "bars",
        strategy: Strategy[Any] | None = None,
    ) -> None:
        if end < start:
            raise ValueError("終了日は開始日以降にしてください")
        self.config = config
        self.repo = repo
        self.start = start
        self.end = end
        self.source: Source = source
        self.strategy = strategy or build_strategy(config)

    def _days(self) -> list[date]:
        return self.config.session.schedule().calendar.trading_days(self.start, self.end)

    def _day_ticks(self, day: date) -> list[Tick]:
        start = datetime.combine(day, time(0), tzinfo=JST)
        end = start + timedelta(days=1)
        symbols = self.config.symbols
        if self.source == "ticks":
            return self.repo.iter_ticks(symbols, start, end)
        streams: list[Iterator[Tick]] = []
        for symbol in symbols:
            bars = self.repo.load_bars(symbol, start, end)
            streams.append(iter(_bars_to_ticks(bars)))
        return list(heapq.merge(*streams, key=lambda t: (t.time, t.symbol)))

    async def run(self) -> BacktestResult:
        cfg = self.config
        days = self._days()
        day_ticks = [(d, self._day_ticks(d)) for d in days]
        if not any(ticks for _, ticks in day_ticks):
            raise NoDataError(
                f"{self.start}〜{self.end} に {', '.join(cfg.symbols)} の"
                f"{'ティック' if self.source == 'ticks' else '足'}データがありません"
            )
        first_day = days[0]
        clock = ManualClock(datetime.combine(first_day, time(8, 0), tzinfo=JST))
        broker = SimulatedBroker(
            clock,
            cfg.fees.model(),
            cfg.tick_table,
            slippage_ticks=cfg.simulation.slippage_ticks,
            limit_fill_on_touch=cfg.simulation.limit_fill_on_touch,
            name="bt",
            allow_short=cfg.trading.account_mode is AccountMode.MARGIN,
        )
        run_id = f"bt-{datetime.now(JST):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        engine = Engine(
            config=cfg,
            mode="backtest",
            clock=clock,
            broker=broker,
            strategy=self.strategy,
            run_id=run_id,
        )
        await engine.start()
        for day, ticks in day_ticks:
            if not ticks:
                continue
            await self._run_day(engine, clock, broker, day, ticks)
            engine.close_day()
            engine.day = None  # 次の日の最初のイベントで日次の状態をリセットさせる
            engine.equity_curve.append((clock.now(), engine.cumulative_pnl))
        total_fees = sum((s.fees for s in engine.day_summaries), Decimal(0))
        trips = tuple(t for s in engine.day_summaries for t in s.trades)
        daily = tuple((s.day, s.net, len(s.trades)) for s in engine.day_summaries)
        equity = tuple(engine.equity_curve)
        summary = summarize(
            trips,
            [(d, p) for d, p, _ in daily],
            equity,
            cfg.simulation.initial_capital,
            total_fees,
        )
        return BacktestResult(
            run_id=run_id,
            start=self.start,
            end=self.end,
            symbols=cfg.symbols,
            summary=summary,
            trades=trips,
            daily=daily,
            equity=equity,
            strategy=self.strategy.describe(),
            sim_stats={
                "orders": broker.stats.orders,
                "fills": broker.stats.fills,
                "rejects": broker.stats.rejects,
                "cancels": broker.stats.cancels,
                "slippage_cost": float(broker.stats.slippage_cost),
                "source": self.source,
            },
        )

    async def _run_day(
        self,
        engine: Engine,
        clock: ManualClock,
        broker: SimulatedBroker,
        day: date,
        ticks: Sequence[Tick],
    ) -> None:
        # 寄付き前に日付を切り替える(売買高の累計を0から数えるため)
        pre_open = datetime.combine(day, time(8, 59), tzinfo=JST)
        clock.set(max(pre_open, clock.now()))
        engine.push_timer(clock.now())
        await engine.drain()
        for when in _merge_timers(ticks, day):
            if isinstance(when, Tick):
                if when.time > clock.now():
                    clock.set(when.time)
                engine.push_tick(when)
            else:
                if when > clock.now():
                    clock.set(when)
                engine.push_timer(when)
            await engine.drain()
        # 取引終了後に残った注文・建玉を整理する
        closing = datetime.combine(day, time(15, 35), tzinfo=JST)
        if closing > clock.now():
            clock.set(closing)
        engine.push_timer(clock.now())
        await engine.drain()
        reason = "取引終了時に残った建玉の精算"
        await engine.submit_closing_orders_unchecked(reason)
        if broker.settle_at_last(reason):
            engine.log(
                "warning", "backtest", f"{day}: 取引終了時に残った建玉を最後の値段で決済しました"
            )
        await engine.drain()


def _bars_to_ticks(bars: Iterable[Bar]) -> list[Tick]:
    ticks: list[Tick] = []
    cum = 0
    for bar in bars:
        ticks.extend(synthesize_ticks(bar, cum))
        cum += bar.volume
    return ticks


def _merge_timers(ticks: Sequence[Tick], day: date) -> Iterator[Tick | datetime]:
    """ティックの間に、毎分(足の確定や手仕舞いの判定のため)タイマーを挟む。"""
    open_ = datetime.combine(day, time(9, 0), tzinfo=JST)
    close = datetime.combine(day, time(15, 31), tzinfo=JST)
    grace = timedelta(seconds=3)
    timers = []
    t = open_
    while t <= close:
        timers.append(t + grace)
        t += timedelta(minutes=1)
    i = 0
    for tick in ticks:
        while i < len(timers) and timers[i] <= tick.time:
            yield timers[i]
            i += 1
        yield tick
    yield from timers[i:]
