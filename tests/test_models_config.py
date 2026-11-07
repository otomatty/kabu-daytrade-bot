from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

import pytest
from pydantic import ValidationError

from daytrade_bot.config import (
    AccountMode,
    ConfigError,
    KabuConfig,
    load_config,
    load_dotenv,
    load_secrets,
)
from daytrade_bot.core.fees import FixedFee, PercentFee, ZeroFee
from daytrade_bot.core.models import (
    Bar,
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
from daytrade_bot.core.ticksize import TickTable
from tests.conftest import D, at, make_config


class TestModels:
    def test_side_helpers(self) -> None:
        assert Side.BUY.sign == 1 and Side.SELL.sign == -1
        assert Side.BUY.opposite is Side.SELL and Side.SELL.opposite is Side.BUY
        assert Side.BUY.label == "買"

    def test_status_properties(self) -> None:
        assert OrderStatus.FILLED.is_terminal and not OrderStatus.SUBMITTED.is_terminal
        assert OrderStatus.UNKNOWN.is_active
        assert {s.label for s in OrderStatus}  # すべてに表示名がある

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="タイムゾーン"):
            Tick(symbol="7203", time=datetime(2026, 11, 9, 9), price=D(1))  # noqa: DTZ001

    def test_tick_and_bar_validation(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            Tick(symbol="7203", time=at(9), price=D(0))
        with pytest.raises(ValueError, match="OHLC"):
            Bar("7203", at(9), at(9, 1), D(10), D(9), D(8), D(10), 1)
        with pytest.raises(ValueError, match="end"):
            Bar("7203", at(9), at(9), D(10), D(10), D(10), D(10), 1)
        with pytest.raises(ValueError, match="出来高"):
            Bar("7203", at(9), at(9, 1), D(10), D(10), D(10), D(10), -1)

    def test_order_request_validation(self) -> None:
        kw = {
            "client_order_id": "c",
            "symbol": "7203",
            "side": Side.BUY,
            "effect": PositionEffect.OPEN,
            "created_at": at(9),
        }
        with pytest.raises(ValueError, match="数量"):
            OrderRequest(qty=0, order_type=OrderType.MARKET, **kw)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="指値"):
            OrderRequest(qty=100, order_type=OrderType.LIMIT, **kw)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="成行"):
            OrderRequest(qty=100, order_type=OrderType.MARKET, limit_price=D(1), **kw)  # type: ignore[arg-type]
        req = OrderRequest(qty=100, order_type=OrderType.LIMIT, limit_price=D(10), **kw)  # type: ignore[arg-type]
        assert req.signed_qty == 100
        order = Order(req, OrderStatus.SUBMITTED, at(9), filled_qty=30)
        assert order.remaining_qty == 70 and order.symbol == "7203"
        assert order.copy() is not order

    def test_fill_and_signal_validation(self) -> None:
        kw = {
            "fill_id": "f",
            "client_order_id": "c",
            "symbol": "7203",
            "side": Side.BUY,
            "time": at(9),
        }
        with pytest.raises(ValueError, match="数量"):
            Fill(qty=0, price=D(1), fee=D(0), **kw)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="価格"):
            Fill(qty=1, price=D(0), fee=D(0), **kw)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="手数料"):
            Fill(qty=1, price=D(1), fee=D(-1), **kw)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="qty"):
            Signal("7203", SignalAction.EXIT, qty=0)
        with pytest.raises(ValueError, match="limit_price"):
            Signal("7203", SignalAction.ENTER_LONG, order_type=OrderType.LIMIT)


class TestConfig:
    def test_defaults_are_safe(self) -> None:
        cfg = make_config()
        assert cfg.trading.live_enabled is False
        assert cfg.symbols == ("7203", "6758")
        assert cfg.tick_table("7203") is TickTable.STANDARD
        assert cfg.tick_table("9999") is TickTable.STANDARD
        assert cfg.session.flatten_at == time(15, 20)

    def test_fee_models(self) -> None:
        assert isinstance(make_config().fees.model(), ZeroFee)
        assert isinstance(
            make_config(fees={"kind": "percent", "rate": "0.001"}).fees.model(), PercentFee
        )
        assert isinstance(make_config(fees={"kind": "fixed", "amount": 100}).fees.model(), FixedFee)

    def test_kabu_ports(self) -> None:
        assert KabuConfig().resolved_port == 18080
        assert KabuConfig(environment="verification").resolved_port == 18081
        k = KabuConfig(port=19000)
        assert k.base_url == "http://localhost:19000/kabusapi"
        assert k.ws_url == "ws://localhost:19000/kabusapi/websocket"

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"universe": [{"symbol": "7203"}, {"symbol": "7203"}]}, "重複"),
            ({"universe": [{"symbol": f"{1000 + i}"} for i in range(51)]}, "最大50"),
            ({"universe": [{"symbol": "abc"}]}, "pattern"),
            ({"session": {"flatten_at": "15:26"}}, "15:25"),
            ({"trading": {"account_mode": "cash"}}, "空売り"),
            ({"sizing": {"qty": 150}}, "lot_size"),
            ({"risk": {"max_position_value_per_symbol": 5_000_000}}, "合計の上限"),
            ({"unknown_key": 1}, "Extra inputs"),
            ({"strategy": {"name": "orb", "params": {"range_minutes": 0}}}, "range_minutes"),
            ({"strategy": {"name": "orb", "params": {"typo": 1}}}, "typo"),
            ({"strategy": {"name": "missing"}}, "見つかりません"),
        ],
    )
    def test_invalid(self, overrides: dict[str, object], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            make_config(**overrides)

    def test_cash_mode_without_short_is_ok(self) -> None:
        cfg = make_config(
            trading={"account_mode": "cash"},
            strategy={"name": "orb", "params": {"allow_short": False}},
        )
        assert cfg.trading.account_mode is AccountMode.CASH

    def test_load_config_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            "universe:\n  - symbol: '7203'\n    tick_table: topix500\n", encoding="utf-8"
        )
        cfg = load_config(path)
        assert cfg.tick_table("7203") is TickTable.TOPIX500
        with pytest.raises(ConfigError, match="見つかりません"):
            load_config(tmp_path / "missing.yaml")
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="マッピング"):
            load_config(path)
        path.write_text("universe: [\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="書式"):
            load_config(path)

    def test_example_config_is_valid(self) -> None:
        root = Path(__file__).resolve().parent.parent
        cfg = load_config(root / "src" / "daytrade_bot" / "resources" / "settings.example.yaml")
        assert cfg.trading.live_enabled is False

    def test_dotenv_and_secrets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# comment\n\nKABU_API_PASSWORD='p@ss word'\nexport DTBOT_WEBHOOK_URL=\"https://example.invalid/x\"\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("KABU_API_PASSWORD", raising=False)
        monkeypatch.delenv("DTBOT_WEBHOOK_URL", raising=False)
        values = load_dotenv(env)
        assert values["KABU_API_PASSWORD"] == "p@ss word"
        secrets = load_secrets()
        assert secrets.api_password is not None
        assert secrets.api_password.get_secret_value() == "p@ss word"
        assert "p@ss" not in repr(secrets)  # 秘密情報は表示されない
        assert load_secrets({}).api_password is None
        assert load_dotenv(tmp_path / "none.env") == {}
        env.write_text("NOEQUALS\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="KEY=VALUE"):
            load_dotenv(env)
