"""同梱の戦略。自作の戦略は ``パッケージ.モジュール:クラス名`` で設定ファイルから指定できる。"""

from __future__ import annotations

from typing import Any

from daytrade_bot.core.strategy import Strategy
from daytrade_bot.strategies.orb import OpeningRangeBreakout

REGISTRY: dict[str, type[Strategy[Any]]] = {
    OpeningRangeBreakout.name: OpeningRangeBreakout,
}

__all__ = ["REGISTRY", "OpeningRangeBreakout"]
