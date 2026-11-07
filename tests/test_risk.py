from __future__ import annotations

from datetime import timedelta

import pytest

from daytrade_bot.config import RiskConfig
from daytrade_bot.core.models import Fill, OrderRequest, OrderType, PositionEffect, Side
from daytrade_bot.core.portfolio import Portfolio
from daytrade_bot.core.risk import RiskDecision, RiskManager, RiskState
from daytrade_bot.core.session import MarketCalendar, TradingSchedule
from tests.conftest import D, at


def req(
    side: Side = Side.BUY,
    qty: int = 100,
    effect: PositionEffect = PositionEffect.OPEN,
    symbol: str = "7203",
) -> OrderRequest:
    return OrderRequest(
        client_order_id="c",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        effect=effect,
        created_at=at(10),
    )


def hold(p: Portfolio, symbol: str, qty: int, price: int) -> None:
    p.apply_fill(
        Fill(
            fill_id=f"{symbol}{qty}{price}",
            client_order_id="x",
            symbol=symbol,
            side=Side.BUY if qty > 0 else Side.SELL,
            qty=abs(qty),
            price=D(price),
            fee=D(0),
            time=at(9, 30),
        )
    )


@pytest.fixture
def rm() -> RiskManager:
    cfg = RiskConfig(
        max_daily_loss=D(10_000),
        max_position_value_per_symbol=D(500_000),
        max_total_position_value=D(800_000),
        max_open_positions=2,
        max_order_qty=1000,
        max_orders_per_minute=3,
        max_consecutive_errors=2,
    )
    return RiskManager(cfg, TradingSchedule(MarketCalendar()))


def test_approves_normal_entry(rm: RiskManager) -> None:
    assert rm.check(req(), D(1000), Portfolio(), at(10)) == RiskDecision.ok()


@pytest.mark.parametrize(
    ("kwargs", "price", "when", "reason"),
    [
        ({"qty": 2000}, 10, at(10), "数量上限"),
        ({}, 1000, at(12), "ザラ場ではありません"),
        ({}, 1000, at(15, 15), "新規エントリーの時間帯"),
        ({"qty": 600}, 1000, at(10), "銘柄ごとの建玉上限"),
    ],
)
def test_denies(rm: RiskManager, kwargs: dict[str, int], price: int, when, reason: str) -> None:
    decision = rm.check(req(**kwargs), D(price), Portfolio(), when)  # type: ignore[arg-type]
    assert not decision.approved
    assert reason in decision.reason


def test_pending_orders_count_toward_symbol_limit(rm: RiskManager) -> None:
    d = rm.check(req(qty=300), D(1000), Portfolio(), at(10), pending_open={"7203": 300})
    assert not d.approved and "銘柄ごと" in d.reason


def test_pending_orders_in_other_symbols_count_toward_totals(rm: RiskManager) -> None:
    p = Portfolio()
    p.mark("6758", D(1000))
    d = rm.check(req(qty=400), D(1000), p, at(10), pending_open={"6758": 450})
    assert not d.approved and "建玉合計" in d.reason
    d = rm.check(req(qty=100), D(1000), p, at(10), pending_open={"6758": 100, "9984": -100})
    assert not d.approved and "銘柄数の上限" in d.reason


def test_total_limit_and_open_positions(rm: RiskManager) -> None:
    p = Portfolio()
    hold(p, "6758", 400, 1000)
    d = rm.check(req(qty=450), D(1000), p, at(10))
    assert not d.approved and "建玉合計" in d.reason
    hold(p, "9984", 100, 1000)
    d = rm.check(req(qty=100), D(1000), p, at(10))
    assert not d.approved and "銘柄数の上限" in d.reason


def test_opposite_position_must_be_closed_first(rm: RiskManager) -> None:
    p = Portfolio()
    hold(p, "7203", -100, 1000)
    d = rm.check(req(Side.BUY), D(1000), p, at(10))
    assert not d.approved and "反対方向" in d.reason


def test_rate_limit(rm: RiskManager) -> None:
    for s in range(3):
        rm.record_order(at(10, 0, s))
    assert "発注上限" in rm.check(req(), D(1000), Portfolio(), at(10, 0, 30)).reason
    assert rm.check(req(), D(1000), Portfolio(), at(10, 1, 1)).approved


def test_close_orders_pass_when_paused_or_halted(rm: RiskManager) -> None:
    p = Portfolio()
    hold(p, "7203", 100, 1000)
    rm.halt("test", at(10))
    close = req(Side.SELL, 100, PositionEffect.CLOSE)
    assert rm.check(close, D(1000), p, at(10)).approved
    assert not rm.check(req(), D(1000), p, at(10)).approved
    # 時間外でも閉じる注文は通すが、ザラ場でなければ通さない
    assert rm.check(close, D(1000), p, at(15, 22)).approved
    assert not rm.check(close, D(1000), p, at(11, 40)).approved


def test_close_validation(rm: RiskManager) -> None:
    p = Portfolio()
    assert (
        "返済する建玉"
        in rm.check(req(Side.SELL, 100, PositionEffect.CLOSE), D(1), p, at(10)).reason
    )
    hold(p, "7203", 100, 1000)
    assert (
        "返済する建玉" in rm.check(req(Side.BUY, 100, PositionEffect.CLOSE), D(1), p, at(10)).reason
    )
    assert (
        "超えています"
        in rm.check(req(Side.SELL, 200, PositionEffect.CLOSE), D(1), p, at(10)).reason
    )


def test_daily_loss_halts_once(rm: RiskManager) -> None:
    p = Portfolio()
    hold(p, "7203", 100, 1000)
    p.mark("7203", D(950))
    assert not rm.check_daily_loss(p, at(10))
    assert rm.loss_usage(p) == pytest.approx(0.5)
    p.mark("7203", D(900))
    assert rm.check_daily_loss(p, at(10))
    assert rm.state is RiskState.HALTED and "日次損失上限" in rm.reason
    assert not rm.check_daily_loss(p, at(10))
    assert "緊急停止" in rm.check(req(symbol="6758"), D(1), p, at(10)).reason
    rm2 = RiskManager(rm.config, rm.schedule)
    assert "日次損失上限に達しています" in rm2.check(req(symbol="6758"), D(100), p, at(10)).reason


def test_loss_usage_zero_when_profitable(rm: RiskManager) -> None:
    p = Portfolio()
    hold(p, "7203", 100, 1000)
    p.mark("7203", D(1100))
    assert rm.loss_usage(p) == 0


def test_state_transitions(rm: RiskManager) -> None:
    assert rm.pause() and rm.state is RiskState.PAUSED
    assert rm.pause("別の理由") and rm.reason == "別の理由"  # 止まっていれば理由を更新する
    assert rm.resume() and rm.state is RiskState.ACTIVE
    assert not rm.resume()
    rm.resume()
    assert rm.halt("a", at(10)) and not rm.halt("b", at(10))
    assert rm.reason == "a" and rm.halted_at == at(10)
    assert not rm.pause() and not rm.resume()
    assert rm.clear_halt() and rm.state is RiskState.PAUSED
    assert not rm.clear_halt()
    rm.halt("x", at(10))
    rm.new_day()
    assert rm.state is RiskState.ACTIVE and rm.reason == ""
    assert RiskState.HALTED.label == "緊急停止"


def test_consecutive_errors(rm: RiskManager) -> None:
    assert not rm.record_error(at(10))
    rm.record_success()
    assert rm.consecutive_errors == 0
    assert not rm.record_error(at(10))
    assert rm.record_error(at(10) + timedelta(seconds=1))
    assert rm.state is RiskState.HALTED and "2回" in rm.reason
