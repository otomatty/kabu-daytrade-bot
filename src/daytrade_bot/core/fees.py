"""手数料とスリッページのモデル(バックテストとペーパートレードで使う)。

本番の手数料は証券会社が約定明細で返す値を使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from daytrade_bot.core.models import PositionEffect, Side


class FeeModel(Protocol):
    def fee(self, side: Side, qty: int, price: Decimal, effect: PositionEffect) -> Decimal:
        """約定1件あたりの手数料(税込、円)。"""
        ...


@dataclass(frozen=True, slots=True)
class ZeroFee:
    """手数料無料(一般信用デイトレなど手数料0円のプラン向け)。"""

    def fee(self, side: Side, qty: int, price: Decimal, effect: PositionEffect) -> Decimal:
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class PercentFee:
    """約定代金に対する割合(税込)。``minimum``/``maximum`` で下限と上限を付けられる。"""

    rate: Decimal
    minimum: Decimal = Decimal(0)
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        if self.rate < 0 or self.minimum < 0:
            raise ValueError("手数料率と下限は0以上である必要があります")
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("上限は下限以上である必要があります")

    def fee(self, side: Side, qty: int, price: Decimal, effect: PositionEffect) -> Decimal:
        amount = (price * qty * self.rate).to_integral_value(rounding=ROUND_FLOOR)
        amount = max(amount, self.minimum)
        if self.maximum is not None:
            amount = min(amount, self.maximum)
        return amount


@dataclass(frozen=True, slots=True)
class FixedFee:
    """約定1件ごとに定額(税込)。"""

    amount: Decimal

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("手数料は0以上である必要があります")

    def fee(self, side: Side, qty: int, price: Decimal, effect: PositionEffect) -> Decimal:
        return self.amount
