"""東証の呼値(値段の刻み)。

東証は TOPIX500 構成銘柄とそれ以外で呼値の表が異なる。
表は変更されることがあるので、運用前に日本取引所グループの最新の呼値表と照らし合わせること。
"""

from __future__ import annotations

import enum
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


class TickTable(enum.StrEnum):
    STANDARD = "standard"
    TOPIX500 = "topix500"


# (この値段「以下」なら, 呼値)
_STANDARD: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(3_000), Decimal(1)),
    (Decimal(5_000), Decimal(5)),
    (Decimal(30_000), Decimal(10)),
    (Decimal(50_000), Decimal(50)),
    (Decimal(300_000), Decimal(100)),
    (Decimal(500_000), Decimal(500)),
    (Decimal(3_000_000), Decimal(1_000)),
    (Decimal(5_000_000), Decimal(5_000)),
    (Decimal(30_000_000), Decimal(10_000)),
    (Decimal(50_000_000), Decimal(50_000)),
)
_STANDARD_MAX = Decimal(100_000)

_TOPIX500: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(1_000), Decimal("0.1")),
    (Decimal(3_000), Decimal("0.5")),
    (Decimal(10_000), Decimal(1)),
    (Decimal(30_000), Decimal(5)),
    (Decimal(100_000), Decimal(10)),
    (Decimal(300_000), Decimal(50)),
    (Decimal(1_000_000), Decimal(100)),
    (Decimal(3_000_000), Decimal(500)),
    (Decimal(10_000_000), Decimal(1_000)),
    (Decimal(30_000_000), Decimal(5_000)),
)
_TOPIX500_MAX = Decimal(10_000)

# 価格帯の境目で「すぐ上/すぐ下」の帯を調べるための微小量(最小の呼値0.1より十分小さい)
_EPSILON = Decimal("0.0001")


def tick_size(price: Decimal, table: TickTable = TickTable.STANDARD) -> Decimal:
    """``price`` を含む価格帯の呼値を返す(価格帯は「〜円以下」で区切られる)。"""
    if price <= 0:
        raise ValueError(f"価格は正の値である必要があります: {price}")
    rows, top = (
        (_STANDARD, _STANDARD_MAX) if table is TickTable.STANDARD else (_TOPIX500, _TOPIX500_MAX)
    )
    for upper, size in rows:
        if price <= upper:
            return size
    return top


def round_to_tick(price: Decimal, table: TickTable, *, up: bool) -> Decimal:
    """``price`` を呼値に丸める。``up`` が真なら切り上げ、偽なら切り下げ。

    買いの指値は切り下げ、売りの指値は切り上げると、意図より不利な値段になることがない。
    価格帯の上限は必ずその帯の呼値の倍数なので、丸めた結果が隣の帯にはみ出すことはない。
    """
    size = tick_size(price, table)
    steps = (price / size).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    if steps < 1:
        steps = Decimal(1)
    return (steps * size).quantize(size)


def is_on_tick(price: Decimal, table: TickTable) -> bool:
    return price > 0 and (price % tick_size(price, table)) == 0


def shift_ticks(price: Decimal, ticks: int, table: TickTable) -> Decimal:
    """呼値に乗っている ``price`` から ``ticks`` 呼値ぶん動かした値段(負なら下方向)。

    上に動かすときは「すぐ上」の帯、下に動かすときは「すぐ下」の帯の呼値を使う。
    例: 通常銘柄の3,000円の1つ上は3,005円、3,005円の1つ下は3,000円、3,000円の1つ下は2,999円。
    """
    if not is_on_tick(price, table):
        raise ValueError(f"呼値に乗っていない価格です: {price}")
    result = price
    for _ in range(abs(ticks)):
        if ticks > 0:
            result = result + tick_size(result + _EPSILON, table)
        else:
            if result - _EPSILON <= 0:
                raise ValueError("価格が0以下になります")
            size = tick_size(result - _EPSILON, table)
            result = result - size
            if result <= 0:
                raise ValueError("価格が0以下になります")
    return result
