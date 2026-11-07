from __future__ import annotations

import pytest

from daytrade_bot.brokers.base import OrderRejected
from daytrade_bot.brokers.sim import SimulatedBroker
from daytrade_bot.core.clock import ManualClock
from daytrade_bot.core.fees import FixedFee, ZeroFee
from daytrade_bot.core.models import (
    BrokerOrderUpdate,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
)
from daytrade_bot.core.ticksize import TickTable
from tests.conftest import D, at, tick


def make(slippage: int = 1, touch: bool = False, allow_short: bool = True, fee=None):
    clock = ManualClock(at(10))
    updates: list[BrokerOrderUpdate] = []
    b = SimulatedBroker(
        clock,
        fee or ZeroFee(),
        lambda s: TickTable.STANDARD,
        slippage_ticks=slippage,
        limit_fill_on_touch=touch,
        allow_short=allow_short,
    )
    b.set_update_handler(updates.append)
    return b, clock, updates


def req(
    side=Side.BUY, qty=100, effect=PositionEffect.OPEN, otype=OrderType.MARKET, limit=None, cid="c1"
):
    return OrderRequest(
        cid, "7203", side, qty, otype, effect, at(10), limit_price=D(limit) if limit else None
    )


async def test_market_buy_fills_on_next_tick_with_slippage() -> None:
    b, clock, updates = make(slippage=1, fee=FixedFee(D(55)))
    oid = await b.place_order(req())
    assert updates == []  # 発注した瞬間には約定しない
    clock.advance(1)
    await b.on_tick(tick("7203", at(10, 0, 1), 2000, ask=D(2001), bid=D(1999)))
    (u,) = updates
    assert u.broker_order_id == oid and u.status is OrderStatus.FILLED and u.filled_qty == 100
    (fill,) = u.fills
    assert fill.price == D(2002)  # 最良売り気配 2001 から1呼値不利
    assert fill.fee == D(55) and fill.client_order_id == "c1"
    assert (await b.fetch_positions())[0].qty == 100
    assert b.stats.fills == 1 and b.stats.slippage_cost == D(200)


async def test_off_tick_quote_is_rounded_against_us() -> None:
    b, _, updates = make(slippage=1)
    await b.place_order(req())
    await b.on_tick(tick("7203", at(10, 0, 1), 3003))  # 呼値に乗っていないデータ
    assert updates[0].fills[0].price == D(3010)  # 3005 に切り上げてから1呼値


async def test_market_sell_uses_bid_and_no_slippage() -> None:
    b, _, updates = make(slippage=0)
    await b.place_order(req(Side.SELL))
    await b.on_tick(tick("7203", at(10, 0, 1), 3000, bid=D(2999)))
    assert updates[0].fills[0].price == D(2999)
    pos = (await b.fetch_positions())[0]
    assert pos.qty == -100 and pos.avg_price == D(2999)


async def test_limit_orders_need_to_trade_through() -> None:
    b, _, updates = make()
    await b.place_order(req(otype=OrderType.LIMIT, limit=1000))
    await b.on_tick(tick("7203", at(10, 0, 1), 1000))
    assert updates == []
    await b.on_tick(tick("7203", at(10, 0, 2), 999))
    assert updates[0].fills[0].price == D(1000)
    b2, _, updates2 = make(touch=True)
    await b2.place_order(req(Side.SELL, otype=OrderType.LIMIT, limit=1000))
    await b2.on_tick(tick("7203", at(10, 0, 1), 1000))
    assert updates2[0].fills[0].price == D(1000)


async def test_close_validation_and_short_restriction() -> None:
    b, _, _ = make()
    with pytest.raises(OrderRejected, match="返済する建玉"):
        await b.place_order(req(Side.SELL, effect=PositionEffect.CLOSE))
    await b.place_order(req())
    await b.on_tick(tick("7203", at(10, 0, 1), 1000))
    with pytest.raises(OrderRejected, match="返済する建玉"):
        await b.place_order(req(Side.BUY, effect=PositionEffect.CLOSE, cid="c2"))
    await b.place_order(req(Side.SELL, qty=60, effect=PositionEffect.CLOSE, cid="c3"))
    with pytest.raises(OrderRejected, match="超えています"):
        await b.place_order(req(Side.SELL, qty=60, effect=PositionEffect.CLOSE, cid="c4"))
    b2, _, _ = make(allow_short=False)
    with pytest.raises(OrderRejected, match="空売り"):
        await b2.place_order(req(Side.SELL))
    assert b.stats.rejects == 3


