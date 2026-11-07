"""注文の状態機械。

注文の状態はボットの中にも持つが、正しい状態は証券会社側にあると考える。
- 状態の変化は決められた遷移だけを受け付け、古い情報による逆戻りは無視する
- 約定は約定番号で重複を除いてから建玉に反映する
- 送信後に応答が取れなかった注文は「状態不明」にして、照合で確定させる
"""

from __future__ import annotations

import itertools
import logging
import secrets
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from daytrade_bot.brokers.base import (
    Broker,
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderStateUnknown,
)
from daytrade_bot.core.models import (
    BrokerOrderSnapshot,
    BrokerOrderUpdate,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
)

log = logging.getLogger(__name__)

S = OrderStatus
_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.PENDING_SUBMIT: frozenset(
        {S.SUBMITTED, S.UNKNOWN, S.REJECTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED}
    ),
    S.UNKNOWN: frozenset(
        {
            S.SUBMITTED,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.PENDING_CANCEL,
            S.CANCELED,
            S.REJECTED,
            S.EXPIRED,
        }
    ),
    S.SUBMITTED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELED, S.REJECTED, S.EXPIRED}
    ),
    S.PARTIALLY_FILLED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELED, S.EXPIRED}
    ),
    S.PENDING_CANCEL: frozenset({S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED}),
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
}

# 状態不明の注文を、何秒たっても証券会社側に見つからなければ「発注されなかった」とみなすか
UNKNOWN_RESOLVE_TIMEOUT = timedelta(seconds=60)


def can_transition(old: OrderStatus, new: OrderStatus) -> bool:
    return new in _ALLOWED[old]


