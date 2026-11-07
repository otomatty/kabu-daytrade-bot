"""SQLite への保存(ティック、足、注文、約定、イベント、バックテスト結果)。

価格は文字列(Decimal の表現)で保存し、読み込み時に Decimal に戻す。
時刻はタイムゾーン付きの ISO 8601 で保存する(JST に揃えるので文字列の順序が時刻の順序になる)。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from daytrade_bot.core.clock import JST
from daytrade_bot.core.models import (
    Bar,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Tick,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    price TEXT NOT NULL,
    cum_volume INTEGER,
    bid TEXT,
    ask TEXT
);
CREATE INDEX IF NOT EXISTS ticks_symbol_ts ON ticks(symbol, ts);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (symbol, start)
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    effect TEXT NOT NULL,
    limit_price TEXT,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    filled_qty INTEGER NOT NULL,
    avg_fill_price TEXT,
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    effect TEXT NOT NULL,
    time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_time ON fills(time);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE TABLE IF NOT EXISTS backtests (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    symbols TEXT NOT NULL,
    summary TEXT NOT NULL,
    equity TEXT NOT NULL,
    trades TEXT NOT NULL,
    daily TEXT NOT NULL
);
"""


def iso(value: datetime) -> str:
    return value.astimezone(JST).isoformat(timespec="microseconds")


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: int
    run_id: str
    time: datetime
    level: str
    kind: str
    message: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BacktestRecord:
    run_id: str
    created_at: datetime
    strategy: str
    params: dict[str, Any]
    start_date: date
    end_date: date
    symbols: list[str]
    summary: dict[str, Any]
    equity: list[list[Any]]
    trades: list[dict[str, Any]]
    daily: list[dict[str, Any]]


