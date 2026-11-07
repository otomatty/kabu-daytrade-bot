"""オープニングレンジ・ブレイクアウト(ORB)戦略。

寄付きから ``range_minutes`` 分間の高値・安値(オープニングレンジ)を記録し、
その後の足の終値がレンジの上に抜けたら買い、下に抜けたら売り(空売り)で入る。
損切りはレンジの反対側(``stop_fraction`` で調整)、利食いは損切り幅の ``take_profit_r`` 倍。
どちらにも届かなければ、エンジンの強制手仕舞い時刻に決済される。

これはボットの仕組みを確かめるためのサンプル戦略で、利益が出ることを保証するものではない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from daytrade_bot.core.models import Bar, Fill, PositionEffect, Signal, SignalAction, Tick
from daytrade_bot.core.strategy import Strategy, StrategyContext, StrategyParams

_SESSION_OPEN = time(9, 0)


class ORBParams(StrategyParams):
    range_minutes: int = Field(default=15, ge=1, le=120, description="レンジを測る分数")
    breakout_buffer_pct: float = Field(
        default=0.0, ge=0.0, le=5.0, description="レンジを何%超えたらブレイクとみなすか"
    )
    stop_fraction: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="損切りの位置。1.0=レンジの反対側、0.5=レンジの中央",
    )
    take_profit_r: float = Field(default=2.0, gt=0.0, le=20.0, description="利食い=損切り幅×R")
    max_trades_per_day: int = Field(default=1, ge=1, le=20, description="1銘柄1日あたりの回数")
    allow_short: bool = Field(default=True, description="下抜けで空売りするか(信用取引が必要)")
    entry_deadline: time = Field(
        default=time(11, 0), description="これ以降は新規ブレイクを狙わない"
    )
    min_range_pct: float = Field(default=0.2, ge=0.0, description="レンジが狭すぎる日は見送る(%)")
    max_range_pct: float = Field(default=3.0, gt=0.0, description="レンジが広すぎる日は見送る(%)")

    @model_validator(mode="after")
    def _check_range(self) -> ORBParams:
        if self.min_range_pct >= self.max_range_pct:
            raise ValueError("min_range_pct は max_range_pct より小さくしてください")
        return self


@dataclass(slots=True)
class _DayState:
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    range_done: bool = False
    skip_reason: str = ""
    trades: int = 0
    stop_offset: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None


class OpeningRangeBreakout(Strategy[ORBParams]):
    name = "orb"
    description = "寄付き後のレンジを抜けた方向に乗るブレイクアウト戦略"
    Params = ORBParams

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        super().__init__(symbols, params)
        self._state: dict[str, _DayState] = {s: _DayState() for s in self.symbols}

    def on_session_start(self, day: date, ctx: StrategyContext) -> None:
        self._state = {s: _DayState() for s in self.symbols}

    def state(self, symbol: str) -> _DayState:
        return self._state[symbol]

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            return ()
        p = self.params
        session_open = bar.start.replace(
            hour=_SESSION_OPEN.hour, minute=_SESSION_OPEN.minute, second=0, microsecond=0
        )
        range_end = session_open + timedelta(minutes=p.range_minutes)

        if bar.start < range_end:
            st.range_high = bar.high if st.range_high is None else max(st.range_high, bar.high)
            st.range_low = bar.low if st.range_low is None else min(st.range_low, bar.low)
            return ()

        if not st.range_done:
            st.range_done = True
            self._evaluate_range(bar.symbol, st, ctx)

        if st.skip_reason or st.range_high is None or st.range_low is None:
            return ()
        position = ctx.position(bar.symbol)
        if position != 0 or ctx.has_active_order(bar.symbol):
            return ()
        if st.trades >= p.max_trades_per_day or bar.end.time() > p.entry_deadline:
            return ()
        if not ctx.entries_allowed():
            return ()

        buffer = Decimal(str(p.breakout_buffer_pct)) / 100
        upper = st.range_high * (1 + buffer)
        lower = st.range_low * (1 - buffer)
        stop_offset = (st.range_high - st.range_low) * Decimal(str(p.stop_fraction))

        if bar.close > upper:
            st.stop_offset = stop_offset
            self._set_exits(st, bar.close, long=True)
            return self._enter(st, bar, SignalAction.ENTER_LONG, "レンジ上抜け")
        if bar.close < lower and p.allow_short:
            st.stop_offset = stop_offset
            self._set_exits(st, bar.close, long=False)
            return self._enter(st, bar, SignalAction.ENTER_SHORT, "レンジ下抜け")
        return ()

    def _set_exits(self, st: _DayState, entry: Decimal, *, long: bool) -> None:
        if st.stop_offset is None:  # pragma: no cover - 呼び出し側で設定済み
            return
        r_mult = Decimal(str(self.params.take_profit_r))
        direction = 1 if long else -1
        st.stop = entry - st.stop_offset * direction
        st.target = entry + st.stop_offset * r_mult * direction

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        """実際の約定値段を基準に、損切りと利食いの位置を置き直す。"""
        st = self._state.get(fill.symbol)
        if st is None or fill.effect is not PositionEffect.OPEN:
            return
        avg = ctx.avg_price(fill.symbol)
        position = ctx.position(fill.symbol)
        if avg is None or position == 0:
            return
        self._set_exits(st, avg, long=position > 0)

    def _enter(self, st: _DayState, bar: Bar, action: SignalAction, label: str) -> Sequence[Signal]:
        st.trades += 1
        return (
            Signal(
                symbol=bar.symbol,
                action=action,
                reason=(
                    f"{label}(レンジ {st.range_low}〜{st.range_high}、終値 {bar.close}、"
                    f"損切り {_fmt(st.stop)}、利食い {_fmt(st.target)})"
                ),
                metadata={"stop": _fmt(st.stop), "target": _fmt(st.target)},
            ),
        )

    def _evaluate_range(self, symbol: str, st: _DayState, ctx: StrategyContext) -> None:
        if st.range_high is None or st.range_low is None:
            st.skip_reason = "レンジ期間に足がありません"
            ctx.log(f"{symbol}: {st.skip_reason}")
            return
        mid = (st.range_high + st.range_low) / 2
        width_pct = float((st.range_high - st.range_low) / mid * 100)
        if width_pct < self.params.min_range_pct:
            st.skip_reason = f"レンジが狭すぎます({width_pct:.2f}%)"
        elif width_pct > self.params.max_range_pct:
            st.skip_reason = f"レンジが広すぎます({width_pct:.2f}%)"
        if st.skip_reason:
            ctx.log(f"{symbol}: 本日は見送り。{st.skip_reason}")
        else:
            ctx.log(f"{symbol}: レンジ確定 {st.range_low}〜{st.range_high}({width_pct:.2f}%)")

    def on_tick(self, tick: Tick, ctx: StrategyContext) -> Sequence[Signal]:
        st = self._state.get(tick.symbol)
        if st is None or st.stop is None or st.target is None:
            return ()
        position = ctx.position(tick.symbol)
        # 手仕舞いの注文が出ている間は重ねて出さない(拒否された場合は次のティックで出し直す)
        if position == 0 or ctx.has_active_order(tick.symbol):
            return ()
        price = tick.price
        reason = ""
        if position > 0:
            if price <= st.stop:
                reason = f"損切り({price} <= {st.stop})"
            elif price >= st.target:
                reason = f"利食い({price} >= {st.target})"
        elif price >= st.stop:
            reason = f"損切り({price} >= {st.stop})"
        elif price <= st.target:
            reason = f"利食い({price} <= {st.target})"
        if not reason:
            return ()
        return (Signal(symbol=tick.symbol, action=SignalAction.EXIT, reason=reason),)


def _fmt(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value.quantize(Decimal('0.1')):f}"
