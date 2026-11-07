"""kabuステーションAPI の時価 PUSH 配信(WebSocket)。

- 接続が切れたら、間隔を延ばしながら(最大30秒)つなぎ直し、銘柄を登録し直す
- 気配の名前に注意: kabuステーションの ``BidPrice`` は最良「売」気配、
  ``AskPrice`` は最良「買」気配。ボットの ``Tick.ask``(最良売り気配)には
  ``BidPrice`` を、``Tick.bid``(最良買い気配)には ``AskPrice`` を入れる
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from daytrade_bot.brokers.base import BrokerError, TickHandler
from daytrade_bot.brokers.kabu.broker import parse_time
from daytrade_bot.brokers.kabu.client import KabuRestClient
from daytrade_bot.core.clock import Clock
from daytrade_bot.core.models import Tick

log = logging.getLogger(__name__)


class _Socket(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connector = Callable[[str], AbstractAsyncContextManager[_Socket]]


def _default_connect(url: str) -> AbstractAsyncContextManager[_Socket]:
    # kabuステーションはローカルで動くので、プロキシは使わない
    return connect(
        url, ping_interval=20, ping_timeout=20, open_timeout=10, max_size=2**22, proxy=None
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d > 0 else None


def parse_push(message: str | bytes, exchange: int | None = None) -> Tick | None:
    """PUSH 配信の1件をティックに変換する。現値が無い(寄り前など)ものは ``None``。"""
    data = json.loads(message, parse_float=Decimal)
    if not isinstance(data, dict):
        return None
    if exchange is not None and data.get("Exchange") not in (None, exchange):
        return None
    price = _decimal(data.get("CurrentPrice"))
    when = parse_time(data.get("CurrentPriceTime"))
    symbol = data.get("Symbol")
    if price is None or when is None or not symbol:
        return None
    volume = data.get("TradingVolume")
    return Tick(
        symbol=str(symbol),
        time=when,
        price=price,
        cum_volume=int(Decimal(str(volume))) if volume is not None else None,
        bid=_decimal(data.get("AskPrice")),  # 最良買気配
        ask=_decimal(data.get("BidPrice")),  # 最良売気配
    )


class KabuPushFeed:
    name = "kabu"

    def __init__(
        self,
        ws_url: str,
        client: KabuRestClient,
        clock: Clock,
        *,
        exchange: int = 1,
        connector: Connector = _default_connect,
        max_backoff_sec: float = 30.0,
    ) -> None:
        self._url = ws_url
        self._client = client
        self._clock = clock
        self._exchange = exchange
        self._connector = connector
        self._max_backoff = max_backoff_sec
        self._handler: TickHandler = lambda _t: None
        self._symbols: tuple[str, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_message_at: datetime | None = None
        self.messages = 0
        self.parse_errors = 0
        self.reconnects = 0

    def set_tick_handler(self, handler: TickHandler) -> None:
        self._handler = handler

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    async def start(self, symbols: Sequence[str]) -> None:
        self._symbols = tuple(symbols)
        await self._register()
        self._task = asyncio.create_task(self._run(), name="kabu-push")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected = False
        if self._symbols:
            try:
                await self._client.unregister(self._symbols, self._exchange)
            except BrokerError as exc:
                log.warning("銘柄登録の解除に失敗しました: %s", exc)

    async def _register(self) -> None:
        registered = await self._client.register(self._symbols, self._exchange)
        missing = set(self._symbols) - {str(r.get("Symbol")) for r in registered}
        if missing:
            log.warning("PUSH配信に登録できなかった銘柄: %s", ", ".join(sorted(missing)))

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while True:
            try:
                if not first:
                    await self._register()
                async with self._connector(self._url) as ws:
                    self._connected = True
                    if not first:
                        self.reconnects += 1
                        log.info("PUSH配信に再接続しました")
                    first = False
                    backoff = 1.0
                    async for message in ws:
                        self._on_message(message)
                log.warning("PUSH配信の接続が閉じられました")
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, TimeoutError, BrokerError) as exc:
                log.warning("PUSH配信の接続エラー: %s(%.0f秒後に再接続)", exc, backoff)
            finally:
                self._connected = False
            first = False
            await asyncio.sleep(backoff + random.uniform(0, backoff / 4))  # noqa: S311 - 再接続の揺らぎ
            backoff = min(backoff * 2, self._max_backoff)

    def _on_message(self, message: str | bytes) -> None:
        self._last_message_at = self._clock.now()
        self.messages += 1
        try:
            tick = parse_push(message, self._exchange)
        except (ValueError, TypeError, InvalidOperation) as exc:
            self.parse_errors += 1
            if self.parse_errors <= 5:
                log.warning("PUSH配信のデータを解釈できません: %s", exc)
            return
        if tick is not None and tick.symbol in self._symbols:
            self._handler(tick)
