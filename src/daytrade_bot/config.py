"""設定ファイル(YAML)と秘密情報(環境変数・.env)の読み込み。

秘密情報(APIパスワード、通知用Webhook URL)は設定ファイルに書かず、環境変数か .env に置く。
"""

from __future__ import annotations

import enum
import os
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from daytrade_bot.core.fees import FeeModel, FixedFee, PercentFee, ZeroFee
from daytrade_bot.core.session import (
    MarketCalendar,
    SessionTimes,
    TradingRules,
    TradingSchedule,
)
from daytrade_bot.core.ticksize import TickTable

ENV_API_PASSWORD = "KABU_API_PASSWORD"  # noqa: S105 - 環境変数の名前
ENV_WEBHOOK_URL = "DTBOT_WEBHOOK_URL"
ENV_DASHBOARD_TOKEN = "DTBOT_DASHBOARD_TOKEN"  # noqa: S105 - 環境変数の名前

PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolConfig(_Model):
    symbol: str = Field(pattern=r"^[0-9A-Z]{4,5}$", description="銘柄コード(例: 7203)")
    name: str = ""
    tick_table: TickTable = TickTable.STANDARD


class StrategyConfig(_Model):
    name: str = "orb"
    params: dict[str, Any] = Field(default_factory=dict)
    bar_minutes: int = Field(default=1, ge=1, le=60)


class SizingConfig(_Model):
    method: Literal["fixed_qty", "fixed_notional"] = "fixed_notional"
    qty: int = Field(default=100, gt=0)
    notional: PositiveDecimal = Decimal(300_000)
    lot_size: int = Field(default=100, gt=0)

    @model_validator(mode="after")
    def _check_lot(self) -> SizingConfig:
        if self.qty % self.lot_size != 0:
            raise ValueError("sizing.qty は lot_size の倍数にしてください")
        return self


class AccountMode(enum.StrEnum):
    MARGIN = "margin"
    CASH = "cash"


class MarginTradeType(enum.StrEnum):
    SYSTEM = "system"  # 制度信用
    LONG_TERM = "long_term"  # 一般信用(長期)
    DAY_TRADE = "day_trade"  # 一般信用(デイトレ)


class TradingConfig(_Model):
    account_mode: AccountMode = AccountMode.MARGIN
    margin_trade_type: MarginTradeType = MarginTradeType.SYSTEM
    account_type: Literal["specific", "general", "corporate"] = "specific"
    order_exchange: Literal["tse_plus", "sor"] = "tse_plus"
    cash_fund_type: Literal["AA", "02"] = "AA"
    limit_order_timeout_sec: float = Field(default=30.0, gt=0)
    live_enabled: bool = Field(
        default=False, description="本番の発注を許可するか。true にしない限り live は起動しない"
    )


class RiskConfig(_Model):
    max_daily_loss: PositiveDecimal = Decimal(30_000)
    max_position_value_per_symbol: PositiveDecimal = Decimal(1_000_000)
    max_total_position_value: PositiveDecimal = Decimal(2_000_000)
    max_open_positions: int = Field(default=3, ge=1)
    max_order_qty: int = Field(default=10_000, ge=1)
    max_orders_per_minute: int = Field(default=10, ge=1)
    max_consecutive_errors: int = Field(default=3, ge=1)
    stale_data_sec: float = Field(default=30.0, gt=0)
    reconcile_interval_sec: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        if self.max_position_value_per_symbol > self.max_total_position_value:
            raise ValueError("銘柄ごとの上限は合計の上限以下にしてください")
        return self


class SessionConfig(_Model):
    entry_start: time = time(9, 0)
    entry_cutoff: time = time(15, 10)
    flatten_at: time = time(15, 20)
    trade_morning: bool = True
    trade_afternoon: bool = True
    extra_holidays: tuple[date, ...] = ()

    def schedule(self) -> TradingSchedule:
        return TradingSchedule(
            MarketCalendar(self.extra_holidays),
            SessionTimes(),
            TradingRules(
                entry_start=self.entry_start,
                entry_cutoff=self.entry_cutoff,
                flatten_at=self.flatten_at,
                trade_morning=self.trade_morning,
                trade_afternoon=self.trade_afternoon,
            ),
        )


class FeeConfig(_Model):
    kind: Literal["zero", "percent", "fixed"] = "zero"
    rate: Decimal = Field(default=Decimal(0), ge=0, description="percent のときの料率(0.001=0.1%)")
    minimum: Decimal = Field(default=Decimal(0), ge=0)
    maximum: Decimal | None = None
    amount: Decimal = Field(default=Decimal(0), ge=0, description="fixed のときの1約定あたり")

    def model(self) -> FeeModel:
        if self.kind == "percent":
            return PercentFee(self.rate, self.minimum, self.maximum)
        if self.kind == "fixed":
            return FixedFee(self.amount)
        return ZeroFee()


