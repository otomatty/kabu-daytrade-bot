"""デモ用の合成の時価配信(口座がなくてもダッシュボードの動きを確かめられる)。

加速した時計の上で、銘柄ごとにランダムウォーク(日ごとにトレンドの強さが変わる)の値段を作る。
昼休みと夜間は時計を進めて飛ばす。値段は実在の銘柄の値動きとは関係ない。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal

from daytrade_bot.brokers.base import TickHandler
from daytrade_bot.core.clock import JST, AcceleratedClock
from daytrade_bot.core.models import Tick
from daytrade_bot.core.session import Phase, TradingSchedule
from daytrade_bot.core.ticksize import TickTable, round_to_tick, tick_size


class _Walk:
    def __init__(self, price: Decimal, table: TickTable, rng: random.Random) -> None:
        self.price = price
        self.table = table
        self.rng = rng
        self.volume = 0
        self.drift = 0.0
        self.vol = 0.015

    def new_day(self) -> None:
        self.volume = 0
        # 日ごとに、トレンドの向きと強さ、値動きの大きさを変える
        self.drift = self.rng.gauss(0, 0.012)
        self.vol = self.rng.uniform(0.008, 0.02)

    def step(self, dt_days: float) -> Decimal:
        z = self.rng.gauss(0, 1)
        factor = math.exp(self.drift * dt_days + self.vol * math.sqrt(dt_days) * z)
        raw = Decimal(str(float(self.price) * factor))
        self.price = round_to_tick(raw, self.table, up=self.rng.random() < 0.5)
        self.volume += self.rng.randint(1, 30) * 100
        return self.price


class DemoFeed:
    name = "demo"

    def __init__(
        self,
        clock: AcceleratedClock,
        schedule: TradingSchedule,
        prices: Mapping[str, Decimal],
        tables: Mapping[str, TickTable] | None = None,
        *,
        seed: int | None = None,
        interval_sec: float = 1.0,
    ) -> None:
        self._clock = clock
        self._schedule = schedule
        self._rng = random.Random(seed)
        tables = tables or {}
        self._walks = {
            s: _Walk(p, tables.get(s, TickTable.STANDARD), self._rng) for s, p in prices.items()
        }
        self._interval = interval_sec
        self._handler: TickHandler = lambda _t: None
        self._task: asyncio.Task[None] | None = None
        self._last: datetime | None = None
        self._day: date | None = None
        self._symbols: tuple[str, ...] = ()

    def set_tick_handler(self, handler: TickHandler) -> None:
        self._handler = handler

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_message_at(self) -> datetime | None:
        return self._last

    async def start(self, symbols: Sequence[str]) -> None:
        self._symbols = tuple(s for s in symbols if s in self._walks)
        self._task = asyncio.create_task(self._run(), name="demo-feed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def emit_once(self, now: datetime) -> list[Tick]:
        """全銘柄のティックを1つずつ作って配信する。"""
        if self._day != now.date():
            self._day = now.date()
            for walk in self._walks.values():
                walk.new_day()
        dt_days = self._interval / (5 * 3600)
        ticks = []
        for symbol in self._symbols:
            walk = self._walks[symbol]
            price = walk.step(dt_days)
            spread = tick_size(price + Decimal("0.0001"), walk.table)
            tick = Tick(
                symbol=symbol,
                time=now,
                price=price,
                cum_volume=walk.volume,
                bid=price,
                ask=price + spread,
            )
            ticks.append(tick)
            self._handler(tick)
        self._last = now
        return ticks

    def skip_closed_time(self, now: datetime) -> bool:
        """昼休みや取引時間外なら時計を次の立会まで進め、真を返す。"""
        phase = self._schedule.phase(now)
        times = self._schedule.times
        if phase is Phase.LUNCH:
            target = datetime.combine(now.date(), times.afternoon_open, tzinfo=JST)
        elif phase is Phase.HOLIDAY or (
            phase is Phase.CLOSED and now.time() >= times.closing_auction
        ):
            day = self._schedule.calendar.next_trading_day(now.date())
            target = datetime.combine(day, time(8, 59, 30), tzinfo=JST)
        else:
            return False
        self._clock.jump_to(target)
        # 時計を進めたことで「配信が途絶えた」と誤って判定されないようにする
        self._last = target
        return True

    async def _run(self) -> None:
        while True:
            now = self._clock.now()
            if not self.skip_closed_time(now) and self._schedule.phase(now).is_continuous:
                self.emit_once(now)
            await self._clock.sleep(self._interval)
