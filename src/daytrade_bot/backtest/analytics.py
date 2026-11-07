"""バックテスト結果の指標。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from daytrade_bot.core.portfolio import RoundTrip

TRADING_DAYS_PER_YEAR = 245


@dataclass(frozen=True, slots=True)
class Summary:
    net_pnl: float
    gross_profit: float
    gross_loss: float
    total_fees: float
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    profit_factor: float | None
    avg_trade: float | None
    avg_win: float | None
    avg_loss: float | None
    payoff_ratio: float | None
    max_drawdown: float
    max_drawdown_pct: float | None
    sharpe: float | None
    trading_days: int
    positive_days: int
    best_day: float | None
    worst_day: float | None
    avg_holding_minutes: float | None
    long_trades: int
    short_trades: int
    long_pnl: float
    short_pnl: float
    initial_capital: float
    return_pct: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def max_drawdown(equity: Sequence[float], base: float = 0.0) -> tuple[float, float | None]:
    """累積損益の列から最大ドローダウン(金額、資金に対する割合)を求める。

    ``base`` は初期資金。割合は (初期資金 + その時点までの最高値) に対する比率。
    """
    peak = 0.0
    worst = 0.0
    worst_pct: float | None = None
    for value in equity:
        peak = max(peak, value)
        dd = value - peak
        if dd < worst:
            worst = dd
            denom = base + peak
            worst_pct = (dd / denom) if denom > 0 else None
    return worst, worst_pct


def sharpe_ratio(daily_pnl: Sequence[float], capital: float) -> float | None:
    """日次リターン(日次損益 / 初期資金)の年率換算シャープレシオ(無リスク金利0)。"""
    if len(daily_pnl) < 2 or capital <= 0:
        return None
    returns = [p / capital for p in daily_pnl]
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return None
    return statistics.fmean(returns) / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)


def summarize(
    trips: Sequence[RoundTrip],
    daily: Sequence[tuple[date, Decimal]],
    equity: Sequence[tuple[datetime, Decimal]],
    initial_capital: Decimal,
    total_fees: Decimal,
) -> Summary:
    pnls = [float(t.pnl) for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    daily_values = [float(v) for _, v in daily]
    net = sum(daily_values)
    capital = float(initial_capital)
    dd, dd_pct = max_drawdown([float(v) for _, v in equity], capital)
    holding = [(t.closed_at - t.opened_at).total_seconds() / 60 for t in trips]
    longs = [t for t in trips if t.side.sign > 0]
    shorts = [t for t in trips if t.side.sign < 0]
    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = statistics.fmean(losses) if losses else None
    return Summary(
        net_pnl=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_fees=float(total_fees),
        trades=len(trips),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trips) if trips else None,
        profit_factor=(gross_profit / -gross_loss) if gross_loss < 0 else None,
        avg_trade=statistics.fmean(pnls) if pnls else None,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=(avg_win / -avg_loss) if avg_win is not None and avg_loss else None,
        max_drawdown=dd,
        max_drawdown_pct=dd_pct,
        sharpe=sharpe_ratio(daily_values, capital),
        trading_days=len(daily_values),
        positive_days=sum(1 for v in daily_values if v > 0),
        best_day=max(daily_values) if daily_values else None,
        worst_day=min(daily_values) if daily_values else None,
        avg_holding_minutes=statistics.fmean(holding) if holding else None,
        long_trades=len(longs),
        short_trades=len(shorts),
        long_pnl=sum(float(t.pnl) for t in longs),
        short_pnl=sum(float(t.pnl) for t in shorts),
        initial_capital=capital,
        return_pct=(net / capital) if capital > 0 else 0.0,
    )
