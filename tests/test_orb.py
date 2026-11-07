from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from decimal import Decimal

from daytrade_bot.core.models import Bar, Fill, PositionEffect, Side, Signal, SignalAction
from daytrade_bot.strategies.orb import OpeningRangeBreakout
from tests.conftest import D, at, bar, tick


class Ctx:
    def __init__(self) -> None:
        self.pos = 0
        self.avg: Decimal | None = None
        self.active = False
        self.allowed = True
        self.logs: list[str] = []

    def now(self):  # pragma: no cover - 使わない
        return at(10)

    def position(self, symbol: str) -> int:
        return self.pos

    def avg_price(self, symbol: str) -> Decimal | None:
        return self.avg

    def has_active_order(self, symbol: str) -> bool:
        return self.active

    def bars(self, symbol: str) -> Sequence[Bar]:  # pragma: no cover - 使わない
        return ()

    def entries_allowed(self) -> bool:
        return self.allowed

    def log(self, message: str) -> None:
        self.logs.append(message)


def range_bars(low: float = 1990, high: float = 2010) -> list[Bar]:
    return [
        bar("7203", at(9, m), 2000, high if m == 1 else 2001, low if m == 2 else 1999, 2000)
        for m in range(15)
    ]


def run(strategy: OpeningRangeBreakout, ctx: Ctx, bars: list[Bar]) -> list[Signal]:
    out: list[Signal] = []
    for b in bars:
        out += list(strategy.on_bar(b, ctx))
    return out


def test_skips_when_range_too_narrow_or_wide() -> None:
    for low, high, word in ((1999, 2001, "狭すぎ"), (1900, 2100, "広すぎ")):
        s, ctx = OpeningRangeBreakout(["7203"]), Ctx()
        signals = run(
            s, ctx, [*range_bars(low, high), bar("7203", at(9, 15), 2000, 2200, 1990, 2150)]
        )
        assert signals == []
        assert any(word in m for m in ctx.logs)


def test_no_range_bars() -> None:
    s, ctx = OpeningRangeBreakout(["7203"]), Ctx()
    assert run(s, ctx, [bar("7203", at(9, 20), 2000, 2030, 1999, 2025)]) == []
    assert any("足がありません" in m for m in ctx.logs)


def test_guards_block_entries() -> None:
    breakout = bar("7203", at(9, 15), 2008, 2016, 2006, 2015)
    for setup in ("position", "active", "not_allowed", "deadline", "short_disabled"):
        params = {"allow_short": False} if setup == "short_disabled" else {}
        s, ctx = OpeningRangeBreakout(["7203"], params), Ctx()
        b = breakout
        if setup == "position":
            ctx.pos = 100
        elif setup == "active":
            ctx.active = True
        elif setup == "not_allowed":
            ctx.allowed = False
        elif setup == "deadline":
            b = bar("7203", at(11, 5), 2008, 2016, 2006, 2015)
        else:
            b = bar("7203", at(9, 15), 1992, 1994, 1984, 1985)
        assert run(s, ctx, [*range_bars(), b]) == [], setup


def test_one_trade_per_day_and_reset() -> None:
    s, ctx = OpeningRangeBreakout(["7203"]), Ctx()
    first = run(s, ctx, [*range_bars(), bar("7203", at(9, 15), 2008, 2016, 2006, 2015)])
    assert [x.action for x in first] == [SignalAction.ENTER_LONG]
    assert first[0].metadata == {"stop": "1995.0", "target": "2055.0"}
    assert run(s, ctx, [bar("7203", at(9, 20), 2015, 2030, 2014, 2029)]) == []
    s.on_session_start(at(9).date(), ctx)
    assert s.state("7203").trades == 0
    assert s.on_bar(bar("9999", at(9), 1, 1, 1, 1), ctx) == ()


def test_exits_follow_actual_fill_price() -> None:
    s, ctx = OpeningRangeBreakout(["7203"]), Ctx()
    run(s, ctx, [*range_bars(), bar("7203", at(9, 15), 2008, 2016, 2006, 2015)])
    ctx.pos, ctx.avg = 100, D(2020)  # 想定より5円高く約定した
    fill = Fill("f", "c", "7203", Side.BUY, 100, D(2020), D(0), at(9, 16), PositionEffect.OPEN)
    s.on_fill(fill, ctx)
    assert s.state("7203").stop == D(2000) and s.state("7203").target == D(2060)
    s.on_fill(
        Fill("g", "c", "7203", Side.SELL, 100, D(2030), D(0), at(9, 30), PositionEffect.CLOSE), ctx
    )
    assert s.on_tick(tick("7203", at(9, 17), 2059), ctx) == ()
    ctx.active = True
    assert (
        s.on_tick(tick("7203", at(9, 17), 2061), ctx) == ()
    )  # 手仕舞いの注文が出ている間は重ねない
    ctx.active = False
    (sig,) = s.on_tick(tick("7203", at(9, 17), 2061), ctx)
    assert sig.action is SignalAction.EXIT and "利食い" in sig.reason
    assert s.on_tick(tick("9999", at(9, 17), 1), ctx) == ()


def test_short_exits() -> None:
    s, ctx = OpeningRangeBreakout(["7203"]), Ctx()
    run(s, ctx, [*range_bars(), bar("7203", at(9, 15), 1992, 1994, 1984, 1985)])
    ctx.pos, ctx.avg = -100, D(1985)
    (stop,) = s.on_tick(tick("7203", at(9, 17), 2005), ctx)
    assert "損切り" in stop.reason
    (take,) = s.on_tick(tick("7203", at(9, 17), 1945), ctx)
    assert "利食い" in take.reason
    ctx.pos = 0
    assert s.on_tick(tick("7203", at(9, 17), 1945), ctx) == ()


def test_describe() -> None:
    d = OpeningRangeBreakout(["7203"], {"entry_deadline": "10:30"}).describe()
    assert d["name"] == "orb" and d["params"]["entry_deadline"] == "10:30:00"
    assert OpeningRangeBreakout(["7203"]).params.entry_deadline == time(11, 0)
