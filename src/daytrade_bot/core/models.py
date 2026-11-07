"""ボット全体で共有するドメインモデル。

価格はすべて ``Decimal`` で扱う(浮動小数点の丸め誤差で損益や呼値判定がずれないようにするため)。
時刻はすべてタイムゾーン付き(JST)の ``datetime``。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal


class Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """建玉数量の符号(買い=+1、売り=-1)。"""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def label(self) -> str:
        return "買" if self is Side.BUY else "売"


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class PositionEffect(enum.StrEnum):
    """新規(建てる)か返済(閉じる)か。"""

    OPEN = "open"
    CLOSE = "close"


class OrderStatus(enum.StrEnum):
    PENDING_SUBMIT = "pending_submit"  # ボット内で作成済み、証券会社へ未送信
    UNKNOWN = "unknown"  # 送信したが応答が取れず、受け付けられたか不明(照合で確定させる)
    SUBMITTED = "submitted"  # 証券会社が受付済み
    PARTIALLY_FILLED = "partially_filled"
    PENDING_CANCEL = "pending_cancel"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_TERMINAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)

_STATUS_LABELS = {
    OrderStatus.PENDING_SUBMIT: "送信前",
    OrderStatus.UNKNOWN: "状態不明",
    OrderStatus.SUBMITTED: "受付",
    OrderStatus.PARTIALLY_FILLED: "一部約定",
    OrderStatus.PENDING_CANCEL: "取消中",
    OrderStatus.FILLED: "全約定",
    OrderStatus.CANCELED: "取消済",
    OrderStatus.REJECTED: "拒否",
    OrderStatus.EXPIRED: "失効",
}


@dataclass(frozen=True, slots=True)
class Tick:
    """約定値(現値)と気配の更新。

    ``bid`` は最良の買い気配(売りたい人が即座に売れる値段)、``ask`` は最良の売り気配。
    kabuステーションAPIの ``BidPrice``/``AskPrice`` とは名前が逆なので、変換はアダプタ側で行う。
    """

    symbol: str
    time: datetime
    price: Decimal
    cum_volume: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.time, "Tick.time")
        if self.price <= 0:
            raise ValueError(f"価格は正の値である必要があります: {self.price}")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        _require_aware(self.start, "Bar.start")
        _require_aware(self.end, "Bar.end")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError(f"OHLC が矛盾しています: {self}")
        if self.end <= self.start:
            raise ValueError("Bar.end は start より後である必要があります")
        if self.volume < 0:
            raise ValueError("出来高は0以上である必要があります")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    effect: PositionEffect
    created_at: datetime
    limit_price: Decimal | None = None
    tag: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "OrderRequest.created_at")
        if self.qty <= 0:
            raise ValueError(f"数量は正の整数である必要があります: {self.qty}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("指値注文には正の指値が必要です")
        elif self.limit_price is not None:
            raise ValueError("成行注文に指値は指定できません")

    @property
    def signed_qty(self) -> int:
        return self.qty * self.side.sign


@dataclass(slots=True)
class Order:
    """ボットが管理する注文の現在の状態。"""

    request: OrderRequest
    status: OrderStatus
    updated_at: datetime
    broker_order_id: str | None = None
    filled_qty: int = 0
    avg_fill_price: Decimal | None = None
    reason: str = ""

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def symbol(self) -> str:
        return self.request.symbol

    @property
    def remaining_qty(self) -> int:
        return max(self.request.qty - self.filled_qty, 0)

    def copy(self) -> Order:
        return replace(self)


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    price: Decimal
    fee: Decimal
    time: datetime
    effect: PositionEffect = PositionEffect.OPEN

    def __post_init__(self) -> None:
        _require_aware(self.time, "Fill.time")
        if self.qty <= 0:
            raise ValueError("約定数量は正の整数である必要があります")
        if self.price <= 0:
            raise ValueError("約定価格は正の値である必要があります")
        if self.fee < 0:
            raise ValueError("手数料は0以上である必要があります")


@dataclass(frozen=True, slots=True)
class BrokerOrderUpdate:
    """証券会社側で注文の状態が変わったことの通知。

    ``filled_qty`` は累計の約定数量。``fills`` には今回新たに判明した約定だけを入れる
    (受け取る側でも ``fill_id`` で重複を除く)。
    """

    broker_order_id: str
    status: OrderStatus
    filled_qty: int
    time: datetime
    fills: tuple[Fill, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    """照合用の、証券会社側の注文一覧の1件。"""

    broker_order_id: str
    symbol: str
    side: Side
    qty: int
    filled_qty: int
    status: OrderStatus
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """証券会社側の建玉(銘柄ごとに集計済み)。``qty`` は買いが正、売りが負。"""

    symbol: str
    qty: int
    avg_price: Decimal


class SignalAction(enum.StrEnum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class Signal:
    """戦略が出す売買の意思表示。発注するかどうかはエンジンとリスク管理が決める。

    ``qty`` が ``None`` なら、エントリーは設定の資金配分で、手仕舞いは全数量で発注する。
    """

    symbol: str
    action: SignalAction
    reason: str = ""
    qty: int | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.qty is not None and self.qty <= 0:
            raise ValueError("Signal.qty は正の整数か None です")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("指値のシグナルには limit_price が必要です")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} はタイムゾーン付きの datetime である必要があります")
