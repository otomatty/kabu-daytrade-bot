"""Slack / Discord への通知(Incoming Webhook)。

通知の失敗で売買が止まらないように、送信は別タスクで行い、例外はログに残すだけにする。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

log = logging.getLogger(__name__)

Level = Literal["info", "warning", "error"]
_LEVEL_ORDER = {"info": 0, "warning": 1, "error": 2}
_ICONS = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}


class Notifier(Protocol):
    def post(self, level: Level, title: str, message: str) -> None:
        """通知を予約する(待たずに戻る)。"""
        ...

    async def aclose(self) -> None: ...


class NullNotifier:
    def post(self, level: Level, title: str, message: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Message:
    level: Level
    title: str
    message: str


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        kind: Literal["slack", "discord"],
        *,
        min_level: Level = "warning",
        prefix: str = "dtbot",
        client: httpx.AsyncClient | None = None,
        min_interval_sec: float = 1.0,
    ) -> None:
        self._url = url
        self._kind = kind
        self._min_level = min_level
        self._prefix = prefix
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._interval = min_interval_sec
        self._queue: asyncio.Queue[_Message | None] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.failed = 0

    def post(self, level: Level, title: str, message: str) -> None:
        if _LEVEL_ORDER[level] < _LEVEL_ORDER[self._min_level]:
            return
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._worker())
        try:
            self._queue.put_nowait(_Message(level, title, message))
        except asyncio.QueueFull:
            self.failed += 1
            log.warning("通知のキューがあふれたため破棄しました: %s", title)

    def payload(self, msg: _Message) -> dict[str, str]:
        text = f"{_ICONS[msg.level]} [{self._prefix}] {msg.title}\n{msg.message}".strip()
        if self._kind == "discord":
            return {"content": text[:1900]}
        return {"text": text[:3900]}

    async def _worker(self) -> None:
        while True:
            msg = await self._queue.get()
            if msg is None:
                return
            try:
                resp = await self._client.post(self._url, json=self.payload(msg))
                if resp.status_code >= 400:
                    self.failed += 1
                    log.warning("通知の送信に失敗しました(HTTP %s)", resp.status_code)
                else:
                    self.sent += 1
            except httpx.HTTPError as exc:
                self.failed += 1
                log.warning("通知の送信に失敗しました: %s", exc)
            await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        if self._task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        if self._owns_client:
            await self._client.aclose()
