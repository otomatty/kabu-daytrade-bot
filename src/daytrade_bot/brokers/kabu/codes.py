"""kabuステーションAPI のコード値(公式リファレンス kabu_STATION_API.yaml v1.5 による)。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from daytrade_bot.config import AccountMode, MarginTradeType, TradingConfig
from daytrade_bot.core.models import OrderStatus, OrderType, Side

SIDE = {Side.SELL: "1", Side.BUY: "2"}
SIDE_FROM_CODE = {"1": Side.SELL, "2": Side.BUY}

CASH_MARGIN_CASH = 1
CASH_MARGIN_OPEN = 2
CASH_MARGIN_CLOSE = 3

MARGIN_TRADE_TYPE = {
    MarginTradeType.SYSTEM: 1,
    MarginTradeType.LONG_TERM: 2,
    MarginTradeType.DAY_TRADE: 3,
}
ACCOUNT_TYPE = {"general": 2, "specific": 4, "corporate": 12}
# 新規注文は SOR(9) か 東証+(27)。通常時は東証(1)を指定した新規注文はできない
ORDER_EXCHANGE = {"sor": 9, "tse_plus": 27}
FRONT_ORDER_TYPE = {OrderType.MARKET: 10, OrderType.LIMIT: 20}

DELIV_NONE = 0
DELIV_DEPOSIT = 2  # お預り金
FUND_TYPE_CASH_SELL = "  "  # 現物売は半角スペース2つ
FUND_TYPE_MARGIN = "11"

PRODUCT_ALL = 0
PRODUCT_CASH = 1
PRODUCT_MARGIN = 2

# 注文の状態(State / OrderState)
STATE_WAITING = 1
STATE_SENDING = 2
STATE_DONE = 3
STATE_CANCEL_SENDING = 4
STATE_ENDED = 5

# 注文明細の種別(Details.RecType)
REC_ACCEPTED = 1
REC_EXPIRED = 3
REC_CANCELED = 6
REC_LAPSED = 7
REC_EXECUTION = 8
DETAIL_STATE_ERROR = 4

# 再発行で解決しうる認証エラー
AUTH_RETRY_CODES = frozenset({4001009})  # APIキー不一致(トークンが無効になった)
NOT_LOGGED_IN_CODES = frozenset({4001007, 4001017})
BAD_PASSWORD_CODE = 4001013


def order_status(order: dict[str, Any]) -> tuple[OrderStatus, str]:
    """注文照会の1件から、ボットの注文状態と理由を求める。"""
    state = int(order.get("State") or order.get("OrderState") or 0)
    cum = Decimal(str(order.get("CumQty") or 0))
    qty = Decimal(str(order.get("OrderQty") or 0))
    details: list[dict[str, Any]] = list(order.get("Details") or [])
    rec_types = {int(d.get("RecType") or 0) for d in details}
    if state == STATE_ENDED:
        if qty > 0 and cum >= qty:
            return OrderStatus.FILLED, ""
        if cum == 0 and any(int(d.get("State") or 0) == DETAIL_STATE_ERROR for d in details):
            return OrderStatus.REJECTED, "発注エラー(kabuステーションのログを確認してください)"
        if rec_types & {REC_EXPIRED, REC_LAPSED}:
            return OrderStatus.EXPIRED, "期限切れ・失効"
        return OrderStatus.CANCELED, "取消済" if REC_CANCELED in rec_types else "終了"
    if state == STATE_CANCEL_SENDING:
        return OrderStatus.PENDING_CANCEL, ""
    if cum > 0:
        return OrderStatus.PARTIALLY_FILLED, ""
    return OrderStatus.SUBMITTED, ""


def base_order_payload(trading: TradingConfig) -> dict[str, Any]:
    return {
        "SecurityType": 1,
        "AccountType": ACCOUNT_TYPE[trading.account_type],
        "ExpireDay": 0,
    }


def uses_margin(trading: TradingConfig) -> bool:
    return trading.account_mode is AccountMode.MARGIN
