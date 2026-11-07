"""戦略で使う逐次計算のテクニカル指標。"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from daytrade_bot.core.models import Bar


class SessionVWAP:
    """当日の出来高加重平均価格。足の代表値として (高値+安値+終値)/3 を使う。"""

    def __init__(self) -> None:
        self._pv = Decimal(0)
        self._volume = 0
        self._fallback: Decimal | None = None

    def reset(self) -> None:
        self._pv = Decimal(0)
        self._volume = 0
        self._fallback = None

    def update(self, bar: Bar) -> Decimal:
        typical = (bar.high + bar.low + bar.close) / 3
        self._pv += typical * bar.volume
        self._volume += bar.volume
        self._fallback = typical
        return self.value_or(typical)

    def value_or(self, default: Decimal) -> Decimal:
        if self._volume > 0:
            return self._pv / self._volume
        return self._fallback if self._fallback is not None else default

    @property
    def value(self) -> Decimal | None:
        if self._volume > 0:
            return self._pv / self._volume
        return self._fallback


class EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period は1以上である必要があります")
        self.period = period
        self._alpha = Decimal(2) / (period + 1)
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal:
        self.value = x if self.value is None else self.value + self._alpha * (x - self.value)
        return self.value


class ATR:
    """平均真の値幅(単純平均版)。``period`` 本そろうまでは ``None``。"""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period は1以上である必要があります")
        self.period = period
        self._ranges: deque[Decimal] = deque(maxlen=period)
        self._prev_close: Decimal | None = None

    def update(self, bar: Bar) -> Decimal | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        self._ranges.append(tr)
        return self.value

    @property
    def value(self) -> Decimal | None:
        if len(self._ranges) < self.period:
            return None
        return sum(self._ranges, Decimal(0)) / self.period
