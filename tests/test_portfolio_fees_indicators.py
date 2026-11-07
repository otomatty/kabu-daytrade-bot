from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from daytrade_bot.core.fees import FixedFee, PercentFee, ZeroFee
from daytrade_bot.core.indicators import ATR, EMA, SessionVWAP
from daytrade_bot.core.models import Fill, PositionEffect, Side
from daytrade_bot.core.portfolio import Portfolio
from tests.conftest import D, at, bar

_seq = iter(range(1_000_000))


def fill(
    side: Side,
    qty: int,
    price: str | int,
    fee: str | int = 0,
    minute: int = 0,
    symbol: str = "7203",
) -> Fill:
    return Fill(
        fill_id=f"f{next(_seq)}",
        client_order_id="c",
        symbol=symbol,
        side=side,
        qty=qty,
        price=D(price),
        fee=D(fee),
        time=at(9, minute),
    )


class TestPortfolio:
    def test_long_round_trip_with_fees(self) -> None:
        p = Portfolio()
        assert p.apply_fill(fill(Side.BUY, 100, 3000, fee=50)) == 0
        assert p.qty("7203") == 100
        p.mark("7203", D(3100))
        assert p.unrealized("7203") == D(10_000)
        assert p.exposure("7203") == D(310_000)
        assert p.apply_fill(fill(Side.SELL, 100, 3050, fee=50, minute=5)) == D(5000)
        assert p.qty("7203") == 0
        assert p.realized_pnl == D(5000) and p.fees == D(100)
        assert p.net_pnl() == D(4900)
        (trip,) = p.round_trips
        assert trip.side is Side.BUY and trip.qty == 100
        assert trip.entry_price == D(3000) and trip.exit_price == D(3050)
        assert trip.pnl == D(4900) and trip.fees == D(100) and trip.is_win
        assert trip.closed_at - trip.opened_at == timedelta(minutes=5)

    def test_short_round_trip(self) -> None:
        p = Portfolio()
        p.apply_fill(fill(Side.SELL, 200, 1000))
        assert p.qty("7203") == -200
        p.mark("7203", D(990))
        assert p.unrealized("7203") == D(2000)
        assert p.apply_fill(fill(Side.BUY, 200, 1010)) == D(-2000)
        (trip,) = p.round_trips
        assert trip.side is Side.SELL and trip.pnl == D(-2000) and not trip.is_win

    def test_scale_in_averages_and_partial_close_keeps_average(self) -> None:
        p = Portfolio()
        p.apply_fill(fill(Side.BUY, 100, 1000))
        p.apply_fill(fill(Side.BUY, 300, 1100))
        assert p.position("7203").avg_price == D(1075)
        p.apply_fill(fill(Side.SELL, 100, 1200))
        assert p.qty("7203") == 300 and p.position("7203").avg_price == D(1075)
        assert p.realized_pnl == D(12_500)
        assert not p.round_trips
        p.apply_fill(fill(Side.SELL, 300, 1000))
        (trip,) = p.round_trips
        assert trip.qty == 400
        assert trip.entry_price == D(1075)
        assert trip.exit_price == D(1050)
        assert trip.pnl == D(12_500) + D(-22_500)

    def test_flip_position(self) -> None:
        p = Portfolio()
        p.apply_fill(fill(Side.BUY, 100, 1000, fee=10))
        p.apply_fill(fill(Side.SELL, 300, 1100, fee=30))
        assert p.qty("7203") == -200
        assert p.position("7203").avg_price == D(1100)
        (trip,) = p.round_trips
        assert trip.pnl == D(10_000) - D(10) - D(10)  # 新規の手数料 + 返済分の手数料
        p.apply_fill(fill(Side.BUY, 200, 1050))
        second = p.round_trips[1]
        assert second.side is Side.SELL and second.pnl == D(10_000) - D(20)
        assert p.fees == D(40)

    def test_set_position_and_reset_day(self) -> None:
        p = Portfolio()
        p.set_position("7203", -100, D(2000), at(9))
        assert p.qty("7203") == -100 and p.open_symbols() == ["7203"]
        assert p.last_prices["7203"] == D(2000)
        p.apply_fill(fill(Side.BUY, 100, 1990))
        assert p.round_trips[0].pnl == D(1000)
        p.reset_day()
        assert p.realized_pnl == 0 and p.fees == 0 and not p.round_trips
        p.set_position("7203", 0, D(1), at(9))
        assert p.qty("7203") == 0 and p.position("7203").avg_price == 0

    def test_mark_validation_and_empty(self) -> None:
        p = Portfolio()
        with pytest.raises(ValueError, match="正の値"):
            p.mark("7203", D(0))
        assert p.unrealized("9999") == 0
        assert p.exposure("9999") == 0
        assert p.total_exposure() == 0
        assert p.position("7203").side is None

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.sampled_from([Side.BUY, Side.SELL]),
                st.integers(min_value=1, max_value=10).map(lambda n: n * 100),
                st.integers(min_value=900, max_value=1100),
                st.integers(min_value=0, max_value=200),
            ),
            min_size=1,
            max_size=30,
        ),
        st.integers(min_value=900, max_value=1100),
    )
    def test_pnl_equals_cash_flow_invariant(
        self, fills: list[tuple[Side, int, int, int]], last: int
    ) -> None:
        """損益 = 現金の出入り + 建玉の時価 - 手数料 が、どんな約定の並びでも成り立つ。"""
        p = Portfolio()
        cash = Decimal(0)
        qty = 0
        for side, q, price, fee in fills:
            p.apply_fill(fill(side, q, price, fee))
            cash -= Decimal(q * side.sign * price)
            qty += q * side.sign
        p.mark("7203", D(last))
        assert p.qty("7203") == qty
        expected = cash + qty * D(last) - p.fees
        assert abs(p.net_pnl() - expected) < Decimal("0.000001")
        # 完了した取引の損益の合計は、建玉が0なら全体の損益と一致する
        if qty == 0:
            assert abs(sum(t.pnl for t in p.round_trips) - p.net_pnl()) < Decimal("0.000001")


