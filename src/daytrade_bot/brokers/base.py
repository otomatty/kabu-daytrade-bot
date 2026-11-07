"""ブローカー(発注先)と時価配信のインターフェース。

エンジンはこのインターフェースだけを相手にするので、バックテスト・ペーパー・本番を差し替えられる。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from daytrade_bot.core.models import (
    BrokerOrderSnapshot,
    BrokerOrderUpdate,
    BrokerPosition,
    OrderRequest,
    Tick,
)

UpdateHandler = Callable[[BrokerOrderUpdate], None]
TickHandler = Callable[[Tick], None]


class BrokerError(Exception):
    """ブローカー関連のエラーの基底クラス。"""


class OrderRejected(BrokerError):
    """注文が受け付けられなかった(確実に発注されていない)。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BrokerUnavailable(BrokerError):
    """接続できない(注文は送られていない)。"""


class OrderStateUnknown(BrokerError):
    """送信後に応答が得られず、注文が受け付けられたか分からない。照合で確定させる必要がある。"""


class Broker(Protocol):
    name: str

    def set_update_handler(self, handler: UpdateHandler) -> None:
        """注文状態の変化を受け取る関数を登録する。"""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def place_order(self, req: OrderRequest) -> str:
        """発注して、証券会社の注文番号を返す。

        Raises:
            OrderRejected: 受け付けられなかった
            BrokerUnavailable: 送信できなかった
            OrderStateUnknown: 受け付けられたか分からない
        """
        ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def fetch_orders(self) -> Sequence[BrokerOrderSnapshot]:
        """当日の注文一覧(照合用)。"""
        ...

    async def fetch_positions(self) -> Sequence[BrokerPosition]:
        """建玉の一覧(銘柄ごとに集計)。"""
        ...

    async def on_tick(self, tick: Tick) -> None:
        """時価の更新(シミュレーションの約定判定に使う。本番では何もしない)。"""
        ...


class MarketDataFeed(Protocol):
    name: str

    def set_tick_handler(self, handler: TickHandler) -> None: ...

    async def start(self, symbols: Sequence[str]) -> None: ...

    async def stop(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    @property
    def last_message_at(self) -> datetime | None:
        """最後に配信を受け取った時刻(取引所の時刻ではなく受信した時刻)。"""
        ...
