"""取引日カレンダーと立会時間。

東証(2024年11月5日以降): 前場 9:00〜11:30、後場 12:30〜15:30。
15:25〜15:30 は売買が成立しない注文受付時間(プレ・クロージング)で、15:30 に板寄せで終値が決まる。
デイトレードでは、ザラ場が終わる 15:25 より前にすべての建玉を閉じる設計にする。
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import jpholiday

from daytrade_bot.core.clock import JST


class Phase(enum.StrEnum):
    HOLIDAY = "holiday"
    PRE_OPEN = "pre_open"
    MORNING = "morning"
    LUNCH = "lunch"
    AFTERNOON = "afternoon"
    PRE_CLOSE = "pre_close"
    CLOSED = "closed"

    @property
    def label(self) -> str:
        return _PHASE_LABELS[self]

    @property
    def is_continuous(self) -> bool:
        """ザラ場(連続売買)中か。"""
        return self in (Phase.MORNING, Phase.AFTERNOON)


_PHASE_LABELS = {
    Phase.HOLIDAY: "休場日",
    Phase.PRE_OPEN: "寄り前",
    Phase.MORNING: "前場",
    Phase.LUNCH: "昼休み",
    Phase.AFTERNOON: "後場",
    Phase.PRE_CLOSE: "引け前の注文受付",
    Phase.CLOSED: "取引終了",
}


class MarketCalendar:
    """東証の営業日判定。土日、祝日・振替休日、年末年始(12/31〜1/3)を休場とする。

    臨時休場などは ``extra_holidays`` で追加できる。
    """

    def __init__(self, extra_holidays: Iterable[date] = ()) -> None:
        self._extra = frozenset(extra_holidays)

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        if (day.month, day.day) in ((12, 31), (1, 1), (1, 2), (1, 3)):
            return False
        if day in self._extra:
            return False
        return not jpholiday.is_holiday(day)

    def next_trading_day(self, day: date) -> date:
        """``day`` より後の最初の営業日。"""
        candidate = day + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError(f"{day} の後30日以内に営業日が見つかりません")

    def trading_days(self, start: date, end: date) -> list[date]:
        """``start`` から ``end`` まで(両端を含む)の営業日。"""
        days: list[date] = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days


@dataclass(frozen=True, slots=True)
class SessionTimes:
    morning_open: time = time(9, 0)
    morning_close: time = time(11, 30)
    afternoon_open: time = time(12, 30)
    continuous_close: time = time(15, 25)
    closing_auction: time = time(15, 30)
    pre_open_start: time = time(8, 0)

    def __post_init__(self) -> None:
        order = (
            self.pre_open_start,
            self.morning_open,
            self.morning_close,
            self.afternoon_open,
            self.continuous_close,
            self.closing_auction,
        )
        if list(order) != sorted(order) or len(set(order)) != len(order):
            raise ValueError("立会時間の順序が正しくありません")


@dataclass(frozen=True, slots=True)
class TradingRules:
    """ボットが新規注文を出してよい時間帯と、強制手仕舞いの時刻。"""

    entry_start: time = time(9, 0)
    entry_cutoff: time = time(15, 10)
    flatten_at: time = time(15, 20)
    trade_morning: bool = True
    trade_afternoon: bool = True


class TradingSchedule:
    def __init__(
        self,
        calendar: MarketCalendar,
        times: SessionTimes | None = None,
        rules: TradingRules | None = None,
    ) -> None:
        self.calendar = calendar
        self.times = times or SessionTimes()
        self.rules = rules or TradingRules()
        r, t = self.rules, self.times
        if not t.morning_open <= r.entry_start < r.entry_cutoff:
            raise ValueError("entry_start は寄付き以降かつ entry_cutoff より前である必要があります")
        if not r.entry_cutoff <= r.flatten_at < t.continuous_close:
            raise ValueError(
                "entry_cutoff <= flatten_at < 15:25(ザラ場終了)を満たす必要があります。"
                "15:25〜15:30 は約定しないため、それより前に手仕舞いしてください"
            )

    def phase(self, now: datetime) -> Phase:
        local = now.astimezone(JST)
        if not self.calendar.is_trading_day(local.date()):
            return Phase.HOLIDAY
        t = local.time()
        s = self.times
        if t < s.pre_open_start:
            return Phase.CLOSED
        if t < s.morning_open:
            return Phase.PRE_OPEN
        if t < s.morning_close:
            return Phase.MORNING
        if t < s.afternoon_open:
            return Phase.LUNCH
        if t < s.continuous_close:
            return Phase.AFTERNOON
        if t < s.closing_auction:
            return Phase.PRE_CLOSE
        return Phase.CLOSED

    def is_continuous(self, now: datetime) -> bool:
        return self.phase(now).is_continuous

    def entries_allowed(self, now: datetime) -> bool:
        """新規エントリーを出してよいか。"""
        phase = self.phase(now)
        if not phase.is_continuous:
            return False
        if phase is Phase.MORNING and not self.rules.trade_morning:
            return False
        if phase is Phase.AFTERNOON and not self.rules.trade_afternoon:
            return False
        t = now.astimezone(JST).time()
        return self.rules.entry_start <= t < self.rules.entry_cutoff

    def must_flatten(self, now: datetime) -> bool:
        """強制手仕舞いの時刻を過ぎたか(営業日のザラ場終了まで)。"""
        local = now.astimezone(JST)
        if not self.calendar.is_trading_day(local.date()):
            return False
        return local.time() >= self.rules.flatten_at

    def session_open(self, day: date) -> datetime:
        return datetime.combine(day, self.times.morning_open, tzinfo=JST)

    def at(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=JST)
