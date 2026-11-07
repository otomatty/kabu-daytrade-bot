"""kabuステーションAPI のモックサーバー(結合テストと、本番前のリハーサル用)。

公式リファレンス(kabu_STATION_API.yaml v1.5)の形式で応答し、発注の内容を厳しく検証する。
本物の kabuステーションと違い、値段は乱数で動き、注文はこのサーバーの中だけで約定する。

起動: ``dtbot mock-kabu --port 18090``(設定の kabu.port を 18090 にすると live をリハーサルできる)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import random
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from daytrade_bot.core.clock import JST, Clock, SystemClock
from daytrade_bot.core.ticksize import TickTable, round_to_tick, tick_size

ORDER_PATHS = {"/kabusapi/sendorder", "/kabusapi/cancelorder"}


def _iso(t: datetime) -> str:
    return t.astimezone(JST).isoformat()


def _err(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"Code": code, "Message": message}, status_code=status)


@dataclass
class _Symbol:
    symbol: str
    price: Decimal
    table: TickTable
    volume: int = 0
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    time: datetime | None = None

    @property
    def tick(self) -> Decimal:
        return tick_size(self.price, self.table)

    @property
    def best_ask(self) -> Decimal:
        return self.price + tick_size(self.price + Decimal("0.0001"), self.table)

    @property
    def best_bid(self) -> Decimal:
        return self.price


@dataclass
class _Order:
    id: str
    body: dict[str, Any]
    recv: datetime
    state: int = 3
    cum: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    updated: datetime | None = None

    @property
    def qty(self) -> int:
        return int(self.body["Qty"])

    @property
    def active(self) -> bool:
        return self.state != 5


@dataclass
class _Position:
    hold_id: str
    symbol: str
    side: str
    qty: int
    hold: int
    price: Decimal
    exchange: int
    margin_trade_type: int | None
    day: str
    cash: bool = False


@dataclass
class FailRule:
    path: str
    mode: str  # "status" | "drop"
    status: int = 500
    count: int = 1
    after_processing: bool = False
    delay: float = 2.0  # "drop" のとき、応答を返さずに待つ秒数


class MockKabuStation:
    def __init__(
        self,
        *,
        password: str = "mock-password",  # noqa: S107 - モック専用の既定値
        clock: Clock | None = None,
        prices: Mapping[str, Decimal] | None = None,
        seed: int = 7,
        tick_interval_sec: float = 0.2,
        rate_limit_per_sec: int = 10,
        auto_ticks: bool = True,
    ) -> None:
        self.password = password
        self.clock = clock or SystemClock()
        self.token: str | None = None
        self.symbols: dict[str, _Symbol] = {}
        self._initial_prices = dict(prices or {})
        self.registered: list[str] = []
        self.orders: dict[str, _Order] = {}
        self.positions: list[_Position] = []
        self.requests: list[dict[str, Any]] = []
        self.fail_rules: list[FailRule] = []
        self.rng = random.Random(seed)
        self.tick_interval = tick_interval_sec
        self.rate_limit = rate_limit_per_sec
        self.auto_ticks = auto_ticks
        self.push_paused = False
        self.sockets: set[WebSocket] = set()
        self._seq = itertools.count(1)
        self._exec_seq = itertools.count(1)
        self._req_times: dict[str, list[float]] = {"order": [], "info": []}
        self.app = self._build_app()

    # ---- 値段 -------------------------------------------------------------------------------

    def _sym(self, symbol: str) -> _Symbol:
        sym = self.symbols.get(symbol)
        if sym is None:
            price = Decimal(str(self._initial_prices.get(symbol, Decimal(2000))))
            sym = _Symbol(symbol, price, TickTable.STANDARD)
            self.symbols[symbol] = sym
        return sym

    def set_price(self, symbol: str, price: Decimal, volume: int = 100) -> None:
        sym = self._sym(symbol)
        sym.price = round_to_tick(price, sym.table, up=False)
        sym.volume += volume
        sym.open = sym.open or sym.price
        sym.high = max(sym.high or sym.price, sym.price)
        sym.low = min(sym.low or sym.price, sym.price)
        sym.time = self.clock.now()
        self._match(symbol)

    def step(self) -> None:
        for symbol in list(self.registered):
            sym = self._sym(symbol)
            move = self.rng.choice((-2, -1, 0, 0, 1, 2))
            self.set_price(
                symbol, max(sym.price + move * sym.tick, sym.tick), self.rng.randint(1, 20) * 100
            )

    def board(self, symbol: str) -> dict[str, Any]:
        sym = self._sym(symbol)
        now = sym.time or self.clock.now()
        ask, bid = sym.best_ask, sym.best_bid
        return {
            "Symbol": symbol,
            "SymbolName": f"モック{symbol}",
            "Exchange": 1,
            "ExchangeName": "東証プ",
            "CurrentPrice": float(sym.price),
            "CurrentPriceTime": _iso(now),
            "CurrentPriceChangeStatus": "0057",
            "CurrentPriceStatus": 1,
            "CalcPrice": float(sym.price),
            "OpeningPrice": float(sym.open or sym.price),
            "HighPrice": float(sym.high or sym.price),
            "LowPrice": float(sym.low or sym.price),
            "TradingVolume": float(sym.volume),
            "TradingVolumeTime": _iso(now),
            "VWAP": float(sym.price),
            # kabuステーションの命名: Bid=最良売気配(Sell1)、Ask=最良買気配(Buy1)
            "BidQty": 1000.0,
            "BidPrice": float(ask),
            "BidTime": _iso(now),
            "BidSign": "0101",
            "Sell1": {"Time": _iso(now), "Sign": "0101", "Price": float(ask), "Qty": 1000.0},
            "AskQty": 1000.0,
            "AskPrice": float(bid),
            "AskTime": _iso(now),
            "AskSign": "0101",
            "Buy1": {"Time": _iso(now), "Sign": "0101", "Price": float(bid), "Qty": 1000.0},
            "SecurityType": 1,
        }

    async def push_all(self) -> None:
        if self.push_paused:
            return
        for symbol in list(self.registered):
            await self.broadcast(json.dumps(self.board(symbol), ensure_ascii=False))

    async def broadcast(self, text: str) -> None:
        for ws in list(self.sockets):
            try:
                await ws.send_text(text)
            except (RuntimeError, WebSocketDisconnect):
                self.sockets.discard(ws)

    async def drop_sockets(self) -> None:
        for ws in list(self.sockets):
            with contextlib.suppress(RuntimeError):
                await ws.close(code=1011)
        self.sockets.clear()

    # ---- 約定 -------------------------------------------------------------------------------

    def _match(self, symbol: str) -> None:
        sym = self._sym(symbol)
        for order in [o for o in self.orders.values() if o.active and o.body["Symbol"] == symbol]:
            body = order.body
            buy = body["Side"] == "2"
            market = sym.best_ask if buy else sym.best_bid
            if body["FrontOrderType"] == 10:
                price = market
            else:
                limit = Decimal(str(body["Price"]))
                if (buy and market > limit) or (not buy and market < limit):
                    continue
                price = limit
            self._execute(order, order.qty - order.cum, price)

    def _execute(self, order: _Order, qty: int, price: Decimal) -> None:
        now = self.clock.now()
        body = order.body
        exec_id = f"E{now:%Y%m%d}{next(self._exec_seq):08d}"
        order.cum += qty
        order.details.append(
            {
                "SeqNum": len(order.details) + 1,
                "ID": order.id,
                "RecType": 8,
                "ExchangeID": "00000000-0000-0000-0000-00000000",
                "State": 3,
                "TransactTime": _iso(now),
                "OrdType": 0,
                "Price": float(price),
                "Qty": float(qty),
                "ExecutionID": exec_id,
                "ExecutionDay": _iso(now),
                "DelivDay": int(f"{now:%Y%m%d}"),
                "Commission": 0.0,
                "CommissionTax": 0.0,
            }
        )
        if order.cum >= order.qty:
            order.state = 5
        order.updated = now
        cash_margin = int(body["CashMargin"])
        if cash_margin == 3:
            for close in body.get("ClosePositions") or []:
                pos = next(p for p in self.positions if p.hold_id == close["HoldID"])
                pos.qty -= int(close["Qty"])
                pos.hold -= int(close["Qty"])
            self.positions = [p for p in self.positions if p.qty > 0]
        elif cash_margin == 2:
            self.positions.append(
                _Position(
                    exec_id,
                    body["Symbol"],
                    body["Side"],
                    qty,
                    0,
                    price,
                    int(body["Exchange"]),
                    int(body["MarginTradeType"]),
                    f"{now:%Y%m%d}",
                )
            )
        else:
            self._apply_cash(body, qty, price)

    def _apply_cash(self, body: dict[str, Any], qty: int, price: Decimal) -> None:
        symbol = body["Symbol"]
        pos = next((p for p in self.positions if p.cash and p.symbol == symbol), None)
        if body["Side"] == "2":
            if pos is None:
                self.positions.append(
                    _Position(f"C{symbol}", symbol, "2", qty, 0, price, 1, None, "", cash=True)
                )
            else:
                pos.price = (pos.price * pos.qty + price * qty) / (pos.qty + qty)
                pos.qty += qty
        elif pos is not None:
            pos.qty -= qty
            pos.hold = max(pos.hold - qty, 0)
            if pos.qty <= 0:
                self.positions.remove(pos)

    def add_cash_holding(self, symbol: str, qty: int, price: Decimal) -> None:
        self.positions.append(
            _Position(f"C{symbol}", symbol, "2", qty, 0, price, 1, None, "", cash=True)
        )

    # ---- 検証 -------------------------------------------------------------------------------

    def validate_order(self, body: dict[str, Any]) -> JSONResponse | None:
        required = (
            "Symbol",
            "Exchange",
            "SecurityType",
            "Side",
            "CashMargin",
            "DelivType",
            "AccountType",
            "Qty",
            "Price",
            "ExpireDay",
            "FrontOrderType",
        )
        missing = [k for k in required if k not in body]
        if missing:
            return _err(400, 4001012, f"リクエスト不正: 必須項目がありません {missing}")
        if body["Side"] not in ("1", "2"):
            return _err(400, 4002008, "パラメータ不正：Side")
        if body["AccountType"] not in (2, 4, 12):
            return _err(400, 4002007, "パラメータ不正：AccountType")
        if body["SecurityType"] != 1:
            return _err(400, 4002005, "商品エラー")
        cash_margin = body["CashMargin"]
        if cash_margin not in (1, 2, 3):
            return _err(400, 4002009, "パラメータ不正：CashMargin")
        if cash_margin in (1, 2) and body["Exchange"] not in (9, 27):
            return _err(400, 4002005, "新規注文は SOR(9) か 東証+(27) を指定してください")
        if not isinstance(body["Qty"], int) or body["Qty"] <= 0 or body["Qty"] % 100:
            return _err(400, 4001012, "数量は売買単位(100株)の倍数で指定してください")
        fot = body["FrontOrderType"]
        if fot not in (10, 20):
            return _err(400, 4002012, "パラメータ不正：FrontOrderType(モックは10と20のみ)")
        price = Decimal(str(body["Price"]))
        if fot == 10 and price != 0:
            return _err(400, 4002017, "パラメータ不正：成行の Price は0")
        if fot == 20:
            sym = self._sym(body["Symbol"])
            if price <= 0 or price % tick_size(price, sym.table) != 0:
                return _err(400, 4002017, "パラメータ不正：値段指定エラー")
        if body["ExpireDay"] != 0:
            return _err(400, 4001012, "モックは ExpireDay=0(本日)のみ")
        side = body["Side"]
        if cash_margin == 1:
            if side == "2":
                if body["DelivType"] not in (2, 3):
                    return _err(400, 4002010, "パラメータ不正：DelivType(現物買は2か3)")
                if body.get("FundType") not in ("02", "AA"):
                    return _err(400, 4002011, "パラメータ不正：FundType(現物買は02かAA)")
            else:
                if body["DelivType"] != 0:
                    return _err(400, 4002010, "パラメータ不正：DelivType(現物売は0)")
                if body.get("FundType") != "  ":
                    return _err(400, 4002011, "パラメータ不正：FundType(現物売は半角スペース2つ)")
                held = sum(
                    p.qty - p.hold for p in self.positions if p.cash and p.symbol == body["Symbol"]
                )
                if held < body["Qty"]:
                    return _err(400, 4002001, "売却可能数量が不足しています")
            return None
        if body.get("MarginTradeType") not in (1, 2, 3):
            return _err(400, 4002013, "パラメータ不正：MarginTradeType")
        if cash_margin == 2:
            if body["DelivType"] != 0:
                return _err(400, 4002010, "パラメータ不正：DelivType(信用新規は0)")
            return None
        if body["DelivType"] not in (2, 3):
            return _err(400, 4002016, "パラメータ不正：DelivType(信用返済は2か3)")
        has_list = "ClosePositions" in body
        if has_list == ("ClosePositionOrder" in body):
            return _err(
                400, 4002015, "返済順指定と返済指定は同時に設定できない(どちらか一方が必須)"
            )
        if has_list:
            total = 0
            for item in body["ClosePositions"]:
                pos = next((p for p in self.positions if p.hold_id == item.get("HoldID")), None)
                want_side = "1" if side == "2" else "2"
                if pos is None or pos.cash or pos.side != want_side or pos.symbol != body["Symbol"]:
                    return _err(400, 4002001, f"返済建玉が見つかりません: {item.get('HoldID')}")
                if pos.exchange != body["Exchange"]:
                    return _err(400, 4002005, "建玉の市場と返済注文の市場が違います")
                if item["Qty"] > pos.qty - pos.hold:
                    return _err(400, 4002001, "返済可能数量を超えています")
                total += item["Qty"]
            if total != body["Qty"]:
                return _err(400, 4001012, "ClosePositions の数量の合計が Qty と一致しません")
        return None

    def _rate_limited(self, path: str) -> bool:
        category = "order" if path in ORDER_PATHS else "info"
        now = asyncio.get_running_loop().time()
        times = [t for t in self._req_times[category] if now - t < 1.0]
        times.append(now)
        self._req_times[category] = times
        return len(times) > self.rate_limit

    def _take_fail(self, path: str, *, after: bool) -> FailRule | None:
        for rule in self.fail_rules:
            if rule.path == path and rule.after_processing == after and rule.count > 0:
                rule.count -= 1
                return rule
        return None

    # ---- 応答の組み立て ----------------------------------------------------------------------

    def order_json(self, o: _Order) -> dict[str, Any]:
        b = o.body
        details = [
            {
                "SeqNum": 0,
                "ID": o.id,
                "RecType": 1,
                "ExchangeID": "00000000-0000-0000-0000-00000000",
                "State": 3,
                "TransactTime": _iso(o.recv),
                "OrdType": 1,
                "Price": float(b["Price"]),
                "Qty": float(b["Qty"]),
                "ExecutionID": "",
                "ExecutionDay": None,
                "DelivDay": None,
                "Commission": None,
                "CommissionTax": None,
            },
            *o.details,
        ]
        return {
            "ID": o.id,
            "State": o.state,
            "OrderState": o.state,
            "OrdType": 1,
            "RecvTime": _iso(o.recv),
            "Symbol": b["Symbol"],
            "SymbolName": f"モック{b['Symbol']}",
            "Exchange": b["Exchange"],
            "ExchangeName": "東証＋" if b["Exchange"] == 27 else "SOR",
            "TimeInForce": None,
            "Price": float(b["Price"]),
            "OrderQty": float(b["Qty"]),
            "CumQty": float(o.cum),
            "Side": b["Side"],
            "CashMargin": b["CashMargin"] if b["CashMargin"] != 1 else None,
            "AccountType": b["AccountType"],
            "DelivType": b["DelivType"],
            "ExpireDay": int(f"{o.recv:%Y%m%d}"),
            "MarginTradeType": b.get("MarginTradeType"),
            "MarginPremium": None,
            "Details": details,
        }

    def position_json(self, p: _Position) -> dict[str, Any]:
        return {
            "ExecutionID": None if p.cash else p.hold_id,
            "AccountType": 4,
            "Symbol": p.symbol,
            "SymbolName": f"モック{p.symbol}",
            "Exchange": p.exchange,
            "ExchangeName": "東証＋" if p.exchange == 27 else "東証",
            "SecurityType": None,
            "ExecutionDay": None if p.cash else int(p.day),
            "Price": float(p.price),
            "LeavesQty": float(p.qty),
            "HoldQty": float(p.hold),
            "Side": p.side,
            "Expenses": 0.0,
            "Commission": 0.0,
            "CommissionTax": 0.0,
            "ExpireDay": None if p.cash else int(p.day),
            "MarginTradeType": p.margin_trade_type,
            "CurrentPrice": None,
            "Valuation": None,
            "ProfitLoss": None,
            "ProfitLossRate": None,
        }

    # ---- アプリ -----------------------------------------------------------------------------

    def _build_app(self) -> FastAPI:  # noqa: PLR0915
        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            task = asyncio.create_task(self._ticker()) if self.auto_ticks else None
            yield
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        app = FastAPI(title="mock kabuステーションAPI", lifespan=lifespan)

        @app.middleware("http")
        async def gate(request: Request, call_next: Any) -> Response:
            path = request.url.path
            if path.startswith("/__mock"):
                return await call_next(request)  # type: ignore[no-any-return]
            raw = await request.body()
            entry: dict[str, Any] = {
                "method": request.method,
                "path": path,
                "query": dict(request.query_params),
                "body": json.loads(raw) if raw else None,
            }
            self.requests.append(entry)
            rule = self._take_fail(path, after=False)
            if rule is not None:
                if rule.mode == "drop":
                    await asyncio.sleep(rule.delay)
                return _err(rule.status, 4001001, "内部エラー(モック)")
            if path != "/kabusapi/token":
                if self.token is None or request.headers.get("X-API-KEY") != self.token:
                    return _err(401, 4001009, "APIキー不一致")
                if self._rate_limited(path):
                    return _err(429, 4001006, "API実行回数エラー")
            response: Response = await call_next(request)
            rule = self._take_fail(path, after=True)
            if rule is not None:
                await asyncio.sleep(rule.delay)  # 処理した後に応答が遅れる(タイムアウトを起こす)
            return response

        @app.post("/kabusapi/token")
        async def token(request: Request) -> Response:
            body = await request.json()
            if body.get("APIPassword") != self.password:
                return _err(400, 4001013, "トークン取得失敗")
            self.token = secrets.token_hex(16)
            return JSONResponse({"ResultCode": 0, "Token": self.token})

        @app.post("/kabusapi/sendorder")
        async def sendorder(request: Request) -> Response:
            body = await request.json()
            error = self.validate_order(body)
            if error is not None:
                return error
            now = self.clock.now()
            oid = f"{now:%Y%m%d}A02N{next(self._seq):08d}"
            order = _Order(oid, body, now, updated=now)
            self.orders[oid] = order
            for close in body.get("ClosePositions") or []:
                pos = next(p for p in self.positions if p.hold_id == close["HoldID"])
                pos.hold += int(close["Qty"])
            if body["CashMargin"] == 1 and body["Side"] == "1":
                for p in self.positions:
                    if p.cash and p.symbol == body["Symbol"]:
                        p.hold += int(body["Qty"])
            self._match(body["Symbol"])
            return JSONResponse({"Result": 0, "OrderId": oid})

        @app.put("/kabusapi/cancelorder")
        async def cancelorder(request: Request) -> Response:
            body = await request.json()
            order = self.orders.get(body.get("OrderId", ""))
            if order is None:
                return _err(400, 4004001, "該当注文なしエラー")
            if not order.active:
                return _err(400, 4004002, "取消不可")
            now = self.clock.now()
            order.state = 5
            order.updated = now
            order.details.append(
                {
                    "SeqNum": len(order.details) + 1,
                    "ID": order.id,
                    "RecType": 6,
                    "State": 3,
                    "TransactTime": _iso(now),
                    "OrdType": None,
                    "Price": None,
                    "Qty": float(order.qty - order.cum),
                    "ExecutionID": None,
                }
            )
            remaining = order.qty - order.cum
            for close in order.body.get("ClosePositions") or []:
                pos = next((p for p in self.positions if p.hold_id == close["HoldID"]), None)
                if pos is not None:
                    pos.hold = max(pos.hold - min(int(close["Qty"]), remaining), 0)
            return JSONResponse({"Result": 0, "OrderId": order.id})

        @app.get("/kabusapi/orders")
        async def orders(request: Request) -> Response:
            q = request.query_params
            result = []
            for o in self.orders.values():
                if q.get("symbol") and o.body["Symbol"] != q["symbol"]:
                    continue
                if q.get("updtime"):
                    since = datetime.strptime(q["updtime"], "%Y%m%d%H%M%S").replace(tzinfo=JST)
                    if (o.updated or o.recv) < since:
                        continue
                result.append(self.order_json(o))
            return JSONResponse(result)

        @app.get("/kabusapi/positions")
        async def positions(request: Request) -> Response:
            q = request.query_params
            product = int(q.get("product", "0"))
            rows = []
            for p in self.positions:
                if product == 1 and not p.cash:
                    continue
                if product == 2 and p.cash:
                    continue
                if q.get("symbol") and p.symbol != q["symbol"]:
                    continue
                rows.append(self.position_json(p))
            return JSONResponse(rows)

        @app.put("/kabusapi/register")
        async def register(request: Request) -> Response:
            body = await request.json()
            for item in body.get("Symbols", []):
                if item["Symbol"] not in self.registered:
                    if len(self.registered) >= 50:
                        return _err(400, 4002006, "レジスト数エラー")
                    self.registered.append(item["Symbol"])
                    self._sym(item["Symbol"])
            return JSONResponse(
                {"RegistList": [{"Symbol": s, "Exchange": 1} for s in self.registered]}
            )

        @app.put("/kabusapi/unregister")
        async def unregister(request: Request) -> Response:
            body = await request.json()
            for item in body.get("Symbols", []):
                if item["Symbol"] in self.registered:
                    self.registered.remove(item["Symbol"])
            return JSONResponse(
                {"RegistList": [{"Symbol": s, "Exchange": 1} for s in self.registered]}
            )

        @app.put("/kabusapi/unregister/all")
        async def unregister_all() -> Response:
            self.registered.clear()
            return JSONResponse({"RegistList": []})

        @app.get("/kabusapi/board/{target}")
        async def board(target: str) -> Response:
            symbol, _, _exchange = target.partition("@")
            return JSONResponse(self.board(symbol))

        @app.get("/kabusapi/apisoftlimit")
        async def soft_limit() -> Response:
            return JSONResponse(
                {
                    "Stock": 200.0,
                    "Margin": 200.0,
                    "Future": 10.0,
                    "FutureMini": 100.0,
                    "FutureMicro": 100.0,
                    "Option": 20.0,
                    "MiniOption": 20.0,
                    "KabuSVersion": "mock-5.0.0",
                }
            )

        @app.get("/kabusapi/wallet/margin")
        async def wallet_margin() -> Response:
            return JSONResponse({"MarginAccountWallet": 3_000_000.0, "DepositkeepRate": 0.0})

        @app.get("/kabusapi/wallet/cash")
        async def wallet_cash() -> Response:
            return JSONResponse(
                {
                    "StockAccountWallet": 3_000_000.0,
                    "AuKCStockAccountWallet": 3_000_000.0,
                    "AuJbnStockAccountWallet": 0.0,
                }
            )

        @app.websocket("/kabusapi/websocket")
        async def websocket(ws: WebSocket) -> None:
            await ws.accept()
            self.sockets.add(ws)
            try:
                while True:
                    await ws.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self.sockets.discard(ws)

        # ---- テスト用の操作 ----
        @app.post("/__mock/expire_token")
        async def expire_token() -> Response:
            self.token = secrets.token_hex(16)  # 別のトークンが発行された状態
            return JSONResponse({"ok": True})

        @app.post("/__mock/price")
        async def price(request: Request) -> Response:
            body = await request.json()
            self.set_price(body["symbol"], Decimal(str(body["price"])))
            await self.push_all()
            return JSONResponse({"ok": True})

        @app.post("/__mock/fail")
        async def fail(request: Request) -> Response:
            body = await request.json()
            self.fail_rules.append(FailRule(**body))
            return JSONResponse({"ok": True})

        @app.post("/__mock/drop_ws")
        async def drop_ws() -> Response:
            await self.drop_sockets()
            return JSONResponse({"ok": True})

        return app

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            self.step()
            await self.push_all()