class TestFees:
    def test_zero(self) -> None:
        assert ZeroFee().fee(Side.BUY, 100, D(1000), PositionEffect.OPEN) == 0

    def test_percent_with_bounds(self) -> None:
        m = PercentFee(D("0.001"), minimum=D(55), maximum=D(500))
        assert m.fee(Side.BUY, 100, D(1000), PositionEffect.OPEN) == D(100)
        assert m.fee(Side.BUY, 1, D(100), PositionEffect.OPEN) == D(55)
        assert m.fee(Side.BUY, 10_000, D(1000), PositionEffect.OPEN) == D(500)

    def test_fixed_and_validation(self) -> None:
        assert FixedFee(D(99)).fee(Side.SELL, 1, D(1), PositionEffect.CLOSE) == D(99)
        with pytest.raises(ValueError, match="0以上"):
            FixedFee(D(-1))
        with pytest.raises(ValueError, match="0以上"):
            PercentFee(D(-1))
        with pytest.raises(ValueError, match="上限"):
            PercentFee(D("0.1"), minimum=D(10), maximum=D(5))


class TestIndicators:
    def test_vwap(self) -> None:
        v = SessionVWAP()
        assert v.value is None
        assert v.value_or(D(1)) == D(1)
        v.update(bar("7203", at(9), 100, 110, 90, 100, v=0))
        assert v.value == D(100)  # 出来高0の間は代表値
        v.update(bar("7203", at(9, 1), 100, 103, 97, 100, v=100))
        v.update(bar("7203", at(9, 2), 100, 106, 100, 103, v=300))
        assert v.value == (D(100) * 100 + D(103) * 300) / 400
        v.reset()
        assert v.value is None

    def test_ema(self) -> None:
        e = EMA(3)
        assert e.update(D(10)) == D(10)
        assert e.update(D(20)) == D(15)
        with pytest.raises(ValueError, match="period"):
            EMA(0)

    def test_atr(self) -> None:
        a = ATR(2)
        assert a.update(bar("7203", at(9), 100, 110, 95, 105)) is None
        assert a.update(bar("7203", at(9, 1), 105, 108, 100, 101)) == D("11.5")
        # 前の終値からの窓開けを真の値幅に含める
        assert a.update(bar("7203", at(9, 2), 120, 125, 119, 124)) == (D(8) + D(24)) / 2
        with pytest.raises(ValueError, match="period"):
            ATR(0)
