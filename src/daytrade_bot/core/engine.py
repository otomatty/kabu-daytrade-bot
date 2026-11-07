"""売買エンジン。

時価・注文の更新・タイマー・利用者の操作は、すべて1本のイベントキューに入り、1つのタスクが順番に
処理する。処理の途中に別の処理が割り込まないので、ロックなしで状態の一貫性が保てる。
優先度は 操作 > 注文の更新 > タイマー > 時価 の順(緊急停止を時価の処理より先に行うため)。

バックテストでは ``push_*`` でイベントを入れて ``drain()`` で処理し尽くす。実時間のモードでは
``run()`` がキューを処理し続け、別タスクのタイマーが0.5秒ごとにイベントを入れる。
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from daytrade_bot.brokers.base import Broker, BrokerError, MarketDataFeed
from daytrade_bot.config import AppConfig
from daytrade_bot.core.bars import BarBuilder
from daytrade_bot.core.clock import JST, Clock
from daytrade_bot.core.events import EventBus
from daytrade_bot.core.models import (
    Bar,
    BrokerOrderUpdate,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionEffect,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from daytrade_bot.core.order_manager import OrderManager
from daytrade_bot.core.portfolio import Portfolio, RoundTrip
from daytrade_bot.core.risk import RiskManager, RiskState
from daytrade_bot.core.session import Phase
from daytrade_bot.core.strategy import Strategy
from daytrade_bot.core.ticksize import round_to_tick
from daytrade_bot.notify import Level, Notifier, NullNotifier
from daytrade_bot.storage.db import Repository, iso

log = logging.getLogger("daytrade_bot.engine")

Mode = Literal["backtest", "paper", "live", "demo"]
LogLevel = Literal["debug", "info", "warning", "error"]

STALE_FEED_REASON = "時価配信が途絶えています"
_SESSION_PHASES = frozenset({Phase.MORNING, Phase.LUNCH, Phase.AFTERNOON, Phase.PRE_CLOSE})


class Command(enum.StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    FLATTEN = "flatten"
    HALT = "halt"
    CLEAR_HALT = "clear_halt"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class _TickEvent:
    tick: Tick


@dataclass(frozen=True, slots=True)
class _UpdateEvent:
    update: BrokerOrderUpdate


@dataclass(frozen=True, slots=True)
class _TimerEvent:
    now: datetime


@dataclass(slots=True)
class _CommandEvent:
    command: Command
    reason: str
    future: asyncio.Future[str] | None = None


_EngineEvent = _TickEvent | _UpdateEvent | _TimerEvent | _CommandEvent


class _EventQueue:
    """優先度付きの FIFO キュー(優先度ごとに deque を持つ)。"""

    def __init__(self) -> None:
        self._commands: deque[_CommandEvent] = deque()
        self._updates: deque[_UpdateEvent] = deque()
        self._timers: deque[_TimerEvent] = deque()
        self._ticks: deque[_TickEvent] = deque()
        self._wakeup = asyncio.Event()

    def put(self, event: _EngineEvent) -> None:
        match event:
            case _CommandEvent():
                self._commands.append(event)
            case _UpdateEvent():
                self._updates.append(event)
            case _TimerEvent():
                self._timers.append(event)
            case _TickEvent():
                self._ticks.append(event)
        self._wakeup.set()

    def pop(self, *, urgent_only: bool = False) -> _EngineEvent | None:
        if self._commands:
            return self._commands.popleft()
        if self._updates:
            return self._updates.popleft()
        if urgent_only:
            return None
        if self._timers:
            return self._timers.popleft()
        if self._ticks:
            return self._ticks.popleft()
        return None

    async def get(self) -> _EngineEvent:
        while True:
            event = self.pop()
            if event is not None:
                return event
            self._wakeup.clear()
            await self._wakeup.wait()

    def __len__(self) -> int:
        return len(self._commands) + len(self._updates) + len(self._timers) + len(self._ticks)


@dataclass(frozen=True, slots=True)
class DaySummary:
    day: date
    realized: Decimal
    fees: Decimal
    net: Decimal
    trades: tuple[RoundTrip, ...]


@dataclass(slots=True)
class _LogEntry:
    time: datetime
    level: LogLevel
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "time": iso(self.time),
            "level": self.level,
            "kind": self.kind,
            "message": self.message,
            "data": self.data,
        }


class _Context:
    """戦略に渡す読み取り専用のビュー。"""

    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def now(self) -> datetime:
        return self._e.clock.now()

    def position(self, symbol: str) -> int:
        return self._e.portfolio.qty(symbol)

    def avg_price(self, symbol: str) -> Decimal | None:
        pos = self._e.portfolio.positions.get(symbol)
        return pos.avg_price if pos is not None and pos.qty != 0 else None

    def has_active_order(self, symbol: str) -> bool:
        return self._e.orders.has_active(symbol)

    def bars(self, symbol: str) -> Sequence[Bar]:
        return tuple(self._e.bars_history.get(symbol, ()))

    def entries_allowed(self) -> bool:
        e = self._e
        return e.risk.state is RiskState.ACTIVE and e.schedule.entries_allowed(e.clock.now())

    def log(self, message: str) -> None:
        self._e.log("info", "strategy", message)


class Engine:
    def __init__(
        self,
        *,
        config: AppConfig,
        mode: Mode,
        clock: Clock,
        broker: Broker,
        strategy: Strategy[Any],
        feed: MarketDataFeed | None = None,
        repo: Repository | None = None,
        bus: EventBus | None = None,
        notifier: Notifier | None = None,
        run_id: str = "run",
    ) -> None:
        self.config = config
        self.mode: Mode = mode
        self.clock = clock
        self.broker = broker
        self.feed = feed
        self.strategy = strategy
        self.repo = repo
        self.bus = bus or EventBus()
        self.notifier = notifier or NullNotifier()
        self.run_id = run_id
        self.realtime = mode != "backtest"

        self.schedule = config.session.schedule()
        self.risk = RiskManager(config.risk, self.schedule)
        self.portfolio = Portfolio()
        self.orders = OrderManager(broker, on_change=self._on_order_change, id_prefix=mode[:2])
        self.builders = {s: BarBuilder(s, config.strategy.bar_minutes) for s in config.symbols}
        self.bars_history: dict[str, deque[Bar]] = {s: deque(maxlen=600) for s in config.symbols}
        self.ctx = _Context(self)
        self.day: date | None = None
        self.day_summaries: list[DaySummary] = []
        self.equity_curve: list[tuple[datetime, Decimal]] = []
        self.events_log: deque[_LogEntry] = deque(maxlen=300)
        self.started_at: datetime | None = None

        self._queue = _EventQueue()
        self._stopping = False
        self._started = False
        self._running = False
        self._cumulative_before_today = Decimal(0)
        self._flatten_requested = False
        self._flatten_reason = ""
        self._last_flatten_attempt: datetime | None = None
        self._last_reconcile: datetime | None = None
        self._mismatch_streak: dict[str, int] = {}
        self._stale_paused = False
        self._last_equity_minute: datetime | None = None
        self._last_snapshot_at: datetime | None = None
        self._last_tick_publish: dict[str, datetime] = {}
        self._last_deny: dict[tuple[str, str], datetime] = {}
        self._timer_task: asyncio.Task[None] | None = None

        broker.set_update_handler(self.push_update)
        if feed is not None:
            feed.set_tick_handler(self.push_tick)

    # ---- 入力 -------------------------------------------------------------------------------

    def push_tick(self, tick: Tick) -> None:
        if tick.symbol in self.builders:
            self._queue.put(_TickEvent(tick))

    def push_update(self, update: BrokerOrderUpdate) -> None:
        self._queue.put(_UpdateEvent(update))

    def push_timer(self, now: datetime | None = None) -> None:
        self._queue.put(_TimerEvent(now or self.clock.now()))

    async def command(self, command: Command | str, reason: str = "利用者の操作") -> str:
        """利用者の操作を受け付け、処理の結果(説明)を返す。"""
        cmd = Command(command)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._queue.put(_CommandEvent(cmd, reason, future))
        if not self._running:
            # run() が動いていない(バックテストやテスト)ときは、その場で処理する
            await self.drain()
        return await future

    # ---- 起動と停止 -------------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        now = self.clock.now()
        self.started_at = now
        if self.repo is not None:
            self.repo.start_run(
                self.run_id,
                self.mode,
                self.strategy.name,
                now,
                self.config.model_dump(mode="json"),
            )
        await self.broker.start()
        self._ensure_day(now)
        if self.mode == "live":
            await self._restore_live_state(now)
        if self.feed is not None:
            await self.feed.start(self.config.symbols)
        self.log(
            "info",
            "system",
            f"起動しました(モード: {self.mode}、戦略: {self.strategy.name}、"
            f"銘柄: {', '.join(self.config.symbols)})",
        )

    async def run(self) -> None:
        """実時間のモードでイベントを処理し続ける(STOP の操作か ``stop()`` で終わる)。"""
        await self.start()
        self._timer_task = asyncio.create_task(self._timer_loop(), name="engine-timer")
        self._running = True
        try:
            while not self._stopping:
                event = await self._queue.get()
                await self._dispatch(event)
        finally:
            self._running = False
            await self.shutdown()

    def stop(self) -> None:
        self._stopping = True
        self._queue.put(_TimerEvent(self.clock.now()))

    async def shutdown(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer_task
            self._timer_task = None
        if self.feed is not None:
            try:
                await self.feed.stop()
            except Exception:  # 停止処理は最後まで進める
                log.exception("時価配信の停止中にエラー")
        try:
            await self.broker.stop()
        except Exception:
            log.exception("ブローカーの停止中にエラー")
        now = self.clock.now()
        if self.portfolio.open_symbols():
            self.log(
                "warning",
                "system",
                f"建玉が残ったまま停止しました: {', '.join(self.portfolio.open_symbols())}",
            )
        self.log("info", "system", "停止しました")
        if self.repo is not None:
            self.repo.flush_ticks()
            self.repo.end_run(self.run_id, now)
        await self.notifier.aclose()

    async def _timer_loop(self) -> None:
        while not self._stopping:
            await self.clock.sleep(0.5)
            self._queue.put(_TimerEvent(self.clock.now()))

    async def drain(self) -> None:
        """キューに溜まったイベントをすべて処理する(バックテストとテスト用)。"""
        while (event := self._queue.pop()) is not None:
            await self._dispatch(event)

    async def _drain_urgent(self) -> None:
        while (event := self._queue.pop(urgent_only=True)) is not None:
            await self._dispatch(event)

    async def _dispatch(self, event: _EngineEvent) -> None:
        try:
            match event:
                case _TickEvent(tick):
                    await self._on_tick(tick)
                case _UpdateEvent(update):
                    await self._on_update(update)
                case _TimerEvent(now):
                    await self._on_timer(now)
                case _CommandEvent():
                    await self._on_command(event)
        except Exception as exc:
            log.exception("イベント処理中のエラー")
            now = self.clock.now()
            self.log("error", "system", f"内部エラー: {type(exc).__name__}: {exc}")
            if self.risk.record_error(now):
                await self._on_halt(now)
            if isinstance(event, _CommandEvent) and event.future and not event.future.done():
                event.future.set_exception(exc)

    # ---- 取引日の切り替え ---------------------------------------------------------------------

    def _ensure_day(self, now: datetime) -> None:
        today = now.astimezone(JST).date()
        if self.day == today:
            return
        if self.day is not None:
            self.close_day()
        self.day = today
        self.risk.new_day()
        self._flatten_requested = False
        self._mismatch_streak.clear()
        self._stale_paused = False
        for symbol, history in self.bars_history.items():
            history.clear()
            builder = BarBuilder(symbol, self.config.strategy.bar_minutes)
            if now.astimezone(JST).time() < self.schedule.times.morning_open:
                # 寄付き前から動いていれば、売買高の累計は0から始まる
                builder.seed_volume(today, 0)
            self.builders[symbol] = builder
        self.strategy.on_session_start(today, self.ctx)
        if self.schedule.calendar.is_trading_day(today):
            self.log("info", "system", f"{today:%Y-%m-%d} の取引を開始します")

    def close_day(self) -> DaySummary | None:
        """当日の損益を確定して記録する(日付が変わったときとバックテストの終わりに呼ぶ)。"""
        if self.day is None:
            return None
        p = self.portfolio
        summary = DaySummary(
            day=self.day,
            realized=p.realized_pnl,
            fees=p.fees,
            net=p.realized_pnl - p.fees,
            trades=tuple(p.round_trips),
        )
        has_activity = bool(p.round_trips) or p.realized_pnl != 0 or p.fees != 0
        if has_activity or self.schedule.calendar.is_trading_day(self.day):
            self.day_summaries.append(summary)
        self._cumulative_before_today += summary.net
        self.portfolio.reset_day()
        return summary

    @property
    def cumulative_pnl(self) -> Decimal:
        """起動してからの累積損益(当日の評価損益を含む)。"""
        return self._cumulative_before_today + self.portfolio.net_pnl()

    # ---- ハンドラ ---------------------------------------------------------------------------

    async def _on_tick(self, tick: Tick) -> None:
        self._ensure_day(tick.time)
        if self.repo is not None and self.config.storage.record_ticks and self.mode != "backtest":
            self.repo.buffer_tick(tick)
        await self.broker.on_tick(tick)
        await self._drain_urgent()
        self.portfolio.mark(tick.symbol, tick.price)
        for bar in self.builders[tick.symbol].on_tick(tick):
            await self._on_bar(bar)
        signals = self.strategy.on_tick(tick, self.ctx)
        if signals:
            await self._process_signals(signals, tick.price)
        if self.risk.check_daily_loss(self.portfolio, self.clock.now()):
            await self._on_halt(self.clock.now())
        self._publish_tick(tick)

    async def _on_bar(self, bar: Bar) -> None:
        self.bars_history[bar.symbol].append(bar)
        self.bus.publish({"type": "bar", "bar": bar_json(bar)})
        signals = self.strategy.on_bar(bar, self.ctx)
        if signals:
            await self._process_signals(signals, bar.close)

    async def _on_update(self, update: BrokerOrderUpdate) -> None:
        fills = self.orders.apply_update(update)
        for fill in fills:
            await self._on_fill(fill)

    async def _on_fill(self, fill: Fill) -> None:
        realized = self.portfolio.apply_fill(fill)
        if self.repo is not None:
            self.repo.save_fill(self.run_id, self.mode, fill)
        self.strategy.on_fill(fill, self.ctx)
        effect = "新規" if fill.effect is PositionEffect.OPEN else "返済"
        text = f"約定 {fill.symbol} {effect}{fill.side.label} {fill.qty}株 @{fill.price:,}"
        if fill.effect is PositionEffect.CLOSE:
            text += f"(確定損益 {realized:+,.0f}円)"
        self.log("info", "fill", text, fill=fill_json(fill))
        self.bus.publish({"type": "fill", "fill": fill_json(fill)})
        now = self.clock.now()
        if self.risk.check_daily_loss(self.portfolio, now):
            await self._on_halt(now)

    async def _on_timer(self, now: datetime) -> None:
        self._ensure_day(now)
        for builder in list(self.builders.values()):
            for bar in builder.flush(now):
                await self._on_bar(bar)
        phase = self.schedule.phase(now)
        if phase.is_continuous:
            if self.schedule.must_flatten(now) and not self._flatten_requested:
                self._request_flatten("強制手仕舞いの時刻になりました")
            if self._flatten_requested:
                await self._flatten(now)
        if self.realtime:
            self._check_stale_feed(now, phase)
            await self._expire_limit_orders(now)
            interval = timedelta(seconds=self.config.risk.reconcile_interval_sec)
            if self._last_reconcile is None or now - self._last_reconcile >= interval:
                self._last_reconcile = now
                await self.reconcile(now)
        self._sample_equity(now)
        if self.repo is not None:
            self.repo.flush_ticks()
        if self._last_snapshot_at is None or now - self._last_snapshot_at >= timedelta(seconds=1):
            self._last_snapshot_at = now
            if self.bus.subscriber_count:
                self.bus.publish({"type": "snapshot", "snapshot": self.snapshot()})

    async def _on_command(self, event: _CommandEvent) -> None:
        now = self.clock.now()
        cmd = event.command
        result: str
        if cmd is Command.PAUSE:
            result = (
                "新規エントリーを停止しました"
                if self.risk.pause(event.reason)
                else f"現在の状態({self.risk.state.label})では一時停止できません"
            )
        elif cmd is Command.RESUME:
            self._stale_paused = False
            self._flatten_requested = False
            result = (
                "新規エントリーを再開しました"
                if self.risk.resume()
                else f"現在の状態({self.risk.state.label})では再開できません"
            )
        elif cmd is Command.FLATTEN:
            self.risk.pause("全決済の操作")
            self._request_flatten(event.reason)
            await self._flatten(now)
            result = "全決済を開始しました(新規エントリーは停止)"
        elif cmd is Command.HALT:
            self.risk.halt(f"緊急停止: {event.reason}", now)
            await self._on_halt(now)
            result = "緊急停止しました。建玉をすべて決済します"
        elif cmd is Command.CLEAR_HALT:
            cleared = self.risk.clear_halt()
            if cleared:
                self._flatten_requested = False
            result = (
                "緊急停止を解除しました(新規停止の状態です)"
                if cleared
                else "緊急停止中ではありません"
            )
        else:
            self._stopping = True
            result = "停止します"
        self.log("warning" if cmd in (Command.HALT, Command.FLATTEN) else "info", "control", result)
        if event.future is not None and not event.future.done():
            event.future.set_result(result)
        if self.bus.subscriber_count:
            self.bus.publish({"type": "snapshot", "snapshot": self.snapshot()})

    async def _on_halt(self, now: datetime) -> None:
        self.log("error", "risk", f"緊急停止: {self.risk.reason}")
        self._request_flatten(self.risk.reason)
        await self._flatten(now)

    # ---- シグナルから発注へ -------------------------------------------------------------------

    async def _process_signals(self, signals: Sequence[Signal], price: Decimal) -> None:
        for signal in signals:
            if signal.symbol not in self.builders:
                self.log(
                    "warning", "strategy", f"対象外の銘柄のシグナルを無視しました: {signal.symbol}"
                )
                continue
            req = self._build_request(signal, price)
            if req is None:
                continue
            await self._submit(req, price, signal.reason)

    def _build_request(self, signal: Signal, price: Decimal) -> OrderRequest | None:
        now = self.clock.now()
        symbol = signal.symbol
        position = self.portfolio.qty(symbol)
        if signal.action is SignalAction.EXIT:
            if position == 0:
                return None
            pending_close = abs(self.orders.pending_qty(symbol, PositionEffect.CLOSE))
            qty = abs(position) - pending_close
            if signal.qty is not None:
                qty = min(qty, signal.qty)
            if qty <= 0:
                return None
            side = Side.SELL if position > 0 else Side.BUY
            effect = PositionEffect.CLOSE
        else:
            side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
            if self.orders.has_active(symbol):
                self._deny_once(symbol, "未約定の注文があるため、新しいエントリーを見送りました")
                return None
            if position != 0:
                self._deny_once(symbol, "すでに建玉があるため、新しいエントリーを見送りました")
                return None
            qty = signal.qty if signal.qty is not None else self._size(price)
            lot = self.config.sizing.lot_size
            qty = (qty // lot) * lot
            if qty <= 0:
                self._deny_once(symbol, f"資金配分では1単元({lot}株)も買えません(株価 {price:,})")
                return None
            effect = PositionEffect.OPEN
        limit_price: Decimal | None = None
        if signal.order_type is OrderType.LIMIT and signal.limit_price is not None:
            limit_price = round_to_tick(
                signal.limit_price, self.config.tick_table(symbol), up=side is Side.SELL
            )
        return self.orders.new_request(
            symbol=symbol,
            side=side,
            qty=qty,
            effect=effect,
            now=now,
            order_type=signal.order_type,
            limit_price=limit_price,
            tag=signal.reason[:200],
        )

    def _size(self, price: Decimal) -> int:
        sizing = self.config.sizing
        if sizing.method == "fixed_qty":
            return sizing.qty
        units = (sizing.notional / price / sizing.lot_size).to_integral_value(rounding=ROUND_FLOOR)
        return int(units) * sizing.lot_size

    async def _submit(self, req: OrderRequest, price: Decimal, reason: str) -> Order | None:
        now = self.clock.now()
        pending_open = self.orders.pending_qty(req.symbol, PositionEffect.OPEN)
        decision = self.risk.check(req, price, self.portfolio, now, pending_open)
        if not decision.approved:
            self._deny_once(req.symbol, f"発注を見送りました({req.symbol}): {decision.reason}")
            return None
        self.risk.record_order(now)
        result = await self.orders.submit(req, now)
        for fill in self.orders.take_replayed_fills():
            await self._on_fill(fill)
        order = result.order
        effect = "新規" if req.effect is PositionEffect.OPEN else "返済"
        kind = "成行" if req.order_type is OrderType.MARKET else f"指値{req.limit_price:,}"
        summary = f"{req.symbol} {effect}{req.side.label} {req.qty}株 {kind}"
        if order.status is OrderStatus.REJECTED:
            self.log("error", "order", f"発注が拒否されました: {summary}: {result.error}")
            if self.risk.record_error(now):
                await self._on_halt(now)
        elif order.status is OrderStatus.UNKNOWN:
            self.log("warning", "order", f"発注の結果が不明です。照合で確認します: {summary}")
            self._last_reconcile = None  # 次のタイマーで照合する
        else:
            self.risk.record_success()
            self.log("info", "order", f"発注: {summary}({reason})")
        return order

    def _deny_once(self, symbol: str, message: str) -> None:
        """同じ理由の見送りを1分に1回だけ記録する(ログがあふれないように)。"""
        now = self.clock.now()
        key = (symbol, message)
        last = self._last_deny.get(key)
        if last is None or now - last >= timedelta(minutes=1):
            self._last_deny[key] = now
            self.log("info", "risk", message)

    # ---- 手仕舞い ---------------------------------------------------------------------------

    def _request_flatten(self, reason: str) -> None:
        if not self._flatten_requested:
            self._flatten_requested = True
            self._flatten_reason = reason
            self.log("warning", "risk", f"全決済します: {reason}")

    async def _flatten(self, now: datetime) -> None:
        """未約定の新規注文を取り消し、すべての建玉を成行で返済する。ザラ場でなければ待つ。"""
        if not self.schedule.is_continuous(now):
            return
        if self._last_flatten_attempt is not None and now - self._last_flatten_attempt < timedelta(
            seconds=3
        ):
            return
        self._last_flatten_attempt = now
        for order in self.orders.active_orders():
            if order.request.effect is PositionEffect.OPEN and order.status in (
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                try:
                    await self.orders.cancel(order.client_order_id, now)
                except BrokerError as exc:
                    self.log("warning", "order", f"取消に失敗しました({order.symbol}): {exc}")
        for symbol in self.portfolio.open_symbols():
            position = self.portfolio.qty(symbol)
            pending_close = abs(self.orders.pending_qty(symbol, PositionEffect.CLOSE))
            qty = abs(position) - pending_close
            if qty <= 0:
                continue
            req = self.orders.new_request(
                symbol=symbol,
                side=Side.SELL if position > 0 else Side.BUY,
                qty=qty,
                effect=PositionEffect.CLOSE,
                now=now,
                tag=f"全決済: {self._flatten_reason}"[:200],
            )
            price = self.portfolio.last_prices.get(symbol, Decimal(1))
            await self._submit(req, price, f"全決済: {self._flatten_reason}")

    async def submit_closing_orders_unchecked(self, reason: str) -> int:
        """残っている建玉に、リスク判定を通さずに成行の返済注文を出す(バックテストの精算専用)。"""
        now = self.clock.now()
        count = 0
        for symbol in self.portfolio.open_symbols():
            position = self.portfolio.qty(symbol)
            qty = abs(position) - abs(self.orders.pending_qty(symbol, PositionEffect.CLOSE))
            if qty <= 0:
                continue
            req = self.orders.new_request(
                symbol=symbol,
                side=Side.SELL if position > 0 else Side.BUY,
                qty=qty,
                effect=PositionEffect.CLOSE,
                now=now,
                tag=reason[:200],
            )
            await self.orders.submit(req, now)
            for fill in self.orders.take_replayed_fills():
                await self._on_fill(fill)
            count += 1
        return count

    async def _expire_limit_orders(self, now: datetime) -> None:
        timeout = self.config.trading.limit_order_timeout_sec
        for order in self.orders.stale_limit_orders(now, timeout):
            try:
                await self.orders.cancel(order.client_order_id, now)
                self.log(
                    "info",
                    "order",
                    f"指値が{timeout:.0f}秒約定しないため取り消します: {order.symbol}",
                )
            except BrokerError as exc:
                self.log("warning", "order", f"取消に失敗しました({order.symbol}): {exc}")

    # ---- 監視 -------------------------------------------------------------------------------

    def _check_stale_feed(self, now: datetime, phase: Phase) -> None:
        if self.feed is None or not phase.is_continuous:
            return
        last = self.feed.last_message_at
        limit = timedelta(seconds=self.config.risk.stale_data_sec)
        started = self.started_at or now
        stale = (last is None and now - started > limit) or (
            last is not None and now - last > limit
        )
        if stale and not self._stale_paused and self.risk.state is RiskState.ACTIVE:
            self._stale_paused = True
            self.risk.pause(STALE_FEED_REASON)
            self.log(
                "warning",
                "feed",
                f"時価配信が{self.config.risk.stale_data_sec:.0f}秒以上届いていないため、"
                "新規エントリーを止めました",
            )
        elif not stale and self._stale_paused:
            self._stale_paused = False
            if self.risk.state is RiskState.PAUSED and self.risk.reason == STALE_FEED_REASON:
                self.risk.resume()
                self.log("info", "feed", "時価配信が復旧したため、新規エントリーを再開しました")

    async def reconcile(self, now: datetime) -> None:
        """証券会社側の注文・建玉と照合する。不一致が2回続いたら緊急停止する。"""
        try:
            snapshots = await self.broker.fetch_orders()
            positions = await self.broker.fetch_positions()
        except BrokerError as exc:
            self.log("warning", "reconcile", f"照合のための取得に失敗しました: {exc}")
            if self.risk.record_error(now):
                await self._on_halt(now)
            return
        for order, message in self.orders.resolve_unknown(snapshots, now):
            self.log("warning", "reconcile", f"{order.symbol}: {message}")
        for fill in self.orders.take_replayed_fills():
            await self._on_fill(fill)
        broker_qty = {p.symbol: p.qty for p in positions}
        for symbol in self.config.symbols:
            ours = self.portfolio.qty(symbol)
            theirs = broker_qty.get(symbol, 0)
            if ours == theirs or self.orders.has_active(symbol):
                self._mismatch_streak.pop(symbol, None)
                continue
            streak = self._mismatch_streak.get(symbol, 0) + 1
            self._mismatch_streak[symbol] = streak
            if streak < 2:
                continue
            # 証券会社の建玉を正として内部の状態を合わせ、緊急停止して決済する
            self._mismatch_streak.pop(symbol, None)
            pos = next((p for p in positions if p.symbol == symbol), None)
            self.portfolio.set_position(symbol, theirs, pos.avg_price if pos else Decimal(0), now)
            message = (
                f"建玉が証券会社と一致しません({symbol}: ボット {ours}株 / 証券会社 {theirs}株)"
            )
            self.log("error", "reconcile", message)
            self.risk.halt(message, now)
            await self._on_halt(now)

    async def _restore_live_state(self, now: datetime) -> None:
        """本番の再起動時に、当日の約定と証券会社の建玉から状態を復元する。"""
        if self.repo is not None and self.day is not None:
            for fill in self.repo.load_fills("live", self.day):
                if fill.symbol in self.builders:
                    self.portfolio.apply_fill(fill)
        try:
            positions = await self.broker.fetch_positions()
        except BrokerError as exc:
            self.risk.pause("起動時に建玉を確認できませんでした")
            self.log("error", "reconcile", f"起動時の建玉の取得に失敗しました: {exc}")
            return
        for pos in positions:
            if pos.symbol not in self.builders:
                continue
            if self.portfolio.qty(pos.symbol) != pos.qty:
                self.log(
                    "warning",
                    "reconcile",
                    f"{pos.symbol}: 停止中の約定を検知したため、"
                    f"証券会社の建玉({pos.qty}株)に合わせました",
                )
                self.portfolio.set_position(pos.symbol, pos.qty, pos.avg_price, now)
        for symbol in self.config.symbols:
            if symbol not in {p.symbol for p in positions} and self.portfolio.qty(symbol) != 0:
                self.portfolio.set_position(symbol, 0, Decimal(0), now)
        if self.portfolio.open_symbols():
            held = ", ".join(
                f"{s} {self.portfolio.qty(s)}株" for s in self.portfolio.open_symbols()
            )
            self.log("warning", "reconcile", f"起動時点の建玉: {held}")

    def _sample_equity(self, now: datetime) -> None:
        minute = now.replace(second=0, microsecond=0)
        if self._last_equity_minute == minute:
            return
        if self.schedule.phase(now) not in _SESSION_PHASES:
            return
        self._last_equity_minute = minute
        self.equity_curve.append((minute, self.cumulative_pnl))
        if len(self.equity_curve) > 20_000:
            del self.equity_curve[:5_000]

    # ---- 記録と配信 -------------------------------------------------------------------------

    def log(self, level: LogLevel, kind: str, message: str, **data: Any) -> None:
        now = self.clock.now()
        getattr(log, level)("[%s] %s", kind, message)
        if level == "debug":
            return
        entry = _LogEntry(now, level, kind, message, dict(data))
        self.events_log.append(entry)
        if self.repo is not None and self.mode != "backtest":
            self.repo.save_event(self.run_id, now, level, kind, message, data=data)
        self.bus.publish({"type": "event", "event": entry.to_json()})
        if level in ("warning", "error") or kind in ("fill", "control"):
            notify_level: Level = (
                "error" if level == "error" else ("warning" if level == "warning" else "info")
            )
            self.notifier.post(notify_level, f"{self.mode}: {kind}", message)

    def _on_order_change(self, order: Order) -> None:
        if self.repo is not None and self.mode != "backtest":
            self.repo.save_order(self.run_id, self.mode, order)
        self.bus.publish({"type": "order", "order": order_json(order)})

    def _publish_tick(self, tick: Tick) -> None:
        if not self.bus.subscriber_count:
            return
        last = self._last_tick_publish.get(tick.symbol)
        if last is not None and tick.time - last < timedelta(milliseconds=250):
            return
        self._last_tick_publish[tick.symbol] = tick.time
        current = self.builders[tick.symbol].current
        self.bus.publish(
            {
                "type": "tick",
                "symbol": tick.symbol,
                "price": float(tick.price),
                "time": iso(tick.time),
                "bar": bar_json(current) if current else None,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        p = self.portfolio
        names = {s.symbol: s.name for s in self.config.universe}
        trips = p.round_trips
        wins = sum(1 for t in trips if t.is_win)
        feed = self.feed
        last_msg = feed.last_message_at if feed is not None else None
        phase = self.schedule.phase(now)
        orders = sorted(self.orders.all_orders(), key=lambda o: o.updated_at, reverse=True)
        cfg = self.config
        return {
            "mode": self.mode,
            "run_id": self.run_id,
            "now": iso(now),
            "day": self.day.isoformat() if self.day else None,
            "phase": {"code": phase.value, "label": phase.label},
            "entries_allowed": self.ctx.entries_allowed(),
            "strategy": self.strategy.describe(),
            "risk": {
                "state": self.risk.state.value,
                "label": self.risk.state.label,
                "reason": self.risk.reason,
                "loss_usage": self.risk.loss_usage(p),
                "consecutive_errors": self.risk.consecutive_errors,
                "flattening": self._flatten_requested,
                "limits": {
                    "max_daily_loss": float(cfg.risk.max_daily_loss),
                    "max_position_value_per_symbol": float(cfg.risk.max_position_value_per_symbol),
                    "max_total_position_value": float(cfg.risk.max_total_position_value),
                    "max_open_positions": cfg.risk.max_open_positions,
                    "max_orders_per_minute": cfg.risk.max_orders_per_minute,
                    "entry_cutoff": cfg.session.entry_cutoff.strftime("%H:%M"),
                    "flatten_at": cfg.session.flatten_at.strftime("%H:%M"),
                },
                "exposure": float(p.total_exposure()),
            },
            "feed": {
                "name": feed.name if feed is not None else None,
                "connected": feed.connected if feed is not None else None,
                "last_message_age_sec": (now - last_msg).total_seconds() if last_msg else None,
            },
            "pnl": {
                "realized": float(p.realized_pnl),
                "unrealized": float(p.total_unrealized()),
                "fees": float(p.fees),
                "net": float(p.net_pnl()),
                "cumulative": float(self.cumulative_pnl),
                "trades": len(trips),
                "wins": wins,
                "win_rate": (wins / len(trips)) if trips else None,
            },
            "positions": [
                {
                    "symbol": s,
                    "name": names.get(s, ""),
                    "qty": pos.qty,
                    "avg_price": float(pos.avg_price),
                    "last": float(p.last_prices[s]) if s in p.last_prices else None,
                    "unrealized": float(p.unrealized(s)),
                    "exposure": float(p.exposure(s)),
                }
                for s, pos in p.positions.items()
                if pos.qty != 0
            ],
            "symbols": [
                {
                    "symbol": s.symbol,
                    "name": s.name,
                    "last": float(p.last_prices[s.symbol]) if s.symbol in p.last_prices else None,
                }
                for s in cfg.universe
            ],
            "orders": [order_json(o) for o in orders[:100]],
            "round_trips": [trip_json(t) for t in reversed(trips[-100:])],
            "equity": [
                [iso(t), float(v)]
                for t, v in self.equity_curve[-600:]
                if self.day is None or t.date() == self.day
            ],
            "events": [e.to_json() for e in reversed(self.events_log)],
        }


# ---- JSON 変換(ダッシュボード用) ---------------------------------------------------------------


def bar_json(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "start": iso(bar.start),
        "time": int(bar.start.timestamp()),
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": bar.volume,
    }


def fill_json(fill: Fill) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "client_order_id": fill.client_order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "side_label": fill.side.label,
        "effect": fill.effect.value,
        "qty": fill.qty,
        "price": float(fill.price),
        "fee": float(fill.fee),
        "time": iso(fill.time),
        "unix": int(fill.time.timestamp()),
    }


def order_json(order: Order) -> dict[str, Any]:
    req = order.request
    return {
        "client_order_id": req.client_order_id,
        "broker_order_id": order.broker_order_id,
        "symbol": req.symbol,
        "side": req.side.value,
        "side_label": req.side.label,
        "effect": req.effect.value,
        "qty": req.qty,
        "filled_qty": order.filled_qty,
        "order_type": req.order_type.value,
        "limit_price": float(req.limit_price) if req.limit_price is not None else None,
        "avg_fill_price": float(order.avg_fill_price) if order.avg_fill_price is not None else None,
        "status": order.status.value,
        "status_label": order.status.label,
        "active": order.status.is_active,
        "reason": order.reason,
        "tag": req.tag,
        "created_at": iso(req.created_at),
        "updated_at": iso(order.updated_at),
    }


def trip_json(trip: RoundTrip) -> dict[str, Any]:
    return {
        "symbol": trip.symbol,
        "side": trip.side.value,
        "side_label": trip.side.label,
        "qty": trip.qty,
        "entry_price": float(trip.entry_price),
        "exit_price": float(trip.exit_price),
        "opened_at": iso(trip.opened_at),
        "closed_at": iso(trip.closed_at),
        "pnl": float(trip.pnl),
        "fees": float(trip.fees),
        "holding_minutes": (trip.closed_at - trip.opened_at).total_seconds() / 60,
    }