class Repository:
    """スレッドセーフな SQLite のラッパー(ダッシュボードとエンジンで共有する)。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._tick_buffer: list[tuple[str, str, str, int | None, str | None, str | None]] = []
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"データベースのバージョン({version})がこのプログラム"
                    f"({SCHEMA_VERSION})より新しいです"
                )
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.flush_ticks()
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ---- 実行の記録 -------------------------------------------------------------------------

    def start_run(
        self, run_id: str, mode: str, strategy: str, started_at: datetime, config: dict[str, Any]
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, mode, strategy, started_at, config) VALUES (?,?,?,?,?)",
                (run_id, mode, strategy, iso(started_at), json.dumps(config, ensure_ascii=False)),
            )

    def end_run(self, run_id: str, ended_at: datetime) -> None:
        with self._tx() as c:
            c.execute("UPDATE runs SET ended_at=? WHERE run_id=?", (iso(ended_at), run_id))

    # ---- ティック ---------------------------------------------------------------------------

    def buffer_tick(self, tick: Tick) -> None:
        self._tick_buffer.append(
            (
                tick.symbol,
                iso(tick.time),
                str(tick.price),
                tick.cum_volume,
                str(tick.bid) if tick.bid is not None else None,
                str(tick.ask) if tick.ask is not None else None,
            )
        )
        if len(self._tick_buffer) >= 500:
            self.flush_ticks()

    def flush_ticks(self) -> int:
        if not self._tick_buffer:
            return 0
        rows, self._tick_buffer = self._tick_buffer, []
        with self._tx() as c:
            c.executemany(
                "INSERT INTO ticks(symbol, ts, price, cum_volume, bid, ask) VALUES (?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def iter_ticks(self, symbols: Sequence[str], start: datetime, end: datetime) -> list[Tick]:
        placeholders = ",".join("?" for _ in symbols)
        rows = self._query(
            f"SELECT symbol, ts, price, cum_volume, bid, ask FROM ticks "  # noqa: S608 - プレースホルダのみ
            f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts < ? ORDER BY ts, rowid",
            [*symbols, iso(start), iso(end)],
        )
        return [
            Tick(
                symbol=r["symbol"],
                time=parse_dt(r["ts"]),
                price=Decimal(r["price"]),
                cum_volume=r["cum_volume"],
                bid=_dec(r["bid"]),
                ask=_dec(r["ask"]),
            )
            for r in rows
        ]

    def tick_days(self, symbol: str | None = None) -> list[date]:
        sql = "SELECT DISTINCT substr(ts, 1, 10) AS d FROM ticks"
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol=?"
            params.append(symbol)
        return [date.fromisoformat(r["d"]) for r in self._query(sql + " ORDER BY d", params)]

    # ---- 足 ---------------------------------------------------------------------------------

    def save_bars(self, bars: Iterable[Bar]) -> int:
        rows = [
            (
                b.symbol,
                iso(b.start),
                iso(b.end),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                b.volume,
            )
            for b in bars
        ]
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO bars(symbol, start, end, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def load_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        rows = self._query(
            "SELECT * FROM bars WHERE symbol=? AND start >= ? AND start < ? ORDER BY start",
            (symbol, iso(start), iso(end)),
        )
        return [
            Bar(
                symbol=r["symbol"],
                start=parse_dt(r["start"]),
                end=parse_dt(r["end"]),
                open=Decimal(r["open"]),
                high=Decimal(r["high"]),
                low=Decimal(r["low"]),
                close=Decimal(r["close"]),
                volume=r["volume"],
            )
            for r in rows
        ]

    def bar_days(self, symbol: str) -> list[date]:
        rows = self._query(
            "SELECT DISTINCT substr(start, 1, 10) AS d FROM bars WHERE symbol=? ORDER BY d",
            (symbol,),
        )
        return [date.fromisoformat(r["d"]) for r in rows]

    # ---- 注文と約定 -------------------------------------------------------------------------

    def save_order(self, run_id: str, mode: str, order: Order) -> None:
        req = order.request
        with self._tx() as c:
            c.execute(
                """INSERT INTO orders(client_order_id, run_id, mode, symbol, side, qty, order_type,
                    effect, limit_price, status, broker_order_id, filled_qty, avg_fill_price, tag,
                    reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_order_id) DO UPDATE SET status=excluded.status,
                    broker_order_id=excluded.broker_order_id, filled_qty=excluded.filled_qty,
                    avg_fill_price=excluded.avg_fill_price, reason=excluded.reason,
                    updated_at=excluded.updated_at""",
                (
                    req.client_order_id,
                    run_id,
                    mode,
                    req.symbol,
                    req.side.value,
                    req.qty,
                    req.order_type.value,
                    req.effect.value,
                    str(req.limit_price) if req.limit_price is not None else None,
                    order.status.value,
                    order.broker_order_id,
                    order.filled_qty,
                    str(order.avg_fill_price) if order.avg_fill_price is not None else None,
                    req.tag,
                    order.reason,
                    iso(req.created_at),
                    iso(order.updated_at),
                ),
            )

    def load_orders(self, mode: str, day: date) -> list[Order]:
        start, end = _day_range(day)
        rows = self._query(
            "SELECT * FROM orders WHERE mode=? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at",
            (mode, start, end),
        )
        return [_row_to_order(r) for r in rows]

    def save_fill(self, run_id: str, mode: str, fill: Fill) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO fills(fill_id, run_id, mode, client_order_id, symbol, side,
                    qty, price, fee, effect, time) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fill.fill_id,
                    run_id,
                    mode,
                    fill.client_order_id,
                    fill.symbol,
                    fill.side.value,
                    fill.qty,
                    str(fill.price),
                    str(fill.fee),
                    fill.effect.value,
                    iso(fill.time),
                ),
            )

    def load_fills(self, mode: str, day: date) -> list[Fill]:
        start, end = _day_range(day)
        rows = self._query(
            "SELECT * FROM fills WHERE mode=? AND time >= ? AND time < ? ORDER BY time, rowid",
            (mode, start, end),
        )
        return [
            Fill(
                fill_id=r["fill_id"],
                client_order_id=r["client_order_id"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                qty=r["qty"],
                price=Decimal(r["price"]),
                fee=Decimal(r["fee"]),
                time=parse_dt(r["time"]),
                effect=PositionEffect(r["effect"]),
            )
            for r in rows
        ]

    # ---- イベント ---------------------------------------------------------------------------

    def save_event(
        self,
        run_id: str,
        when: datetime,
        level: str,
        kind: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO events(run_id, time, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    iso(when),
                    level,
                    kind,
                    message,
                    json.dumps(data or {}, ensure_ascii=False, default=str),
                ),
            )

    def recent_events(self, limit: int = 200, run_id: str | None = None) -> list[StoredEvent]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if run_id:
            sql += " WHERE run_id=?"
            params.append(run_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            StoredEvent(
                id=r["id"],
                run_id=r["run_id"],
                time=parse_dt(r["time"]),
                level=r["level"],
                kind=r["kind"],
                message=r["message"],
                data=json.loads(r["data"]),
            )
            for r in self._query(sql, params)
        ]

    # ---- バックテスト -----------------------------------------------------------------------

    def save_backtest(self, record: BacktestRecord) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO backtests(run_id, created_at, strategy, params,
                    start_date, end_date, symbols, summary, equity, trades, daily)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.run_id,
                    iso(record.created_at),
                    record.strategy,
                    _dumps(record.params),
                    record.start_date.isoformat(),
                    record.end_date.isoformat(),
                    _dumps(record.symbols),
                    _dumps(record.summary),
                    _dumps(record.equity),
                    _dumps(record.trades),
                    _dumps(record.daily),
                ),
            )

    def list_backtests(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT run_id, created_at, strategy, params, start_date, end_date, symbols, summary "
            "FROM backtests ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "run_id": r["run_id"],
                "created_at": r["created_at"],
                "strategy": r["strategy"],
                "params": json.loads(r["params"]),
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "symbols": json.loads(r["symbols"]),
                "summary": json.loads(r["summary"]),
            }
            for r in rows
        ]

    def get_backtest(self, run_id: str) -> BacktestRecord | None:
        rows = self._query("SELECT * FROM backtests WHERE run_id=?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        return BacktestRecord(
            run_id=r["run_id"],
            created_at=parse_dt(r["created_at"]),
            strategy=r["strategy"],
            params=json.loads(r["params"]),
            start_date=date.fromisoformat(r["start_date"]),
            end_date=date.fromisoformat(r["end_date"]),
            symbols=json.loads(r["symbols"]),
            summary=json.loads(r["summary"]),
            equity=json.loads(r["equity"]),
            trades=json.loads(r["trades"]),
            daily=json.loads(r["daily"]),
        )


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _day_range(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(0), tzinfo=JST)
    return iso(start), iso(start + timedelta(days=1))


def _row_to_order(r: sqlite3.Row) -> Order:
    req = OrderRequest(
        client_order_id=r["client_order_id"],
        symbol=r["symbol"],
        side=Side(r["side"]),
        qty=r["qty"],
        order_type=OrderType(r["order_type"]),
        effect=PositionEffect(r["effect"]),
        created_at=parse_dt(r["created_at"]),
        limit_price=_dec(r["limit_price"]),
        tag=r["tag"],
    )
    return Order(
        request=req,
        status=OrderStatus(r["status"]),
        updated_at=parse_dt(r["updated_at"]),
        broker_order_id=r["broker_order_id"],
        filled_qty=r["filled_qty"],
        avg_fill_price=_dec(r["avg_fill_price"]),
        reason=r["reason"],
    )
