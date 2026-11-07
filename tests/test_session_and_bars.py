from __future__ import annotations

from datetime import date, time

import pytest

from daytrade_bot.core.bars import BarBuilder, synthesize_ticks
from daytrade_bot.core.session import (
    MarketCalendar,
    Phase,
    SessionTimes,
    TradingRules,
    TradingSchedule,
)
from tests.conftest import TRADING_DAY, D, at, bar, tick


class TestCalendar:
    cal = MarketCalendar()

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 11, 9), True),  # 月曜
            (date(2026, 11, 7), False),  # 土曜
            (date(2026, 11, 8), False),  # 日曜
            (date(2026, 11, 3), False),  # 文化の日
            (date(2026, 11, 23), False),  # 勤労感謝の日
            (date(2026, 12, 31), False),  # 大納会の翌日(年末休場)
            (date(2027, 1, 1), False),
            (date(2027, 1, 4), True),  # 大発会
            (date(2026, 12, 30), True),  # 大納会
        ],
    )
    def test_trading_days(self, day: date, expected: bool) -> None:
        assert self.cal.is_trading_day(day) is expected

    def test_extra_holidays(self) -> None:
        cal = MarketCalendar([date(2026, 11, 9)])
        assert not cal.is_trading_day(date(2026, 11, 9))

    def test_next_trading_day_skips_weekend_and_new_year(self) -> None:
        assert self.cal.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 9)
        assert self.cal.next_trading_day(date(2026, 12, 30)) == date(2027, 1, 4)

    def test_next_trading_day_gives_up(self) -> None:
        cal = MarketCalendar(
            [date(2026, 11, d) for d in range(1, 31)] + [date(2026, 12, d) for d in range(1, 31)]
        )
        with pytest.raises(RuntimeError, match="営業日が見つかりません"):
            cal.next_trading_day(date(2026, 10, 31))

    def test_trading_days_range(self) -> None:
        days = self.cal.trading_days(date(2026, 11, 2), date(2026, 11, 9))
        assert days == [date(2026, 11, d) for d in (2, 4, 5, 6, 9)]


class TestSchedule:
    sched = TradingSchedule(MarketCalendar())

    @pytest.mark.parametrize(
        ("h", "m", "phase"),
        [
            (7, 59, Phase.CLOSED),
            (8, 30, Phase.PRE_OPEN),
            (9, 0, Phase.MORNING),
            (11, 29, Phase.MORNING),
            (11, 30, Phase.LUNCH),
            (12, 30, Phase.AFTERNOON),
            (15, 24, Phase.AFTERNOON),
            (15, 25, Phase.PRE_CLOSE),
            (15, 30, Phase.CLOSED),
        ],
    )
    def test_phases(self, h: int, m: int, phase: Phase) -> None:
        assert self.sched.phase(at(h, m)) is phase

    def test_holiday_phase(self) -> None:
        assert self.sched.phase(at(10, day=date(2026, 11, 7))) is Phase.HOLIDAY
        assert not self.sched.must_flatten(at(15, 21, day=date(2026, 11, 7)))

    def test_entries_and_flatten(self) -> None:
        s = self.sched
        assert s.entries_allowed(at(9, 0))
        assert not s.entries_allowed(at(11, 45))
        assert s.entries_allowed(at(15, 9))
        assert not s.entries_allowed(at(15, 10))
        assert not s.must_flatten(at(15, 19, 59))
        assert s.must_flatten(at(15, 20))

    def test_session_toggles(self) -> None:
        s = TradingSchedule(MarketCalendar(), rules=TradingRules(trade_morning=False))
        assert not s.entries_allowed(at(10))
        assert s.entries_allowed(at(13))
        s2 = TradingSchedule(MarketCalendar(), rules=TradingRules(trade_afternoon=False))
        assert s2.entries_allowed(at(10))
        assert not s2.entries_allowed(at(13))

    def test_rejects_flatten_after_continuous_close(self) -> None:
        with pytest.raises(ValueError, match="15:25"):
            TradingSchedule(MarketCalendar(), rules=TradingRules(flatten_at=time(15, 26)))
        with pytest.raises(ValueError, match="entry_start"):
            TradingSchedule(MarketCalendar(), rules=TradingRules(entry_start=time(8, 0)))

    def test_rejects_bad_session_times(self) -> None:
        with pytest.raises(ValueError, match="順序"):
            SessionTimes(morning_close=time(13, 0))

    def test_helpers(self) -> None:
        assert self.sched.session_open(TRADING_DAY) == at(9)
        assert self.sched.at(TRADING_DAY, time(10, 5)) == at(10, 5)
        assert Phase.MORNING.label == "前場"
        assert Phase.MORNING.is_continuous and not Phase.LUNCH.is_continuous


