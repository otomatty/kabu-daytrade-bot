from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from daytrade_bot.core.ticksize import TickTable, is_on_tick, round_to_tick, shift_ticks, tick_size

S, T = TickTable.STANDARD, TickTable.TOPIX500


@pytest.mark.parametrize(
    ("price", "table", "expected"),
    [
        ("1", S, "1"),
        ("3000", S, "1"),
        ("3001", S, "5"),
        ("5000", S, "5"),
        ("5001", S, "10"),
        ("30000", S, "10"),
        ("30001", S, "50"),
        ("50001", S, "100"),
        ("300001", S, "500"),
        ("500001", S, "1000"),
        ("3000001", S, "5000"),
        ("5000001", S, "10000"),
        ("30000001", S, "50000"),
        ("50000001", S, "100000"),
        ("999.9", T, "0.1"),
        ("1000", T, "0.1"),
        ("1000.5", T, "0.5"),
        ("3000", T, "0.5"),
        ("3001", T, "1"),
        ("10001", T, "5"),
        ("30001", T, "10"),
        ("100001", T, "50"),
        ("300001", T, "100"),
        ("1000001", T, "500"),
        ("3000001", T, "1000"),
        ("10000001", T, "5000"),
        ("30000001", T, "10000"),
    ],
)
def test_tick_size_table(price: str, table: TickTable, expected: str) -> None:
    assert tick_size(Decimal(price), table) == Decimal(expected)


def test_tick_size_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="正の値"):
        tick_size(Decimal(0))


@pytest.mark.parametrize(
    ("price", "up", "expected"),
    [
        ("3002", True, "3005"),
        ("3002", False, "3000"),
        ("2999.5", True, "3000"),
        ("2999.5", False, "2999"),
        ("0.3", False, "1"),  # 1円未満は最小の呼値に切り上げる
        ("5000", True, "5000"),
    ],
)
def test_round_to_tick_standard(price: str, up: bool, expected: str) -> None:
    assert round_to_tick(Decimal(price), S, up=up) == Decimal(expected)


def test_round_to_tick_topix500_keeps_decimal_places() -> None:
    assert str(round_to_tick(Decimal("1234.56"), T, up=False)) == "1234.5"
    assert str(round_to_tick(Decimal("1234.56"), T, up=True)) == "1235.0"
    assert str(round_to_tick(Decimal("3000"), S, up=True)) == "3000"


@pytest.mark.parametrize(
    ("price", "ticks", "expected"),
    [
        ("3000", 1, "3005"),
        ("3005", -1, "3000"),
        ("3000", -1, "2999"),
        ("2999", 2, "3005"),
        ("5000", 1, "5010"),
        ("100", 0, "100"),
    ],
)
def test_shift_ticks_across_band_boundaries(price: str, ticks: int, expected: str) -> None:
    assert shift_ticks(Decimal(price), ticks, S) == Decimal(expected)


def test_shift_ticks_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="呼値に乗っていない"):
        shift_ticks(Decimal("3002"), 1, S)
    with pytest.raises(ValueError, match="0以下"):
        shift_ticks(Decimal("1"), -1, S)


def test_is_on_tick() -> None:
    assert is_on_tick(Decimal("3005"), S)
    assert not is_on_tick(Decimal("3002"), S)
    assert not is_on_tick(Decimal("0"), S)


prices = st.decimals(min_value=Decimal("0.1"), max_value=Decimal("60000000"), places=1)


@given(prices, st.sampled_from([S, T]), st.booleans())
def test_round_to_tick_properties(price: Decimal, table: TickTable, up: bool) -> None:
    rounded = round_to_tick(price, table, up=up)
    assert is_on_tick(rounded, table)
    if up:
        assert rounded >= price
    elif price >= tick_size(price, table):
        assert rounded <= price
    # 丸めた結果と元の価格の差は1呼値未満
    assert abs(rounded - price) < tick_size(max(rounded, price), table)


@given(prices, st.sampled_from([S, T]), st.integers(min_value=1, max_value=5))
def test_shift_up_then_down_roundtrips(price: Decimal, table: TickTable, n: int) -> None:
    base = round_to_tick(price, table, up=True)
    assert shift_ticks(shift_ticks(base, n, table), -n, table) == base
