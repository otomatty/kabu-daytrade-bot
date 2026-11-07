from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from daytrade_bot.config import AppConfig
from daytrade_bot.core.clock import JST
from daytrade_bot.core.models import Bar, Tick
from daytrade_bot.storage.db import Repository

# 2026-11-09 は月曜日の通常の営業日
TRADING_DAY = date(2026, 11, 9)


def at(h: int, m: int = 0, s: int = 0, day: date = TRADING_DAY, us: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, us, tzinfo=JST)


def D(x: str | int | float) -> Decimal:
    return Decimal(str(x))


def make_config(**overrides: Any) -> AppConfig:
    base: dict[str, Any] = {
        "universe": [
            {"symbol": "7203", "name": "トヨタ自動車"},
            {"symbol": "6758", "name": "ソニーG"},
        ],
        "strategy": {"name": "orb", "params": {"range_minutes": 15}},
        "sizing": {"method": "fixed_qty", "qty": 100},
        "risk": {
            "max_daily_loss": 50_000,
            "max_position_value_per_symbol": 2_000_000,
            "max_total_position_value": 3_000_000,
        },
        "simulation": {"slippage_ticks": 0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return AppConfig.model_validate(base)


def tick(
    symbol: str, when: datetime, price: str | int | float, cum: int | None = None, **kw: Any
) -> Tick:
    return Tick(symbol=symbol, time=when, price=D(price), cum_volume=cum, **kw)


def bar(
    symbol: str,
    start: datetime,
    o: float | int,
    h: float | int,
    low: float | int,
    c: float | int,
    v: int = 1000,
    minutes: int = 1,
) -> Bar:
    return Bar(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=minutes),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=v,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repository]:
    r = Repository(tmp_path / "test.sqlite3")
    yield r
    r.close()


@pytest.fixture
def config() -> AppConfig:
    return make_config()
