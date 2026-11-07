"""テスト用の小道具(合成の足、エンジンの組み立て)。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from daytrade_bot.brokers.sim import SimulatedBroker
from daytrade_bot.config import AppConfig
from daytrade_bot.core.bars import synthesize_ticks
from daytrade_bot.core.clock import JST, ManualClock
from daytrade_bot.core.engine import Engine, Mode
from daytrade_bot.core.models import Bar, Tick
from daytrade_bot.core.strategy import Strategy
from daytrade_bot.strategies.orb import OpeningRangeBreakout
from tests.conftest import TRADING_DAY, at, bar


def orb_day(
    symbol: str = "7203",
    day: date = TRADING_DAY,
    *,
    breakout: str = "up",
    after: Sequence[float] | None = None,
) -> list[Bar]:
    """寄付きから15分はレンジ 1990〜2010、その後にブレイクする1日の1分足(呼値は1円)。

    ``after`` を渡すと、ブレイクの足の後の終値の並びをその値にする(足は終値の前後±2円)。
    """
    bars: list[Bar] = []
    for m in range(15):
        base = 2000 + (m % 3) - 1
        bars.append(
            bar(
                symbol,
                at(9, m, day=day),
                base,
                2010 if m == 3 else base + 2,
                1990 if m == 7 else base - 2,
                base,
                v=1000,
            )
        )
    if breakout == "up":
        bars.append(bar(symbol, at(9, 15, day=day), 2008, 2016, 2006, 2015, v=5000))
        closes = after if after is not None else [2020 + 3 * i for i in range(20)]
    elif breakout == "down":
        bars.append(bar(symbol, at(9, 15, day=day), 1992, 1994, 1984, 1985, v=5000))
        closes = after if after is not None else [1980 - 3 * i for i in range(20)]
    else:
        closes = after if after is not None else [2000.0] * 20
    prev: float = float(bars[-1].close)
    start = bars[-1].start + timedelta(minutes=1)
    for i, c in enumerate(closes):
        o = prev
        bars.append(
            bar(symbol, start + timedelta(minutes=i), o, max(o, c) + 2, min(o, c) - 2, c, v=800)
        )
        prev = c
    return bars


def flat_bars(symbol: str, start: datetime, end: datetime, price: float) -> list[Bar]:
    out = []
    t = start
    while t < end:
        out.append(bar(symbol, t, price, price + 1, price - 1, price, v=100))
        t += timedelta(minutes=1)
    return out


def build_engine(
    config: AppConfig,
    *,
    mode: Mode = "backtest",
    start: datetime | None = None,
    strategy: Strategy[Any] | None = None,
    **kw: Any,
) -> tuple[Engine, ManualClock, SimulatedBroker]:
    clock = ManualClock(start or at(8, 59))
    broker = SimulatedBroker(
        clock,
        config.fees.model(),
        config.tick_table,
        slippage_ticks=config.simulation.slippage_ticks,
    )
    engine = Engine(
        config=config,
        mode=mode,
        clock=clock,
        broker=broker,
        strategy=strategy or OpeningRangeBreakout(config.symbols, config.strategy.params),
        **kw,
    )
    return engine, clock, broker


def ticks_from(bars: Iterable[Bar]) -> list[Tick]:
    out: list[Tick] = []
    cum: dict[str, int] = {}
    for b in sorted(bars, key=lambda x: (x.start, x.symbol)):
        out.extend(synthesize_ticks(b, cum.get(b.symbol, 0)))
        cum[b.symbol] = cum.get(b.symbol, 0) + b.volume
    return sorted(out, key=lambda t: (t.time, t.symbol))


async def replay(
    engine: Engine, clock: ManualClock, ticks: Iterable[Tick], until: datetime | None = None
) -> None:
    """ティックと毎分のタイマーを時刻順にエンジンへ流す。"""
    await engine.start()
    engine.push_timer(clock.now())
    await engine.drain()
    next_timer = clock.now().replace(second=3, microsecond=0) + timedelta(minutes=1)
    for t in ticks:
        while next_timer <= t.time:
            clock.set(max(next_timer, clock.now()))
            engine.push_timer(next_timer)
            await engine.drain()
            next_timer += timedelta(minutes=1)
        clock.set(max(t.time, clock.now()))
        engine.push_tick(t)
        await engine.drain()
    if until is not None:
        while next_timer <= until:
            clock.set(max(next_timer, clock.now()))
            engine.push_timer(next_timer)
            await engine.drain()
            next_timer += timedelta(minutes=1)


def jst(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=JST)