class TestBarBuilder:
    def test_builds_ohlcv_from_cumulative_volume(self) -> None:
        b = BarBuilder("7203")
        b.seed_volume(TRADING_DAY, 0)
        assert b.on_tick(tick("7203", at(9, 0, 1), 3000, 1000)) == []
        assert b.on_tick(tick("7203", at(9, 0, 20), 3010, 1500)) == []
        assert b.on_tick(tick("7203", at(9, 0, 40), 2995, 1700)) == []
        assert b.current is not None and b.current.close == D(2995)
        bars = b.on_tick(tick("7203", at(9, 1, 2), 3005, 2000))
        assert len(bars) == 1
        first = bars[0]
        assert (first.open, first.high, first.low, first.close) == (
            D(3000),
            D(3010),
            D(2995),
            D(2995),
        )
        assert first.volume == 1700
        assert first.start == at(9, 0) and first.end == at(9, 1)

    def test_first_tick_without_seed_has_unknown_volume(self) -> None:
        b = BarBuilder("7203")
        b.on_tick(tick("7203", at(10, 0, 1), 3000, 50_000))
        b.on_tick(tick("7203", at(10, 0, 2), 3000, 50_300))
        (closed,) = b.flush(at(10, 1, 5))
        assert closed.volume == 300

    def test_flush_respects_grace(self) -> None:
        b = BarBuilder("7203", grace_seconds=2)
        b.on_tick(tick("7203", at(9, 0, 1), 3000))
        assert b.flush(at(9, 1, 1)) == []
        assert len(b.flush(at(9, 1, 2))) == 1
        assert b.flush(at(9, 2, 5)) == []

    def test_late_ticks_are_ignored_but_track_volume(self) -> None:
        b = BarBuilder("7203")
        b.seed_volume(TRADING_DAY)
        b.on_tick(tick("7203", at(9, 0, 1), 3000, 100))
        b.on_tick(tick("7203", at(9, 1, 1), 3001, 200))
        assert b.on_tick(tick("7203", at(9, 0, 59), 2990, 250)) == []
        assert b.late_ticks == 1
        b.on_tick(tick("7203", at(9, 1, 30), 3002, 300))
        (closed,) = b.flush(at(9, 2, 5))
        # 遅れて届いたティックの50株は確定済みの足に入れず、次の足でも二重に数えない
        assert closed.volume == 100 + 50
        assert closed.low == D(3001)

    def test_gap_without_ticks_skips_bars(self) -> None:
        b = BarBuilder("7203")
        b.on_tick(tick("7203", at(11, 29, 50), 3000))
        bars = b.on_tick(tick("7203", at(12, 30, 1), 3010))
        assert [x.start for x in bars] == [at(11, 29)]

    def test_volume_resets_on_new_day(self) -> None:
        b = BarBuilder("7203")
        b.seed_volume(TRADING_DAY)
        b.on_tick(tick("7203", at(15, 0), 3000, 9_000_000))
        b.on_tick(tick("7203", at(9, 0, 1, day=date(2026, 11, 10)), 3000, 500))
        b.on_tick(tick("7203", at(9, 0, 2, day=date(2026, 11, 10)), 3000, 800))
        assert b.current is not None and b.current.volume == 300

    def test_five_minute_bars(self) -> None:
        b = BarBuilder("7203", interval_minutes=5)
        b.on_tick(tick("7203", at(9, 3, 0), 3000))
        assert b.current is not None and b.current.start == at(9, 0) and b.current.end == at(9, 5)

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="interval_minutes"):
            BarBuilder("7203", interval_minutes=7)
        with pytest.raises(ValueError, match="grace"):
            BarBuilder("7203", grace_seconds=-1)
        with pytest.raises(ValueError, match="銘柄が違います"):
            BarBuilder("7203").on_tick(tick("6758", at(9), 100))

    def test_cum_volume_none(self) -> None:
        b = BarBuilder("7203")
        b.on_tick(tick("7203", at(9, 0, 1), 3000, None))
        assert b.current is not None and b.current.volume == 0


class TestSynthesizeTicks:
    def test_bullish_bar_goes_low_first(self) -> None:
        ticks = synthesize_ticks(bar("7203", at(9, 0), 100, 110, 95, 105, v=10), 0)
        assert [t.price for t in ticks] == [D(100), D(95), D(110), D(105)]
        assert [t.cum_volume for t in ticks] == [3, 6, 8, 10]
        assert all(at(9, 0) < t.time < at(9, 1) for t in ticks)

    def test_bearish_bar_goes_high_first(self) -> None:
        ticks = synthesize_ticks(bar("7203", at(9, 0), 100, 110, 95, 98, v=4), 100)
        assert [t.price for t in ticks] == [D(100), D(110), D(95), D(98)]
        assert ticks[-1].cum_volume == 104

    def test_roundtrip_through_builder(self) -> None:
        src = [bar("7203", at(9, m), 100 + m, 105 + m, 99 + m, 102 + m, v=40) for m in range(3)]
        b = BarBuilder("7203")
        b.seed_volume(TRADING_DAY)
        out = []
        cum = 0
        for x in src:
            for t in synthesize_ticks(x, cum):
                out += b.on_tick(t)
            cum += x.volume
        out += b.flush(at(9, 5))
        assert out == src
