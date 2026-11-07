"""kabuステーションAPI のブローカー(本番の発注)。

kabuステーションAPI には注文状態の PUSH 配信がないので、注文照会を定期的に呼んで
状態の変化と約定を検出する。約定は約定番号で重複を除く。

建玉の扱い:
- 信用取引: 建玉日が当日の建玉だけをボットの建玉とみなす(前日以前からのスイングの建玉は触らない)
- 現物取引: 起動時点の保有株から、当日ボットが売買した分を差し引いた数を「元から持っていた株」として
  除外する(長期保有の株をボットが売ってしまわないように)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from daytrade_bot.brokers.base import BrokerError, OrderRejected, UpdateHandler
from daytrade_bot.brokers.kabu import codes
from daytrade_bot.brokers.kabu.client import KabuRestClient
from daytrade_bot.config import AccountMode, TradingConfig
from daytrade_bot.core.clock import JST, Clock
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

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Tracked:
    status: OrderStatus
    cum_qty: int


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


class KabuBroker:
    name = "kabu"

    def __init__(
        self,
        client: KabuRestClient,
        trading: TradingConfig,
        clock: Clock,
        *,
        poll_interval_sec: float = 1.0,
        today_net_qty: Callable[[], Mapping[str, int]] | None = None,
        full_refresh_every: int = 30,
    ) -> None:
        self._client = client
        self._trading = trading
        self._clock = clock
        self._poll_interval = poll_interval_sec
        self._today_net_qty = today_net_qty or dict
        self._full_refresh_every = full_refresh_every
        self._handler: UpdateHandler = lambda _u: None
        self._effects: dict[str, PositionEffect] = {}
        self._tracked: dict[str, _Tracked] = {}
        self._seen_exec: set[str] = set()
        self._cash_baseline: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_poll: datetime | None = None
        self._polls = 0
        self.poll_errors = 0
        self.preexisting_active_orders: list[str] = []

    def set_update_handler(self, handler: UpdateHandler) -> None:
        self._handler = handler

    # ---- 起動と停止 -------------------------------------------------------------------------

    async def start(self) -> None:
        await self._client.authenticate()
        # 起動前からある注文の約定は、すでに建玉に含まれているので通知しない
        for order in await self._client.orders():
            oid = str(order.get("ID"))
            status, _ = codes.order_status(order)
            self._tracked[oid] = _Tracked(status, int(Decimal(str(order.get("CumQty") or 0))))
            for detail in order.get("Details") or []:
                if int(detail.get("RecType") or 0) == codes.REC_EXECUTION:
                    self._seen_exec.add(_exec_id(oid, detail))
            if status.is_active:
                self.preexisting_active_orders.append(oid)
        if self.preexisting_active_orders:
            log.warning(
                "起動前からの未約定の注文があります(ボットは管理しません): %s",
                ", ".join(self.preexisting_active_orders),
            )
        if self._trading.account_mode is AccountMode.CASH:
            holdings = await self._raw_positions()
            bot_today = self._today_net_qty()
            self._cash_baseline = {
                symbol: qty - bot_today.get(symbol, 0) for symbol, (qty, _) in holdings.items()
            }
        self._last_poll = self._clock.now()
        self._task = asyncio.create_task(self._poll_loop(), name="kabu-order-poll")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---- 発注 -------------------------------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> str:
        payload = await self.build_payload(req)
        order_id = await self._client.send_order(payload)
        self._effects[order_id] = req.effect
        self._tracked.setdefault(order_id, _Tracked(OrderStatus.SUBMITTED, 0))
        return order_id

    async def build_payload(self, req: OrderRequest) -> dict[str, Any]:
        t = self._trading
        payload = codes.base_order_payload(t)
        payload.update(
            {
                "Symbol": req.symbol,
                "Side": codes.SIDE[req.side],
                "Qty": req.qty,
                "FrontOrderType": codes.FRONT_ORDER_TYPE[req.order_type],
                "Price": float(req.limit_price)
                if req.order_type is OrderType.LIMIT and req.limit_price is not None
                else 0,
                "Exchange": codes.ORDER_EXCHANGE[t.order_exchange],
            }
        )
        if t.account_mode is AccountMode.MARGIN:
            payload["MarginTradeType"] = codes.MARGIN_TRADE_TYPE[t.margin_trade_type]
            if req.effect is PositionEffect.OPEN:
                payload["CashMargin"] = codes.CASH_MARGIN_OPEN
                payload["DelivType"] = codes.DELIV_NONE
            else:
                payload["CashMargin"] = codes.CASH_MARGIN_CLOSE
                payload["DelivType"] = codes.DELIV_DEPOSIT
                exchange, close_positions = await self._close_positions(req)
                payload["Exchange"] = exchange
                payload["ClosePositions"] = close_positions
        else:
            payload["CashMargin"] = codes.CASH_MARGIN_CASH
            if req.side is Side.BUY:
                if req.effect is PositionEffect.CLOSE:
                    raise OrderRejected("現物取引では買いの返済(買い戻し)はできません")
                payload["DelivType"] = codes.DELIV_DEPOSIT
                payload["FundType"] = t.cash_fund_type
            else:
                if req.effect is PositionEffect.OPEN:
                    raise OrderRejected("現物取引では空売りできません")
                payload["DelivType"] = codes.DELIV_NONE
                payload["FundType"] = codes.FUND_TYPE_CASH_SELL
        return payload

    async def _close_positions(self, req: OrderRequest) -> tuple[int, list[dict[str, Any]]]:
        """返済する建玉(建玉IDと数量)を、古い建玉から順に選ぶ。"""
        rows = await self._client.positions(product=codes.PRODUCT_MARGIN, symbol=req.symbol)
        want_side = codes.SIDE[req.side.opposite]  # 買いの返済は売り建玉、売りの返済は買い建玉
        today = self._clock.now().astimezone(JST).strftime("%Y%m%d")
        candidates = [
            r
            for r in rows
            if str(r.get("Symbol")) == req.symbol
            and str(r.get("Side")) == want_side
            and str(r.get("ExecutionDay") or "") == today
            and r.get("ExecutionID")
        ]
        candidates.sort(key=lambda r: str(r.get("ExecutionID")))
        remaining = req.qty
        chosen: list[dict[str, Any]] = []
        exchanges: set[int] = set()
        for row in candidates:
            available = int(Decimal(str(row.get("LeavesQty") or 0))) - int(
                Decimal(str(row.get("HoldQty") or 0))
            )
            if available <= 0:
                continue
            take = min(available, remaining)
            chosen.append({"HoldID": str(row["ExecutionID"]), "Qty": take})
            exchanges.add(
                int(row.get("Exchange") or codes.ORDER_EXCHANGE[self._trading.order_exchange])
            )
            remaining -= take
            if remaining == 0:
                break
        if remaining > 0:
            raise OrderRejected(
                f"返済できる当日の建玉が足りません({req.symbol}: 必要 {req.qty}株 / "
                f"返済可能 {req.qty - remaining}株)"
            )
        if len(exchanges) > 1:
            raise OrderRejected(f"返済する建玉の市場が複数あります({req.symbol})")
        return exchanges.pop(), chosen

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._client.cancel_order(broker_order_id)

    async def on_tick(self, tick: Tick) -> None:
        return None

    # ---- 照会 -------------------------------------------------------------------------------

    async def fetch_orders(self) -> Sequence[BrokerOrderSnapshot]:
        result: list[BrokerOrderSnapshot] = []
        for order in await self._client.orders():
            side = codes.SIDE_FROM_CODE.get(str(order.get("Side")))
            if side is None:
                continue
            status, _ = codes.order_status(order)
            result.append(
                BrokerOrderSnapshot(
                    broker_order_id=str(order.get("ID")),
                    symbol=str(order.get("Symbol")),
                    side=side,
                    qty=int(Decimal(str(order.get("OrderQty") or 0))),
                    filled_qty=int(Decimal(str(order.get("CumQty") or 0))),
                    status=status,
                    received_at=parse_time(order.get("RecvTime")),
                )
            )
        return result

    async def _raw_positions(self) -> dict[str, tuple[int, Decimal]]:
        """銘柄ごとの (符号付き数量, 平均単価)。信用は当日の建玉だけ。"""
        margin = self._trading.account_mode is AccountMode.MARGIN
        product = codes.PRODUCT_MARGIN if margin else codes.PRODUCT_CASH
        today = self._clock.now().astimezone(JST).strftime("%Y%m%d")
        sums: dict[str, tuple[int, Decimal]] = {}
        for row in await self._client.positions(product=product):
            if margin and str(row.get("ExecutionDay") or "") != today:
                continue
            side = codes.SIDE_FROM_CODE.get(str(row.get("Side")))
            if side is None:
                continue
            qty = int(Decimal(str(row.get("LeavesQty") or 0))) * side.sign
            price = Decimal(str(row.get("Price") or 0))
            symbol = str(row.get("Symbol"))
            prev_qty, prev_cost = sums.get(symbol, (0, Decimal(0)))
            sums[symbol] = (prev_qty + qty, prev_cost + price * abs(qty))
        return {s: (q, c) for s, (q, c) in sums.items()}

    async def fetch_positions(self) -> Sequence[BrokerPosition]:
        result: list[BrokerPosition] = []
        for symbol, (gross, cost) in (await self._raw_positions()).items():
            qty = gross - self._cash_baseline.get(symbol, 0)
            if qty == 0:
                continue
            avg = cost / abs(gross) if gross else Decimal(0)
            result.append(BrokerPosition(symbol=symbol, qty=qty, avg_price=avg))
        return result

    # ---- 注文状態の監視 ----------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self.poll_once()
                self.poll_errors = 0
            except BrokerError as exc:
                self.poll_errors += 1
                if self.poll_errors in (1, 10) or self.poll_errors % 60 == 0:
                    log.warning("注文照会に失敗しました(%d回目): %s", self.poll_errors, exc)

    async def poll_once(self) -> int:
        """注文照会を1回行い、変化があった注文の更新を通知する。通知した件数を返す。"""
        started = self._clock.now()
        self._polls += 1
        updtime = None
        if self._last_poll is not None and self._polls % self._full_refresh_every != 0:
            # 時刻のずれや取りこぼしに備えて1分重ねて取得する(重複は約定番号で除く)
            updtime = (
                (self._last_poll - timedelta(seconds=60)).astimezone(JST).strftime("%Y%m%d%H%M%S")
            )
        orders = await self._client.orders(updtime=updtime)
        self._last_poll = started
        emitted = 0
        for order in orders:
            update = self._to_update(order)
            if update is not None:
                self._handler(update)
                emitted += 1
        return emitted

    def _to_update(self, order: dict[str, Any]) -> BrokerOrderUpdate | None:
        oid = str(order.get("ID"))
        status, reason = codes.order_status(order)
        cum = int(Decimal(str(order.get("CumQty") or 0)))
        side = codes.SIDE_FROM_CODE.get(str(order.get("Side")), Side.BUY)
        effect = self._effects.get(oid) or (
            PositionEffect.CLOSE
            if int(order.get("CashMargin") or 0) == codes.CASH_MARGIN_CLOSE
            else PositionEffect.OPEN
        )
        fills: list[Fill] = []
        for detail in order.get("Details") or []:
            if int(detail.get("RecType") or 0) != codes.REC_EXECUTION:
                continue
            exec_id = _exec_id(oid, detail)
            if exec_id in self._seen_exec:
                continue
            qty = int(Decimal(str(detail.get("Qty") or 0)))
            price = Decimal(str(detail.get("Price") or 0))
            if qty <= 0 or price <= 0:
                continue
            fee = Decimal(str(detail.get("Commission") or 0)) + Decimal(
                str(detail.get("CommissionTax") or 0)
            )
            when = (
                parse_time(detail.get("ExecutionDay"))
                or parse_time(detail.get("TransactTime"))
                or self._clock.now()
            )
            self._seen_exec.add(exec_id)
            fills.append(
                Fill(
                    fill_id=exec_id,
                    client_order_id="",
                    symbol=str(order.get("Symbol")),
                    side=side,
                    qty=qty,
                    price=price,
                    fee=max(fee, Decimal(0)),
                    time=when,
                    effect=effect,
                )
            )
        tracked = self._tracked.get(oid)
        if (
            tracked is not None
            and tracked.status == status
            and tracked.cum_qty == cum
            and not fills
        ):
            return None
        self._tracked[oid] = _Tracked(status, cum)
        return BrokerOrderUpdate(
            broker_order_id=oid,
            status=status,
            filled_qty=cum,
            time=self._clock.now(),
            fills=tuple(fills),
            reason=reason,
        )


def _exec_id(order_id: str, detail: Mapping[str, Any]) -> str:
    exec_id = str(detail.get("ExecutionID") or "").strip()
    return f"{order_id}:{exec_id or 'seq' + str(detail.get('SeqNum'))}"