async def test_cancel_and_orders_snapshot() -> None:
    b, _, updates = make()
    oid = await b.place_order(req(otype=OrderType.LIMIT, limit=900))
    (snap,) = await b.fetch_orders()
    assert snap.broker_order_id == oid and snap.status is OrderStatus.SUBMITTED
    await b.cancel_order(oid)
    assert updates[-1].status is OrderStatus.CANCELED
    with pytest.raises(OrderRejected, match="取り消せない"):
        await b.cancel_order(oid)
    with pytest.raises(OrderRejected, match="該当"):
        await b.cancel_order("nope")


async def test_round_trip_and_flip_positions() -> None:
    b, _, _ = make(slippage=0)
    await b.place_order(req(qty=100, cid="a"))
    await b.on_tick(tick("7203", at(10, 0, 1), 1000))
    await b.place_order(req(Side.SELL, qty=100, effect=PositionEffect.CLOSE, cid="b"))
    await b.on_tick(tick("7203", at(10, 0, 2), 1010))
    assert await b.fetch_positions() == []
    await b.place_order(req(Side.BUY, qty=100, cid="c"))
    await b.on_tick(tick("7203", at(10, 0, 3), 1000))
    await b.place_order(req(Side.SELL, qty=300, cid="d"))  # ドテン(新規売りとして扱う)
    await b.on_tick(tick("7203", at(10, 0, 4), 1020))
    pos = (await b.fetch_positions())[0]
    assert pos.qty == -200 and pos.avg_price == D(1020)


async def test_settle_at_last() -> None:
    b, _, updates = make(slippage=0)
    await b.place_order(req(qty=100, cid="a"))
    await b.on_tick(tick("7203", at(10, 0, 1), 1000))
    await b.place_order(req(otype=OrderType.LIMIT, limit=500, cid="b"))
    await b.place_order(req(Side.SELL, qty=100, effect=PositionEffect.CLOSE, cid="c"))
    b._last_prices["7203"] = D(1100)
    assert b.settle_at_last() == 1
    statuses = [u.status for u in updates[-2:]]
    assert statuses == [OrderStatus.CANCELED, OrderStatus.FILLED]
    last = updates[-1]
    assert last.fills[0].price == D(1100) and last.fills[0].effect is PositionEffect.CLOSE
    assert await b.fetch_positions() == []


async def test_marketable_limit_fills_at_better_market_price() -> None:
    b, _, updates = make()
    await b.on_tick(tick("7203", at(10), 1000))
    await b.place_order(req(otype=OrderType.LIMIT, limit=1005))  # 現値1000より高い買い指値
    await b.on_tick(tick("7203", at(10, 0, 1), 1001, ask=D(1002)))
    assert updates[0].fills[0].price == D(1002)
    # 発注直後に値が飛んで指値を超えた場合は約定せず、以後は板に並んだ指値として扱う
    await b.place_order(req(otype=OrderType.LIMIT, limit=1003, cid="c2"))
    await b.on_tick(tick("7203", at(10, 0, 2), 1010))
    assert len(updates) == 1
    await b.on_tick(tick("7203", at(10, 0, 3), 1000))
    assert updates[1].fills[0].price == D(1003)
    b2, _, updates2 = make()
    await b2.on_tick(tick("7203", at(10), 1000))
    await b2.place_order(req(Side.SELL, otype=OrderType.LIMIT, limit=995))
    await b2.on_tick(tick("7203", at(10, 0, 1), 999, bid=D(998)))
    assert updates2[0].fills[0].price == D(998)
    await b2.place_order(req(Side.SELL, otype=OrderType.LIMIT, limit=996, cid="c2"))
    await b2.on_tick(tick("7203", at(10, 0, 2), 990))
    assert len(updates2) == 1


def test_rejects_negative_slippage() -> None:
    with pytest.raises(ValueError, match="slippage"):
        make(slippage=-1)
