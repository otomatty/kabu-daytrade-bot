"""ダッシュボードなどへ状態の変化を配る仕組み(購読者ごとの上限付きキュー)。

購読者の処理が遅れても、エンジンは待たない。キューがあふれたら古いものから捨てる。
"""

from __future__ import annotations

import asyncio
from typing import Any

Event = dict[str, Any]


class Subscription:
    def __init__(self, bus: EventBus, maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def get_nowait(self) -> Event | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(self, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, maxsize)
        self._subs.append(sub)
        return sub

    def publish(self, event: Event) -> None:
        for sub in list(self._subs):
            sub._offer(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
