"""リスク管理。発注の最終判断と、キルスイッチ(緊急停止)を受け持つ。

状態は3つ:
- ACTIVE: 通常どおり
- PAUSED: 新規エントリーだけ止める(手仕舞いは通す)。利用者の操作で再開できる
- HALTED: 新規エントリーを止め、エンジンがすべての建玉を閉じる。損失上限や異常で自動的に入る。
  解除は利用者が明示的に行う(同じ日に何度も損失上限に達しないように、自動では戻らない)

手仕舞い(建玉を減らす注文)はリスクを減らす方向なので、取引時間内なら状態にかかわらず通す。
"""

from __future__ import annotations

import enum
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from daytrade_bot.config import RiskConfig
from daytrade_bot.core.models import OrderRequest, PositionEffect
from daytrade_bot.core.portfolio import Portfolio
from daytrade_bot.core.session import TradingSchedule


class RiskState(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    HALTED = "halted"

    @property
    def label(self) -> str:
        return {"active": "稼働中", "paused": "新規停止中", "halted": "緊急停止"}[self.value]


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""

    @staticmethod
    def ok() -> RiskDecision:
        return RiskDecision(True)

    @staticmethod
    def deny(reason: str) -> RiskDecision:
        return RiskDecision(False, reason)


class RiskManager:
    def __init__(self, config: RiskConfig, schedule: TradingSchedule) -> None:
        self.config = config
        self.schedule = schedule
        self.state = RiskState.ACTIVE
        self.reason = ""
        self.halted_at: datetime | None = None
        self._order_times: deque[datetime] = deque()
        self._consecutive_errors = 0

    # ---- 状態の操作 -------------------------------------------------------------------------

    def pause(self, reason: str = "利用者の操作") -> bool:
        """新規エントリーを止める。すでに止まっていれば理由だけを新しくする(緊急停止中は何もしない)。"""
        if self.state is RiskState.HALTED:
            return False
        self.state = RiskState.PAUSED
        self.reason = reason
        return True

    def resume(self) -> bool:
        if self.state is RiskState.PAUSED:
            self.state = RiskState.ACTIVE
            self.reason = ""
            return True
        return False

    def halt(self, reason: str, now: datetime) -> bool:
        """緊急停止に入る。すでに停止中なら何もしない(最初の理由を残す)。"""
        if self.state is RiskState.HALTED:
            return False
        self.state = RiskState.HALTED
        self.reason = reason
        self.halted_at = now
        return True

    def clear_halt(self) -> bool:
        """緊急停止を解除する(利用者の明示的な操作でのみ呼ぶ)。新規停止の状態で再開する。"""
        if self.state is RiskState.HALTED:
            self.state = RiskState.PAUSED
            self.reason = "緊急停止を解除しました(再開ボタンで新規エントリーを再開)"
            self.halted_at = None
            self._consecutive_errors = 0
            return True
        return False

    def new_day(self) -> None:
        """日をまたいだら一時停止と緊急停止を解除し、カウンタを戻す。"""
        self.state = RiskState.ACTIVE
        self.reason = ""
        self.halted_at = None
        self._order_times.clear()
        self._consecutive_errors = 0

    # ---- 発注の判断 -------------------------------------------------------------------------

    def check(
        self,
        req: OrderRequest,
        price: Decimal,
        portfolio: Portfolio,
        now: datetime,
        pending_open: Mapping[str, int] | None = None,
    ) -> RiskDecision:
        """``req`` を送ってよいか。

        ``pending_open`` は銘柄ごとの未約定の新規注文の数量(符号付き)。建玉の上限と銘柄数の上限は、
        約定済みの建玉に未約定の新規注文を足した数で判定する(約定の通知が遅れても上限を超えないように)。
        """
        pending = dict(pending_open or {})
        if req.qty > self.config.max_order_qty:
            return RiskDecision.deny(f"1注文の数量上限({self.config.max_order_qty})を超えています")
        if not self.schedule.is_continuous(now):
            return RiskDecision.deny(f"ザラ場ではありません({self.schedule.phase(now).label})")
        if req.effect is PositionEffect.CLOSE:
            current = portfolio.qty(req.symbol)
            if current == 0 or (current > 0) == (req.signed_qty > 0):
                return RiskDecision.deny("返済する建玉がありません")
            if req.qty > abs(current):
                return RiskDecision.deny(f"返済数量({req.qty})が建玉({abs(current)})を超えています")
            return RiskDecision.ok()

        if self.state is not RiskState.ACTIVE:
            return RiskDecision.deny(
                f"{self.state.label}のため新規エントリーしません: {self.reason}"
            )
        if not self.schedule.entries_allowed(now):
            return RiskDecision.deny("新規エントリーの時間帯ではありません")
        if self._orders_in_last_minute(now) >= self.config.max_orders_per_minute:
            return RiskDecision.deny(
                f"1分あたりの発注上限({self.config.max_orders_per_minute}回)に達しました"
            )
        loss_limit = -self.config.max_daily_loss
        if portfolio.net_pnl() <= loss_limit:
            return RiskDecision.deny("日次損失上限に達しています")

        current_qty = portfolio.qty(req.symbol)
        if current_qty != 0 and (current_qty > 0) != (req.signed_qty > 0):
            return RiskDecision.deny("反対方向の建玉があります。先に返済してください")
        new_symbol_qty = abs(current_qty + pending.get(req.symbol, 0) + req.signed_qty)
        symbol_value = new_symbol_qty * price
        if symbol_value > self.config.max_position_value_per_symbol:
            return RiskDecision.deny(
                f"銘柄ごとの建玉上限({self.config.max_position_value_per_symbol:,}円)を超えます"
                f"(発注後 {symbol_value:,.0f}円)"
            )
        others = (set(portfolio.positions) | set(pending)) - {req.symbol}
        other_value = sum(
            (
                abs(portfolio.qty(s) + pending.get(s, 0))
                * portfolio.last_prices.get(s, portfolio.position(s).avg_price or price)
                for s in others
            ),
            Decimal(0),
        )
        total_value = other_value + symbol_value
        if total_value > self.config.max_total_position_value:
            return RiskDecision.deny(
                f"建玉合計の上限({self.config.max_total_position_value:,}円)を超えます"
                f"(発注後 {total_value:,.0f}円)"
            )
        open_symbols = set(portfolio.open_symbols()) | {s for s, q in pending.items() if q != 0}
        if req.symbol not in open_symbols and len(open_symbols) >= self.config.max_open_positions:
            return RiskDecision.deny(
                f"同時に持てる銘柄数の上限({self.config.max_open_positions})に達しています"
            )
        return RiskDecision.ok()

    def record_order(self, now: datetime) -> None:
        self._order_times.append(now)
        self._trim(now)

    def _orders_in_last_minute(self, now: datetime) -> int:
        self._trim(now)
        return len(self._order_times)

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=1)
        while self._order_times and self._order_times[0] <= cutoff:
            self._order_times.popleft()

    # ---- 異常の監視 -------------------------------------------------------------------------

    def check_daily_loss(self, portfolio: Portfolio, now: datetime) -> bool:
        """日次損失上限に達したら緊急停止に入り、真を返す。"""
        pnl = portfolio.net_pnl()
        if pnl <= -self.config.max_daily_loss:
            limit = self.config.max_daily_loss
            return self.halt(
                f"日次損失上限に達しました(損益 {pnl:,.0f}円 / 上限 -{limit:,}円)", now
            )
        return False

    def record_error(self, now: datetime) -> bool:
        """発注などのエラーを数え、連続回数が上限に達したら緊急停止して真を返す。"""
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.config.max_consecutive_errors:
            return self.halt(f"エラーが{self._consecutive_errors}回続きました", now)
        return False

    def record_success(self) -> None:
        self._consecutive_errors = 0

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    def loss_usage(self, portfolio: Portfolio) -> float:
        """日次損失上限のうち何割を使ったか(0〜1以上)。利益が出ていれば0。"""
        pnl = portfolio.net_pnl()
        if pnl >= 0:
            return 0.0
        return float(-pnl / self.config.max_daily_loss)
