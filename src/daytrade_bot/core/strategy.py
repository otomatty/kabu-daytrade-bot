"""戦略のインターフェース。

戦略は「シグナルを出すだけ」で、発注は直接しない。シグナルを発注に変えるか、数量をいくつにするか、
拒否するかはエンジンとリスク管理が決める。こうしておくと、戦略にバグがあっても損失は
リスク管理の上限で止まり、同じ戦略をバックテスト・ペーパー・本番でそのまま動かせる。
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from daytrade_bot.core.models import Bar, Fill, Signal, Tick


class StrategyContext(Protocol):
    """戦略から見えるボットの状態(読み取り専用)。"""

    def now(self) -> datetime: ...

    def position(self, symbol: str) -> int:
        """建玉数量(買いが正、売りが負)。"""
        ...

    def avg_price(self, symbol: str) -> Decimal | None: ...

    def has_active_order(self, symbol: str) -> bool:
        """未約定の注文があるか。"""
        ...

    def bars(self, symbol: str) -> Sequence[Bar]:
        """当日の確定済みの足(古い順)。"""
        ...

    def entries_allowed(self) -> bool:
        """今、新規エントリーが許される時間帯か。"""
        ...

    def log(self, message: str) -> None: ...


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ParamsT = TypeVar("ParamsT", bound=StrategyParams)


class Strategy(ABC, Generic[ParamsT]):
    """戦略の基底クラス。``Params`` にパラメータの型を定義する。"""

    name: ClassVar[str]
    description: ClassVar[str] = ""
    Params: type[ParamsT]

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        if not symbols:
            raise ValueError("銘柄が1つもありません")
        self.symbols = tuple(symbols)
        self.params: ParamsT = self.Params.model_validate(dict(params or {}))

    def on_session_start(self, day: date, ctx: StrategyContext) -> None:
        """取引日の始まりに1回呼ばれる。日ごとの状態をここでリセットする。"""

    def on_tick(self, tick: Tick, ctx: StrategyContext) -> Sequence[Signal]:
        """ティックごとに呼ばれる(損切り・利食いの判定など)。"""
        return ()

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        """足が確定するたびに呼ばれる。"""

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        """自分の注文が約定したときに呼ばれる。"""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params.model_dump(mode="json"),
        }


def load_strategy_class(name: str) -> type[Strategy[Any]]:
    """登録名(例: ``orb``)か ``パッケージ.モジュール:クラス名`` から戦略クラスを得る。"""
    from daytrade_bot.strategies import REGISTRY  # noqa: PLC0415 - 循環importを避ける

    if name in REGISTRY:
        return REGISTRY[name]
    if ":" not in name:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"戦略 '{name}' が見つかりません。登録済み: {known}")
    module_name, _, class_name = name.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise TypeError(f"{name} は Strategy のサブクラスではありません")
    return cls
