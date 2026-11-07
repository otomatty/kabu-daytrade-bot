from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest

from daytrade_bot.brokers.base import BrokerUnavailable, OrderRejected, OrderStateUnknown
from daytrade_bot.core.models import (
    BrokerOrderSnapshot,
    BrokerOrderUpdate,
    BrokerPosition,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Tick,
)
from daytrade_bot.core.order_manager import OrderManager, can_transition
from tests.conftest import D, at


class FakeBroker:
    name = "fake"

    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.next_error: Exception | None = None
        self.next_id = 1

    def set_update_handler(self, handler) -> None:  # pragma: no cover - 使わない
        pass

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    async def place_order(self, req: OrderRequest) -> str:
        if self.next_error is not None:
            err, self.next_error = self.next_error, None
            raise err
        self.placed.append(req)
        oid = f"B{self.next_id}"
        self.next_id += 1
        return oid

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)

    async def fetch_orders(self) -> Sequence[BrokerOrderSnapshot]:  # pragma: no cover
        return []

    async def fetch_positions(self) -> Sequence[BrokerPosition]:  # pragma: no cover
        return []

    async def on_tick(self, tick: Tick) -> None:  # pragma: no cover
        pass


def make(broker: FakeBroker | None = None) -> tuple[OrderManager, FakeBroker, list[Order]]:
    b = broker or FakeBroker()
    changes: list[Order] = []
    om = OrderManager(b, on_change=lambda o: changes.append(o.copy()), id_prefix="t")
    return om, b, changes


def new(om: OrderManager, side: Side = Side.BUY, qty: int = 100, **kw) -> OrderRequest:
    return om.new_request(
        symbol="7203",
        side=side,
        qty=qty,
        effect=kw.pop("effect", PositionEffect.OPEN),
        now=kw.pop("now", at(10)),
        **kw,
    )


def upd(
    bid: str, status: OrderStatus, filled: int, fills: tuple[Fill, ...] = (), when=None
) -> BrokerOrderUpdate:
    return BrokerOrderUpdate(
        broker_order_id=bid,
        status=status,
        filled_qty=filled,
        time=when or at(10, 0, 5),
        fills=fills,
    )


def f(fid: str, qty: int, price: int, cid: str = "") -> Fill:
    return Fill(
        fill_id=fid,
        client_order_id=cid,
        symbol="7203",
        side=Side.BUY,
        qty=qty,
        price=D(price),
        fee=D(0),
        time=at(10, 0, 5),
    )


async def test_submit_and_fill_lifecycle() -> None:
    om, _, changes = make()
    req = new(om)
    assert req.client_order_id == "t-20261109-00001"
    res = await om.submit(req, at(10))
    assert res.accepted and res.order.status is OrderStatus.SUBMITTED
    assert res.order.broker_order_id == "B1"
    assert [c.status for c in changes] == [OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED]
    assert om.has_active("7203") and om.pending_qty("7203", PositionEffect.OPEN) == 100

    fills = om.apply_update(upd("B1", OrderStatus.PARTIALLY_FILLED, 40, (f("x1", 40, 1000),)))
    assert len(fills) == 1 and fills[0].client_order_id == req.client_order_id
    assert fills[0].effect is PositionEffect.OPEN
    order = om.get(req.client_order_id)
    assert (
        order is not None
        and order.status is OrderStatus.PARTIALLY_FILLED
        and order.filled_qty == 40
    )
    assert om.pending_qty("7203", PositionEffect.OPEN) == 60

    # 同じ約定が再送されても二重に数えない
    assert om.apply_update(upd("B1", OrderStatus.PARTIALLY_FILLED, 40, (f("x1", 40, 1000),))) == []
    fills = om.apply_update(
        upd("B1", OrderStatus.FILLED, 100, (f("x1", 40, 1000), f("x2", 60, 1010)))
    )
    assert [x.fill_id for x in fills] == ["x2"]
    assert order.status is OrderStatus.FILLED and order.avg_fill_price == D("1006")
    assert not om.has_active("7203")
    # 終了した注文が古い情報で戻ることはない
    om.apply_update(upd("B1", OrderStatus.SUBMITTED, 0))
    assert order.status is OrderStatus.FILLED