class SimulationConfig(_Model):
    slippage_ticks: int = Field(default=1, ge=0, description="成行の不利な約定(呼値の数)")
    limit_fill_on_touch: bool = Field(
        default=False, description="指値と同値で約定とみなすか(false=指値を突き抜けたら約定)"
    )
    initial_capital: PositiveDecimal = Decimal(3_000_000)


class KabuConfig(_Model):
    environment: Literal["production", "verification"] = "production"
    host: str = "localhost"
    port: int | None = Field(default=None, ge=1, le=65535)
    order_rate_per_sec: float = Field(default=5.0, gt=0, le=10)
    info_rate_per_sec: float = Field(default=5.0, gt=0, le=10)
    poll_interval_sec: float = Field(default=1.0, ge=0.2)
    request_timeout_sec: float = Field(default=10.0, gt=0)
    data_exchange: int = Field(default=1, description="時価の市場コード(1=東証)")

    @property
    def resolved_port(self) -> int:
        if self.port is not None:
            return self.port
        return 18080 if self.environment == "production" else 18081

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.resolved_port}/kabusapi"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.resolved_port}/kabusapi/websocket"


class DashboardConfig(_Model):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")


class NotifyConfig(_Model):
    kind: Literal["none", "slack", "discord"] = "none"
    min_level: Literal["info", "warning", "error"] = "warning"


class StorageConfig(_Model):
    path: Path = Path("data/dtbot.sqlite3")
    record_ticks: bool = True


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    directory: Path = Path("logs")


class AppConfig(_Model):
    universe: tuple[SymbolConfig, ...] = Field(min_length=1)
    strategy: StrategyConfig = StrategyConfig()
    sizing: SizingConfig = SizingConfig()
    trading: TradingConfig = TradingConfig()
    risk: RiskConfig = RiskConfig()
    session: SessionConfig = SessionConfig()
    fees: FeeConfig = FeeConfig()
    simulation: SimulationConfig = SimulationConfig()
    kabu: KabuConfig = KabuConfig()
    dashboard: DashboardConfig = DashboardConfig()
    notify: NotifyConfig = NotifyConfig()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()

    @field_validator("universe")
    @classmethod
    def _unique_symbols(cls, value: tuple[SymbolConfig, ...]) -> tuple[SymbolConfig, ...]:
        symbols = [s.symbol for s in value]
        if len(symbols) != len(set(symbols)):
            raise ValueError("universe に同じ銘柄が重複しています")
        if len(symbols) > 50:
            raise ValueError("kabuステーションAPIに登録できる銘柄は最大50です")
        return value

    @model_validator(mode="after")
    def _check_cross(self) -> AppConfig:
        # 戦略のパラメータも起動前に検証する(起動してから誤りに気づかないように)
        from daytrade_bot.core.strategy import load_strategy_class  # noqa: PLC0415

        strategy_cls = load_strategy_class(self.strategy.name)
        params = strategy_cls.Params.model_validate(self.strategy.params)
        allow_short = getattr(params, "allow_short", True)
        if self.trading.account_mode is AccountMode.CASH and allow_short:
            raise ValueError(
                "現物取引では空売りできません。strategy.params.allow_short を false にするか、"
                "trading.account_mode を margin にしてください"
            )
        # 取引時間の整合性はスケジュールを作るときに検証される
        self.session.schedule()
        return self

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.symbol for s in self.universe)

    def tick_table(self, symbol: str) -> TickTable:
        for s in self.universe:
            if s.symbol == symbol:
                return s.tick_table
        return TickTable.STANDARD


class Secrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_password: SecretStr | None = None
    webhook_url: SecretStr | None = None
    dashboard_token: SecretStr | None = None


def load_config(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"設定ファイルが見つかりません: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"設定ファイルの書式が正しくありません: {path}\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"設定ファイルの最上位はマッピングである必要があります: {path}")
    return AppConfig.model_validate(raw)


def load_dotenv(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` 形式の .env を読む(既存の環境変数は上書きしない)。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, sep, value = stripped.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"{path}:{lineno} は KEY=VALUE の形式ではありません")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    source = os.environ if env is None else env

    def get(name: str) -> SecretStr | None:
        value = source.get(name, "").strip()
        return SecretStr(value) if value else None

    return Secrets(
        api_password=get(ENV_API_PASSWORD),
        webhook_url=get(ENV_WEBHOOK_URL),
        dashboard_token=get(ENV_DASHBOARD_TOKEN),
    )


class ConfigError(Exception):
    """設定の誤り。"""