@dataclass(frozen=True, slots=True)
class SubmitResult:
    order: Order
    error: str = ""

    @property
    def accepted(self) -> bool:
        return self.order.status not in (OrderStatus.REJECTED,)


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        on_change: Callable[[Order], None] | None = None,
        id_prefix: str = "",
    ) -> None:
        self._broker = broker
        self._on_change = on_change or (lambda _order: None)
        self._orders: dict[str, Order] = {}
        self._by_broker_id: dict[str, str] = {}
        self._seen_fills: set[str] = set()
        self._fill_notional: dict[str, Decimal] = {}
        self._applied_fill_qty: dict[str, int] = {}
        self._seq = itertools.count(1)
        self._prefix = id_prefix or secrets.token_hex(3)
        # 自分の注文と対応付けられていない更新(発注の応答より先に届いたもの、手動の注文など)
        self.unmatched_updates: deque[BrokerOrderUpdate] = deque(maxlen=1000)
        self._replayed: list[Fill] = []

    # ---- 参照 -------------------------------------------------------------------------------

    def get(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def by_broker_id(self, broker_order_id: str) -> Order | None:
        cid = self._by_broker_id.get(broker_order_id)
        return self._orders.get(cid) if cid else None

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def active_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if o.status.is_active and (symbol is None or o.symbol == symbol)
        ]

    def has_active(self, symbol: str) -> bool:
        return any(o.symbol == symbol and o.status.is_active for o in self._orders.values())

    def pending_qty(self, symbol: str, effect: PositionEffect) -> int:
        """未約定で残っている数量(符号付き)。"""
        total = 0
        for o in self._orders.values():
            if o.symbol == symbol and o.status.is_active and o.request.effect is effect:
                total += o.remaining_qty * o.request.side.sign
        return total

    # ---- 発注 -------------------------------------------------------------------------------

    def new_request(
        self,
        *,
        symbol: str,
        side: Side,
        qty: int,
        effect: PositionEffect,
        now: datetime,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        tag: str = "",
    ) -> OrderRequest:
        client_id = f"{self._prefix}-{now:%Y%m%d}-{next(self._seq):05d}"
        return OrderRequest(
            client_order_id=client_id,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            effect=effect,
            created_at=now,
            limit_price=limit_price,
            tag=tag,
        )

    async def submit(self, req: OrderRequest, now: datetime) -> SubmitResult:
        if req.client_order_id in self._orders:
            raise ValueError(f"同じ注文IDが既にあります: {req.client_order_id}")
        order = Order(request=req, status=OrderStatus.PENDING_SUBMIT, updated_at=now)
        self._orders[req.client_order_id] = order
        self._on_change(order)
        try:
            broker_id = await self._broker.place_order(req)
        except OrderRejected as exc:
            self._set_status(order, OrderStatus.REJECTED, now, reason=str(exc))
            return SubmitResult(order, str(exc))
        except BrokerUnavailable as exc:
            self._set_status(
                order, OrderStatus.REJECTED, now, reason=f"送信できませんでした: {exc}"
            )
            return SubmitResult(order, str(exc))
        except OrderStateUnknown as exc:
            self._set_status(order, OrderStatus.UNKNOWN, now, reason=str(exc))
            return SubmitResult(order, str(exc))
        except BrokerError as exc:
            # 発注の前段(返済する建玉の照会など)で失敗した。注文は送られていない
            self._set_status(order, OrderStatus.REJECTED, now, reason=f"送信前のエラー: {exc}")
            return SubmitResult(order, str(exc))
        except Exception as exc:
            # 想定外の例外では送られたか分からないので、状態不明として照合に任せる
            log.exception("発注中の想定外のエラー")
            self._set_status(order, OrderStatus.UNKNOWN, now, reason=f"想定外のエラー: {exc}")
            return SubmitResult(order, str(exc))
        order.broker_order_id = broker_id
        self._by_broker_id[broker_id] = req.client_order_id
        if order.status is OrderStatus.PENDING_SUBMIT:
            self._set_status(order, OrderStatus.SUBMITTED, now)
        else:
            self._on_change(order)
        # 発注の応答より先に届いていた更新があれば、ここで反映する
        self._replay_unmatched(broker_id)
        return SubmitResult(order)

    async def cancel(self, client_order_id: str, now: datetime) -> bool:
        order = self._orders.get(client_order_id)
        if order is None or not order.status.is_active or order.broker_order_id is None:
            return False
        if order.status is OrderStatus.PENDING_CANCEL:
            return True
        await self._broker.cancel_order(order.broker_order_id)
        if order.status.is_active and can_transition(order.status, OrderStatus.PENDING_CANCEL):
            self._set_status(order, OrderStatus.PENDING_CANCEL, now)
        return True

    # ---- 証券会社からの更新 ------------------------------------------------------------------

    def apply_update(self, update: BrokerOrderUpdate) -> list[Fill]:
        """状態の更新を反映し、新しく判明した約定を返す。"""
        order = self.by_broker_id(update.broker_order_id)
        if order is None:
            self.unmatched_updates.append(update)
            return []
        return self._apply(order, update)

    def _apply(self, order: Order, update: BrokerOrderUpdate) -> list[Fill]:
        new_fills: list[Fill] = []
        for raw in update.fills:
            if raw.fill_id in self._seen_fills:
                continue
            fill = raw
            if (
                fill.client_order_id != order.client_order_id
                or fill.effect is not order.request.effect
            ):
                fill = _with_client_id(fill, order)
            self._seen_fills.add(fill.fill_id)
            new_fills.append(fill)
            cid = order.client_order_id
            self._fill_notional[cid] = (
                self._fill_notional.get(cid, Decimal(0)) + fill.price * fill.qty
            )
            self._applied_fill_qty[cid] = self._applied_fill_qty.get(cid, 0) + fill.qty
        applied_qty = self._applied_fill_qty.get(order.client_order_id, 0)
        filled = max(order.filled_qty, update.filled_qty, applied_qty)
        changed = bool(new_fills) or filled != order.filled_qty
        order.filled_qty = min(filled, order.request.qty)
        if applied_qty:
            order.avg_fill_price = self._fill_notional[order.client_order_id] / applied_qty
        status = update.status
        if status is OrderStatus.PARTIALLY_FILLED and order.filled_qty >= order.request.qty:
            status = OrderStatus.FILLED
        if status != order.status and can_transition(order.status, status):
            self._set_status(order, status, update.time, reason=update.reason or order.reason)
        elif changed:
            order.updated_at = update.time
            self._on_change(order)
        elif status != order.status:
            log.debug("無視した状態遷移 %s: %s -> %s", order.client_order_id, order.status, status)
        return new_fills

    def _replay_unmatched(self, broker_id: str) -> None:
        matched = [u for u in self.unmatched_updates if u.broker_order_id == broker_id]
        if not matched:
            return
        remaining = [u for u in self.unmatched_updates if u.broker_order_id != broker_id]
        self.unmatched_updates.clear()
        self.unmatched_updates.extend(remaining)
        order = self.by_broker_id(broker_id)
        if order is None:  # pragma: no cover - 呼び出し側で登録済み
            return
        for update in matched:
            self._replayed.extend(self._apply(order, update))

    def take_replayed_fills(self) -> list[Fill]:
        """発注の応答や照合より先に届いていた更新から判明した約定を受け取る。

        ``submit`` と ``resolve_unknown`` の後に呼び、返ってきた約定を建玉に反映する。
        """
        fills = self._replayed
        self._replayed = []
        return fills

    # ---- 状態不明の注文の解決 ----------------------------------------------------------------

    def resolve_unknown(
        self, snapshots: Sequence[BrokerOrderSnapshot], now: datetime
    ) -> list[tuple[Order, str]]:
        """状態不明の注文を、証券会社の注文一覧と突き合わせて確定させる。

        Returns:
            (注文, 結果の説明) の一覧
        """
        results: list[tuple[Order, str]] = []
        known = set(self._by_broker_id)
        for order in [o for o in self._orders.values() if o.status is OrderStatus.UNKNOWN]:
            req = order.request
            candidates = [
                s
                for s in snapshots
                if s.broker_order_id not in known
                and s.symbol == req.symbol
                and s.side is req.side
                and s.qty == req.qty
                and (
                    s.received_at is None or s.received_at >= req.created_at - timedelta(seconds=5)
                )
            ]
            if candidates:
                snap = min(candidates, key=lambda s: s.received_at or now)
                known.add(snap.broker_order_id)
                order.broker_order_id = snap.broker_order_id
                self._by_broker_id[snap.broker_order_id] = order.client_order_id
                self._set_status(order, OrderStatus.SUBMITTED, now, reason="照合で受付を確認")
                self._replay_unmatched(snap.broker_order_id)
                results.append((order, f"証券会社側で受付を確認しました({snap.broker_order_id})"))
            elif now - req.created_at > UNKNOWN_RESOLVE_TIMEOUT:
                self._set_status(
                    order, OrderStatus.REJECTED, now, reason="証券会社に注文が見つかりませんでした"
                )
                results.append(
                    (order, "証券会社に注文が見つからないため、発注されなかったとみなしました")
                )
        return results

    def stale_limit_orders(self, now: datetime, timeout_sec: float) -> list[Order]:
        """指値のまま ``timeout_sec`` 秒以上約定していない注文。"""
        limit = timedelta(seconds=timeout_sec)
        return [
            o
            for o in self._orders.values()
            if o.request.order_type is OrderType.LIMIT
            and o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
            and now - o.request.created_at >= limit
        ]

    def prune_finished_before(self, cutoff: datetime) -> int:
        """``cutoff`` より前に作られ、すでに終わった注文を忘れる(日をまたいで動かすとき用)。"""
        old = [
            cid
            for cid, o in self._orders.items()
            if o.status.is_terminal and o.request.created_at < cutoff
        ]
        for cid in old:
            order = self._orders.pop(cid)
            if order.broker_order_id:
                self._by_broker_id.pop(order.broker_order_id, None)
            self._fill_notional.pop(cid, None)
            self._applied_fill_qty.pop(cid, None)
        return len(old)

    def restore(self, orders: Iterable[Order]) -> None:
        """保存してあった注文を読み込む(再起動時)。"""
        for order in orders:
            self._orders[order.client_order_id] = order
            if order.broker_order_id:
                self._by_broker_id[order.broker_order_id] = order.client_order_id

    def _set_status(
        self, order: Order, status: OrderStatus, now: datetime, reason: str = ""
    ) -> None:
        order.status = status
        order.updated_at = now
        if reason:
            order.reason = reason
        self._on_change(order)


def _with_client_id(fill: Fill, order: Order) -> Fill:
    return Fill(
        fill_id=fill.fill_id,
        client_order_id=order.client_order_id,
        symbol=fill.symbol,
        side=fill.side,
        qty=fill.qty,
        price=fill.price,
        fee=fill.fee,
        time=fill.time,
        effect=order.request.effect,
    )