async def test_partial_update_with_full_qty_becomes_filled() -> None:
    om, _, _ = make()
    res = await om.submit(new(om), at(10))
    om.apply_update(upd("B1", OrderStatus.PARTIALLY_FILLED, 100, (f("a", 100, 1000),)))
    assert res.order.status is OrderStatus.FILLED


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (OrderRejected("余力不足", code=4002001), OrderStatus.REJECTED),
        (BrokerUnavailable("接続できません"), OrderStatus.REJECTED),
        (OrderStateUnknown("タイムアウト"), OrderStatus.UNKNOWN),
    ],
)
async def test_submit_errors(error: Exception, status: OrderStatus) -> None:
    om, broker, _ = make()
    broker.next_error = error
    res = await om.submit(new(om), at(10))
    assert res.order.status is status
    assert res.error
    assert res.accepted is (status is not OrderStatus.REJECTED)


async def test_duplicate_client_id_rejected() -> None:
    om, _, _ = make()
    req = new(om)
    await om.submit(req, at(10))
    with pytest.raises(ValueError, match="同じ注文ID"):
        await om.submit(req, at(10))


async def test_update_before_submit_response_is_replayed() -> None:
    om, _, _ = make()
    # 発注の応答より先に約定の通知が届いた(B1 はまだ誰の注文か分からない)
    early = upd("B1", OrderStatus.FILLED, 100, (f("e1", 100, 1000),))
    assert om.apply_update(early) == []
    assert len(om.unmatched_updates) == 1
    res = await om.submit(new(om), at(10))
    assert res.order.status is OrderStatus.FILLED
    replayed = om.take_replayed_fills()
    assert [x.fill_id for x in replayed] == ["e1"]
    assert om.take_replayed_fills() == []
    assert not om.unmatched_updates


async def test_cancel() -> None:
    om, broker, _ = make()
    res = await om.submit(new(om), at(10))
    cid = res.order.client_order_id
    assert await om.cancel(cid, at(10, 1))
    assert broker.cancelled == ["B1"] and res.order.status is OrderStatus.PENDING_CANCEL
    assert await om.cancel(cid, at(10, 1))  # 取消中なら何もせず真
    assert broker.cancelled == ["B1"]
    om.apply_update(upd("B1", OrderStatus.CANCELED, 0))
    assert res.order.status is OrderStatus.CANCELED
    assert not await om.cancel(cid, at(10, 2))
    assert not await om.cancel("missing", at(10, 2))


async def test_resolve_unknown_found_and_timeout() -> None:
    om, broker, _ = make()
    broker.next_error = OrderStateUnknown("timeout")
    found = await om.submit(new(om), at(10))
    broker.next_error = OrderStateUnknown("timeout")
    lost = await om.submit(new(om, side=Side.SELL, qty=200), at(10))
    # 証券会社側には found に対応する注文があり、その約定も先に届いている
    om.apply_update(upd("K9", OrderStatus.FILLED, 100, (f("k1", 100, 1000),)))
    snaps = [
        BrokerOrderSnapshot(
            "OTHER", "7203", Side.BUY, 100, 0, OrderStatus.SUBMITTED, at(9)
        ),  # 古い注文
        BrokerOrderSnapshot("K9", "7203", Side.BUY, 100, 100, OrderStatus.FILLED, at(10, 0, 1)),
    ]
    results = om.resolve_unknown(snaps, at(10, 0, 10))
    assert [o.client_order_id for o, _ in results] == [found.order.client_order_id]
    assert found.order.broker_order_id == "K9" and found.order.status is OrderStatus.FILLED
    assert [x.fill_id for x in om.take_replayed_fills()] == ["k1"]
    assert lost.order.status is OrderStatus.UNKNOWN
    results = om.resolve_unknown(snaps, at(10) + timedelta(seconds=61))
    assert lost.order.status is OrderStatus.REJECTED
    assert "見つからない" in results[0][1]


async def test_stale_limit_orders_and_restore() -> None:
    om, _, _ = make()
    res = await om.submit(new(om, order_type=OrderType.LIMIT, limit_price=D(1000)), at(10))
    await om.submit(new(om), at(10))
    assert om.stale_limit_orders(at(10, 0, 20), 30) == []
    assert om.stale_limit_orders(at(10, 0, 30), 30) == [res.order]
    om2, _, _ = make()
    om2.restore([res.order])
    assert om2.by_broker_id("B1") is res.order
    assert om2.all_orders() == [res.order]
    assert om2.active_orders("6758") == []


def test_transition_table() -> None:
    assert can_transition(OrderStatus.SUBMITTED, OrderStatus.FILLED)
    assert not can_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)
    assert can_transition(OrderStatus.PENDING_CANCEL, OrderStatus.SUBMITTED)  # 取消が通らなかった
    for terminal in (
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    ):
        assert not any(can_transition(terminal, s) for s in OrderStatus)
