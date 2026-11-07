"""約定をシミュレーションするブローカー(バックテストとペーパートレードで共通)。

- 注文は、発注の次に届いたその銘柄のティックで約定判定する(発注から約定までの遅れの近似)
- 成行の買いは最良売り気配(無ければ現値)から、売りは最良買い気配(無ければ現値)から、
  ``slippage_ticks`` 呼値だけ不利な値段で約定する
- 指値は、値段が指値を突き抜けたら(``limit_fill_on_touch`` なら同値でも)指値で約定する
- 部分約定は扱わない(全数量が一度に約定する)
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from daytrade_bot.brokers.base import OrderRejected, UpdateHandler
from daytrade_bot.core.clock import Clock
from daytrade_bot.core.fees import FeeModel
from daytrade_bot.core.models import (
    BrokerOrderSnapshot,
    BrokerOrderUpdate,
    BrokerPosition,
    Fill,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Tick,
)
from daytrade_bot.core.ticksize import TickTable, is_on_tick, round_to_tick, shift_ticks


@dataclass(slots=True)
class _SimOrder:
    broker_id: str
    req: OrderRequest
    status: OrderStatus = OrderStatus.SUBMITTED
    filled_qty: int = 0
    # 発注した時点で指値がすでに市場価格に届いていた(即座に約定する指値)か
    marketable: bool = False


@dataclass(slots=True)
class _SimPosition:
    qty: int = 0
    avg_price: Decimal = Decimal(0)


@dataclass(slots=True)
class SimStats:
    orders: int = 0
    fills: int = 0
    rejects: int = 0
    cancels: int = 0
    slippage_cost: Decimal = field(default_factory=lambda: Decimal(0))


class SimulatedBroker:
    def __init__(
        self,
        clock: Clock,
        fee_model: FeeModel,
        tick_table_for: Callable[[str], TickTable],
        *,
        slippage_ticks: int = 1,
        limit_fill_on_touch: bool = False,
        name: str = "sim",
        allow_short: bool = True,
    ) -> None:
        if slippage_ticks < 0:
            raise ValueError("slippage_ticks は0以上である必要があります")
        self.name = name
        self._clock = clock
        self._fees = fee_model
        self._table_for = tick_table_for
        self._slippage = slippage_ticks
        self._touch = limit_fill_on_touch
        self._allow_short = allow_short
        self._handler: UpdateHandler = lambda _u: None
        self._orders: dict[str, _SimOrder] = {}
        self._positions: dict[str, _SimPosition] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._seq = itertools.count(1)
        self.stats = SimStats()

    def set_update_handler(self, handler: UpdateHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def place_order(self, req: OrderRequest) -> str:
        pos = self._positions.get(req.symbol, _SimPosition())
        reserved = sum(
            o.req.qty - o.filled_qty
            for o in self._orders.values()
            if o.req.symbol == req.symbol
            and o.status.is_active
            and o.req.effect is PositionEffect.CLOSE
        )
        if req.effect is PositionEffect.CLOSE:
            if pos.qty == 0 or (pos.qty > 0) == (req.side is Side.BUY):
                self.stats.rejects += 1
                raise OrderRejected("返済する建玉がありません")
            if req.qty + reserved > abs(pos.qty):
                self.stats.rejects += 1
                raise OrderRejected("返済数量が建玉を超えています")
        elif req.side is Side.SELL and not self._allow_short:
            self.stats.rejects += 1
            raise OrderRejected("空売りはできません")
        broker_id = f"{self.name.upper()}{next(self._seq):08d}"
        marketable = False
        last = self._last_prices.get(req.symbol)
        if req.order_type is OrderType.LIMIT and req.limit_price is not None and last is not None:
            marketable = (
                req.limit_price >= last if req.side is Side.BUY else req.limit_price <= last
            )
        self._orders[broker_id] = _SimOrder(broker_id, req, marketable=marketable)
        self.stats.orders += 1
        return broker_id

    async def cancel_order(self, broker_order_id: str) -> None:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise OrderRejected("該当する注文がありません")
        if not order.status.is_active:
            raise OrderRejected("取り消せない注文です")
        order.status = OrderStatus.CANCELED
        self.stats.cancels += 1
        self._emit(order, ())

    async def fetch_orders(self) -> Sequence[BrokerOrderSnapshot]:
        return [
            BrokerOrderSnapshot(
                broker_order_id=o.broker_id,
                symbol=o.req.symbol,
                side=o.req.side,
                qty=o.req.qty,
                filled_qty=o.filled_qty,
                status=o.status,
                received_at=o.req.created_at,
            )
            for o in self._orders.values()
        ]

    async def fetch_positions(self) -> Sequence[BrokerPosition]:
        return [
            BrokerPosition(symbol=s, qty=p.qty, avg_price=p.avg_price)
            for s, p in self._positions.items()
            if p.qty != 0
        ]

    async def on_tick(self, tick: Tick) -> None:
        self._last_prices[tick.symbol] = tick.price
        for order in [
            o for o in self._orders.values() if o.req.symbol == tick.symbol and o.status.is_active
        ]:
            price = self._fill_price(order, tick)
            if price is not None:
                self._fill(order, price, tick)
            else:
                order.marketable = False

    def settle_at_last(self, reason: str = "取引終了後の精算") -> int:
        """取引終了後に残った注文を精算する(バックテスト用)。

        返済の注文は最後の値段で約定させ、新規の注文は取り消す。約定させた件数を返す。
        """
        filled = 0
        for order in [o for o in self._orders.values() if o.status.is_active]:
            symbol = order.req.symbol
            if order.req.effect is PositionEffect.OPEN or symbol not in self._last_prices:
                order.status = OrderStatus.CANCELED
                self.stats.cancels += 1
                self._emit(order, (), reason=reason)
                continue
            last = self._last_prices[symbol]
            self._fill(order, last, Tick(symbol=symbol, time=self._clock.now(), price=last))
            filled += 1
        return filled

    # ---- 内部 -------------------------------------------------------------------------------

    def _fill_price(self, order: _SimOrder, tick: Tick) -> Decimal | None:
        req = order.req
        table = self._table_for(req.symbol)
        if req.order_type is OrderType.MARKET:
            if req.side is Side.BUY:
                base = tick.ask if tick.ask is not None else tick.price
                return _slip(base, self._slippage, table, up=True)
            base = tick.bid if tick.bid is not None else tick.price
            return _slip(base, self._slippage, table, up=False)
        limit = req.limit_price
        if limit is None:  # pragma: no cover - OrderRequest で検証済み
            return None
        price = tick.price
        if req.side is Side.BUY:
            if order.marketable:
                # 発注時点で約定できる指値は、その時の値段(指値より有利なら有利な方)で約定する
                market = tick.ask if tick.ask is not None else price
                return min(limit, market) if market <= limit else None
            crossed = price <= limit if self._touch else price < limit
        else:
            if order.marketable:
                market = tick.bid if tick.bid is not None else price
                return max(limit, market) if market >= limit else None
            crossed = price >= limit if self._touch else price > limit
        # 板に並んでいた指値は、値段が指値を通過したら指値で約定する
        return limit if crossed else None

    def _fill(self, order: _SimOrder, price: Decimal, tick: Tick) -> None:
        req = order.req
        qty = req.qty - order.filled_qty
        fee = self._fees.fee(req.side, qty, price, req.effect)
        fill = Fill(
            fill_id=f"{order.broker_id}-{order.filled_qty + 1}",
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=qty,
            price=price,
            fee=fee,
            time=self._clock.now(),
            effect=req.effect,
        )
        order.filled_qty += qty
        order.status = OrderStatus.FILLED
        self._apply_position(req.symbol, req.side, qty, price)
        self.stats.fills += 1
        if req.order_type is OrderType.MARKET:
            self.stats.slippage_cost += abs(price - tick.price) * qty
        self._emit(order, (fill,))

    def _apply_position(self, symbol: str, side: Side, qty: int, price: Decimal) -> None:
        pos = self._positions.setdefault(symbol, _SimPosition())
        signed = qty * side.sign
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            new_qty = pos.qty + signed
            pos.avg_price = (pos.avg_price * abs(pos.qty) + price * qty) / abs(new_qty)
            pos.qty = new_qty
        else:
            remaining = pos.qty + signed
            if remaining == 0:
                pos.avg_price = Decimal(0)
            elif (remaining > 0) != (pos.qty > 0):
                pos.avg_price = price
            pos.qty = remaining

    def _emit(self, order: _SimOrder, fills: tuple[Fill, ...], reason: str = "") -> None:
        self._handler(
            BrokerOrderUpdate(
                broker_order_id=order.broker_id,
                status=order.status,
                filled_qty=order.filled_qty,
                time=self._clock.now(),
                fills=fills,
                reason=reason,
            )
        )


def _slip(price: Decimal, ticks: int, table: TickTable, *, up: bool) -> Decimal:
    base = price if is_on_tick(price, table) else round_to_tick(price, table, up=up)
    if ticks == 0:
        return base
    return shift_ticks(base, ticks if up else -ticks, table)
