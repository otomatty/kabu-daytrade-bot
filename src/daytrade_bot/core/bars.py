"""ティックから時間足を作る。

kabuステーションの PUSH 配信の売買高(``TradingVolume``)は当日の累計なので、
直前の累計との差分を各足の出来高にする。気配だけが変わった配信は現値時刻が古いままなので、
すでに確定した足より前の時刻のティックは無視する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from daytrade_bot.core.clock import JST
from daytrade_bot.core.models import Bar, Tick


@dataclass(slots=True)
class _Working:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def to_bar(self, symbol: str) -> Bar:
        return Bar(
            symbol=symbol,
            start=self.start,
            end=self.end,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class BarBuilder:
    """1銘柄ぶんの足を組み立てる。"""

    def __init__(self, symbol: str, interval_minutes: int = 1, grace_seconds: float = 2.0) -> None:
        if not 1 <= interval_minutes <= 60 or 60 % interval_minutes != 0:
            raise ValueError("interval_minutes は60を割り切る1〜60の整数である必要があります")
        if grace_seconds < 0:
            raise ValueError("grace_seconds は0以上である必要があります")
        self.symbol = symbol
        self._interval = timedelta(minutes=interval_minutes)
        self._interval_minutes = interval_minutes
        self._grace = timedelta(seconds=grace_seconds)
        self._working: _Working | None = None
        self._last_closed_end: datetime | None = None
        self._last_cum_volume: int | None = None
        self._last_volume_day: date | None = None
        self.late_ticks = 0

    def bucket_start(self, when: datetime) -> datetime:
        local = when.astimezone(JST)
        minute = (local.minute // self._interval_minutes) * self._interval_minutes
        return local.replace(minute=minute, second=0, microsecond=0)

    def seed_volume(self, day: date, cum_volume: int = 0) -> None:
        """``day`` の売買高の累計の起点を与える(寄付き前から動かしたときやバックテストで使う)。

        起点が無いまま途中から受信を始めた場合、最初のティックの出来高は不明として0にする。
        """
        self._last_cum_volume = cum_volume
        self._last_volume_day = day

    def _volume_delta(self, tick: Tick) -> int:
        if tick.cum_volume is None:
            return 0
        day = tick.time.astimezone(JST).date()
        prev = self._last_cum_volume if day == self._last_volume_day else None
        delta = 0 if prev is None else max(tick.cum_volume - prev, 0)
        self._last_cum_volume = tick.cum_volume if prev is None else max(prev, tick.cum_volume)
        self._last_volume_day = day
        return delta

    def on_tick(self, tick: Tick) -> list[Bar]:
        """ティックを取り込み、確定した足を返す(通常は0本か1本)。"""
        if tick.symbol != self.symbol:
            raise ValueError(f"銘柄が違います: {tick.symbol} != {self.symbol}")
        start = self.bucket_start(tick.time)
        if self._last_closed_end is not None and start < self._last_closed_end:
            # 確定済みの足に属する古いティック(気配のみの更新など)。出来高の累計だけは追従する
            self._volume_delta(tick)
            self.late_ticks += 1
            return []
        volume = self._volume_delta(tick)
        closed: list[Bar] = []
        working = self._working
        if working is not None and start >= working.end:
            closed.append(self._close())
            working = None
        if working is None:
            self._working = _Working(
                start=start,
                end=start + self._interval,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=volume,
            )
        else:
            working.high = max(working.high, tick.price)
            working.low = min(working.low, tick.price)
            working.close = tick.price
            working.volume += volume
        return closed

    def flush(self, now: datetime) -> list[Bar]:
        """ティックが来なくても、終了時刻(+猶予)を過ぎた足を確定させる。"""
        working = self._working
        if working is not None and now >= working.end + self._grace:
            return [self._close()]
        return []

    def _close(self) -> Bar:
        working = self._working
        if working is None:  # pragma: no cover - 呼び出し側で確認済み
            raise RuntimeError("確定させる足がありません")
        self._working = None
        self._last_closed_end = working.end
        return working.to_bar(self.symbol)

    @property
    def current(self) -> Bar | None:
        """組み立て中の足(未確定)。"""
        return self._working.to_bar(self.symbol) if self._working else None


def synthesize_ticks(bar: Bar, cum_volume_before: int) -> list[Tick]:
    """足から、足の中の値動きを近似するティック列を作る(足データでのバックテスト用)。

    陽線は 始値→安値→高値→終値、陰線は 始値→高値→安値→終値 の順に動いたとみなす(よく使われる近似)。
    1本の足の中で損切りと利食いの両方に届く場合、実際の順序は分からないので、
    バックテスト結果はこの近似の影響を受ける。より正確に検証したいときは、収集したティックで検証する。
    """
    span = bar.end - bar.start
    points = (
        (bar.open, bar.low, bar.high, bar.close)
        if bar.close >= bar.open
        else (bar.open, bar.high, bar.low, bar.close)
    )
    ticks: list[Tick] = []
    volumes = _split_volume(bar.volume, len(points))
    cum = cum_volume_before
    for i, (price, vol) in enumerate(zip(points, volumes, strict=True)):
        cum += vol
        offset = span * (i + 1) / (len(points) + 1)
        ticks.append(Tick(symbol=bar.symbol, time=bar.start + offset, price=price, cum_volume=cum))
    return ticks


def _split_volume(volume: int, parts: int) -> list[int]:
    base, rest = divmod(volume, parts)
    return [base + (1 if i < rest else 0) for i in range(parts)]
