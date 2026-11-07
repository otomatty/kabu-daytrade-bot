"""時計の抽象化。

本番は実時間、デモは加速した時間、バックテストは手動で進める時間を使う。
エンジンや取引時間の判定はこの抽象だけを見るので、どのモードでも同じコードで動く。
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


class Clock(Protocol):
    def now(self) -> datetime:
        """現在時刻(JST、タイムゾーン付き)。"""
        ...

    async def sleep(self, seconds: float) -> None:
        """この時計の上で ``seconds`` 秒待つ。"""
        ...


class SystemClock:
    """実時間の時計。"""

    def now(self) -> datetime:
        return datetime.now(JST)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class OffsetClock:
    """実時間と同じ速さで進むが、開始時刻をずらした時計(結合テストやリハーサル用)。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start はタイムゾーン付きである必要があります")
        self._start = start.astimezone(JST)
        self._t0 = _time.monotonic()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=_time.monotonic() - self._t0)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class AcceleratedClock:
    """``speed`` 倍速で進む時計(デモ用)。``jump_to`` で夜間などを飛ばせる。"""

    def __init__(self, start: datetime, speed: float) -> None:
        if start.tzinfo is None:
            raise ValueError("start はタイムゾーン付きである必要があります")
        if speed <= 0:
            raise ValueError("speed は正の値である必要があります")
        self._base = start.astimezone(JST)
        self._speed = speed
        self._t0 = _time.monotonic()

    @property
    def speed(self) -> float:
        return self._speed

    def now(self) -> datetime:
        return self._base + timedelta(seconds=(_time.monotonic() - self._t0) * self._speed)

    def jump_to(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("when はタイムゾーン付きである必要があります")
        self._base = when.astimezone(JST)
        self._t0 = _time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0) / self._speed)


class ManualClock:
    """手動で進める時計(バックテストと単体テスト用)。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start はタイムゾーン付きである必要があります")
        self._now = start.astimezone(JST)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("when はタイムゾーン付きである必要があります")
        when = when.astimezone(JST)
        if when < self._now:
            raise ValueError(f"時計を戻すことはできません: {self._now} -> {when}")
        self._now = when

    def advance(self, seconds: float) -> None:
        self.set(self._now + timedelta(seconds=seconds))

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
