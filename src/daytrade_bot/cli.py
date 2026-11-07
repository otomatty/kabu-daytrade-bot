"""コマンドライン(``dtbot``)。

dtbot init                 設定ファイルと .env のひな形を作る
dtbot demo                 合成データで動かす(口座不要。ダッシュボードの確認用)
dtbot check                kabuステーションAPI に接続できるか確かめる
dtbot collect              時価を受信して保存する(バックテスト用のデータを貯める)
dtbot import-bars FILE     1分足の CSV を取り込む
dtbot backtest             保存したデータでバックテストする
dtbot paper                本物の時価で、約定だけシミュレーションする
dtbot live --confirm-live  本番の発注をする
dtbot dashboard            バックテストの結果などを見る(売買はしない)
dtbot mock-kabu            kabuステーションAPI のモックを起動する(リハーサル用)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import logging
import secrets
import signal
import socket
import sys
import webbrowser
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from pydantic import ValidationError

from daytrade_bot import __version__
from daytrade_bot.backtest.runner import BacktestRunner, NoDataError, build_strategy
from daytrade_bot.brokers.base import Broker, BrokerError
from daytrade_bot.brokers.kabu.broker import KabuBroker
from daytrade_bot.brokers.kabu.client import KabuRestClient
from daytrade_bot.brokers.kabu.feed import KabuPushFeed
from daytrade_bot.brokers.sim import SimulatedBroker
from daytrade_bot.config import (
    AccountMode,
    AppConfig,
    ConfigError,
    Secrets,
    load_config,
    load_dotenv,
    load_secrets,
)
from daytrade_bot.core.clock import JST, AcceleratedClock, Clock, SystemClock
from daytrade_bot.core.engine import Engine, Mode
from daytrade_bot.core.models import Bar
from daytrade_bot.dashboard.app import create_app
from daytrade_bot.feeds.demo import DemoFeed
from daytrade_bot.logging_setup import setup_logging
from daytrade_bot.notify import Notifier, NullNotifier, WebhookNotifier
from daytrade_bot.storage.db import Repository

log = logging.getLogger("daytrade_bot")

DEFAULT_CONFIG = Path("config/settings.yaml")
DEFAULT_ENV = Path(".env")


class CliError(Exception):
    """利用者に伝えて終了するエラー。"""


# ---- 共通 --------------------------------------------------------------------------------


def _resource_text(name: str) -> str:
    return (resources.files("daytrade_bot.resources") / name).read_text(encoding="utf-8")


def _load(args: argparse.Namespace, *, allow_example: bool = False) -> tuple[AppConfig, Secrets]:
    load_dotenv(Path(args.env))
    path = Path(args.config)
    try:
        if not path.exists() and allow_example:
            log.info("設定ファイル %s が無いため、例の設定で動かします", path)
            config = AppConfig.model_validate(
                yaml.safe_load(_resource_text("settings.example.yaml"))
            )
        else:
            config = load_config(path)
    except ConfigError as exc:
        raise CliError(f"{exc}\n`dtbot init` で設定ファイルのひな形を作れます") from exc
    except ValidationError as exc:
        raise CliError(f"設定ファイルの内容に誤りがあります({path}):\n{exc}") from exc
    return config, load_secrets()


def _setup_logs(config: AppConfig, name: str) -> None:
    path = setup_logging(config.logging.level, config.logging.directory, name)
    if path is not None:
        log.info("ログ: %s", path)


def _notifier(config: AppConfig, secrets_: Secrets, mode: str) -> Notifier:
    if config.notify.kind == "none":
        return NullNotifier()
    if secrets_.webhook_url is None:
        log.warning("通知が有効ですが、DTBOT_WEBHOOK_URL が設定されていません")
        return NullNotifier()
    return WebhookNotifier(
        secrets_.webhook_url.get_secret_value(),
        config.notify.kind,
        min_level=config.notify.min_level,
        prefix=f"dtbot {mode}",
    )


def _kabu_client(config: AppConfig, secrets_: Secrets) -> KabuRestClient:
    if secrets_.api_password is None:
        raise CliError(
            "kabuステーションの APIパスワードが設定されていません。"
            ".env に KABU_API_PASSWORD=... を書いてください"
        )
    return KabuRestClient(config.kabu, secrets_.api_password.get_secret_value())


def _check_port_free(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        try:
            s.bind((host, port))
        except OSError as exc:
            raise CliError(
                f"ダッシュボードのポート {host}:{port} が使われています。"
                "設定の dashboard.port を変えるか、使っているプログラムを終了してください"
            ) from exc


def _run_id(mode: str) -> str:
    return f"{mode}-{datetime.now(JST):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


async def _serve(
    engine: Engine | None,
    config: AppConfig,
    repo: Repository,
    secrets_: Secrets,
    *,
    open_browser: bool,
) -> None:
    """エンジンとダッシュボードを一緒に動かす。どちらかが止まったら、もう一方も止める。"""
    dash = config.dashboard
    tasks: list[asyncio.Task[Any]] = []
    server: uvicorn.Server | None = None
    if dash.enabled:
        token = (
            secrets_.dashboard_token.get_secret_value()
            if secrets_.dashboard_token
            else secrets.token_urlsafe(24)
        )
        app = create_app(repo=repo, token=token, engine=engine, allowed_hosts=dash.allowed_hosts)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=dash.host,
                port=dash.port,
                log_level="warning",
                ws="websockets-sansio",
                lifespan="off",
            )
        )
        host = f"[{dash.host}]" if ":" in dash.host else dash.host
        url = f"http://{host}:{dash.port}/#token={token}"
        print(f"\nダッシュボード: {url}\n(このURLは他の人に見せないでください)\n", flush=True)
        tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
        if open_browser:
            webbrowser.open(url)
    if engine is not None:
        tasks.append(asyncio.create_task(engine.run(), name="engine"))
    if not tasks:
        return
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        if engine is not None:
            engine.stop()
        if server is not None:
            server.should_exit = True

    if server is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, request_stop)
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    request_stop()
    for task in pending:
        await task


def _demo_start(config: AppConfig) -> datetime:
    """デモの開始時刻: 今日の寄付き前(15時を過ぎていたり休場日なら、次の営業日の寄付き前)。"""
    calendar = config.session.schedule().calendar
    now = datetime.now(JST)
    day = now.date()
    if not calendar.is_trading_day(day) or now.time() >= time(15, 0):
        day = calendar.next_trading_day(day)
    return datetime.combine(day, time(8, 58), tzinfo=JST)


# ---- コマンド ----------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    env_path = Path(args.env)
    for path, template in ((config_path, "settings.example.yaml"), (env_path, "env.example")):
        if path.exists():
            print(f"そのままにしました(すでにあります): {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_resource_text(template), encoding="utf-8")
        print(f"作成しました: {path}")
    print("設定を確認してから `dtbot demo` で動作を確かめてください。")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    config, secrets_ = _load(args, allow_example=True)
    _setup_logs(config, "demo")
    if config.dashboard.enabled:
        _check_port_free(config.dashboard.host, config.dashboard.port)
    start = _demo_start(config)
    clock = AcceleratedClock(start, args.speed)
    # 合成データの初期値(既定の資金配分で1単元を買える値段にする)
    rng_prices = {s.symbol: Decimal(800 + (int(s.symbol) % 12) * 150) for s in config.universe}
    feed = DemoFeed(
        clock,
        config.session.schedule(),
        rng_prices,
        {s.symbol: s.tick_table for s in config.universe},
        seed=args.seed,
    )
    broker = _sim_broker(config, clock, "demo")
    repo = Repository(config.storage.path.with_name("demo.sqlite3"))
    engine = Engine(
        config=config,
        mode="demo",
        clock=clock,
        broker=broker,
        feed=feed,
        strategy=build_strategy(config),
        repo=repo,
        notifier=NullNotifier(),
        run_id=_run_id("demo"),
    )
    log.info("デモを %s から %.0f 倍速で動かします(値段は合成データです)", start, args.speed)
    try:
        asyncio.run(_serve(engine, config, repo, secrets_, open_browser=not args.no_browser))
    finally:
        repo.close()
    return 0


def _sim_broker(config: AppConfig, clock: Clock, name: str) -> SimulatedBroker:
    return SimulatedBroker(
        clock,
        config.fees.model(),
        config.tick_table,
        slippage_ticks=config.simulation.slippage_ticks,
        limit_fill_on_touch=config.simulation.limit_fill_on_touch,
        name=name,
        allow_short=config.trading.account_mode is AccountMode.MARGIN,
    )


def _today_net_qty(repo: Repository) -> Callable[[], dict[str, int]]:
    def compute() -> dict[str, int]:
        net: dict[str, int] = {}
        for fill in repo.load_fills("live", datetime.now(JST).date()):
            net[fill.symbol] = net.get(fill.symbol, 0) + fill.qty * fill.side.sign
        return net

    return compute


def _run_realtime(args: argparse.Namespace, mode: Mode) -> int:
    config, secrets_ = _load(args)
    _setup_logs(config, mode)
    if mode == "live":
        if not config.trading.live_enabled:
            raise CliError(
                "本番の発注は無効です。設定の trading.live_enabled を true にしてください"
            )
        if not args.confirm_live:
            raise CliError("本番の発注をするには --confirm-live を付けて起動してください")
        _print_live_summary(config)
    if config.dashboard.enabled:
        _check_port_free(config.dashboard.host, config.dashboard.port)
    clock = SystemClock()
    client = _kabu_client(config, secrets_)
    repo = Repository(config.storage.path)
    broker: Broker
    if mode == "live":
        broker = KabuBroker(
            client,
            config.trading,
            clock,
            poll_interval_sec=config.kabu.poll_interval_sec,
            today_net_qty=_today_net_qty(repo),
        )
    else:
        broker = _sim_broker(config, clock, "paper")
    feed = KabuPushFeed(config.kabu.ws_url, client, clock, exchange=config.kabu.data_exchange)
    engine = Engine(
        config=config,
        mode=mode,
        clock=clock,
        broker=broker,
        feed=feed,
        strategy=build_strategy(config),
        repo=repo,
        notifier=_notifier(config, secrets_, mode),
        run_id=_run_id(mode),
    )

    async def main() -> None:
        try:
            await _serve(engine, config, repo, secrets_, open_browser=not args.no_browser)
        finally:
            await client.aclose()

    try:
        asyncio.run(main())
    except BrokerError as exc:
        raise CliError(str(exc)) from exc
    finally:
        repo.close()
    return 0


def _print_live_summary(config: AppConfig) -> None:
    r, s, t = config.risk, config.session, config.trading
    lines = [
        "=" * 60,
        "本番の発注を開始します。内容を確認してください。",
        f"  接続先: {config.kabu.base_url}({config.kabu.environment})",
        f"  銘柄: {', '.join(config.symbols)}",
        f"  取引: {t.account_mode.value} / {t.margin_trade_type.value} / 口座 {t.account_type}",
        f"  日次損失上限: {r.max_daily_loss:,}円(達したら全決済して停止)",
        f"  建玉の上限: 銘柄ごと {r.max_position_value_per_symbol:,}円"
        f" / 合計 {r.max_total_position_value:,}円",
        f"  新規締切 {s.entry_cutoff:%H:%M} / 手仕舞い {s.flatten_at:%H:%M}",
        "  止めるときは、ダッシュボードの「全決済する」を押してから Ctrl+C で終了してください。",
        "=" * 60,
    ]
    print("\n".join(lines), flush=True)


def cmd_paper(args: argparse.Namespace) -> int:
    return _run_realtime(args, "paper")


def cmd_live(args: argparse.Namespace) -> int:
    return _run_realtime(args, "live")


def cmd_check(args: argparse.Namespace) -> int:
    config, secrets_ = _load(args)
    setup_logging("WARNING", None, "check")
    client = _kabu_client(config, secrets_)

    async def run() -> bool:
        ok = True

        async def step(label: str, coro: Any) -> Any:
            nonlocal ok
            try:
                result = await coro
            except BrokerError as exc:
                ok = False
                print(f"NG  {label}: {exc}")
                return None
            print(f"OK  {label}")
            return result

        try:
            if await step(f"トークンの発行({config.kabu.base_url})", client.authenticate()) is None:
                return False
            limit = await step("API の上限とバージョン", client.soft_limit())
            if limit:
                print(
                    f"    kabuステーション {limit.get('KabuSVersion')}、"
                    f"現物の1回上限 {limit.get('Stock')}万円"
                )
            if config.trading.account_mode is AccountMode.MARGIN:
                wallet = await step("信用の余力", client.wallet_margin())
                if wallet:
                    amount = Decimal(str(wallet.get("MarginAccountWallet") or 0))
                    print(f"    信用新規可能額 {amount:,.0f}円")
            else:
                wallet = await step("現物の余力", client.wallet_cash())
                if wallet:
                    amount = Decimal(str(wallet.get("StockAccountWallet") or 0))
                    print(f"    現物買付可能額 {amount:,.0f}円")
            first = config.symbols[0]
            board = await step(
                f"時価の取得({first})", client.board(first, config.kabu.data_exchange)
            )
            if board:
                print(f"    {board.get('SymbolName')} 現値 {board.get('CurrentPrice')}")
        finally:
            await client.aclose()
        return ok

    ok = asyncio.run(run())
    print("すべて確認できました。" if ok else "確認できなかった項目があります。")
    return 0 if ok else 1


def cmd_collect(args: argparse.Namespace) -> int:
    config, secrets_ = _load(args)
    _setup_logs(config, "collect")
    client = _kabu_client(config, secrets_)
    clock = SystemClock()
    repo = Repository(config.storage.path)
    feed = KabuPushFeed(config.kabu.ws_url, client, clock, exchange=config.kabu.data_exchange)
    count = 0

    def on_tick(tick: Any) -> None:
        nonlocal count
        repo.buffer_tick(tick)
        count += 1

    feed.set_tick_handler(on_tick)

    async def run() -> None:
        await feed.start(config.symbols)
        log.info("時価の収集を始めました(Ctrl+C で終了): %s", ", ".join(config.symbols))
        try:
            last_report = clock.now()
            while True:
                await asyncio.sleep(1)
                repo.flush_ticks()
                if clock.now() - last_report >= timedelta(minutes=1):
                    last_report = clock.now()
                    log.info(
                        "これまでに %d 件保存しました(接続: %s)",
                        count,
                        "あり" if feed.connected else "なし",
                    )
        finally:
            await feed.stop()
            repo.flush_ticks()
            await client.aclose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        repo.close()
    log.info("収集を終了しました(%d 件)", count)
    return 0


def parse_bars_csv(path: Path, symbol: str, minutes: int = 1) -> tuple[list[Bar], list[str]]:
    """1分足の CSV を読む。列: datetime(または date と time), open, high, low, close, volume。

    時刻はその足の始まりの時刻(日本時間)とする。
    """
    bars: list[Bar] = []
    problems: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
        need = {"open", "high", "low", "close", "volume"}
        if not need <= set(fields) or not (
            {"datetime"} <= set(fields) or {"date", "time"} <= set(fields)
        ):
            raise CliError(
                "CSV の列が足りません。datetime(または date と time), "
                "open, high, low, close, volume が必要です"
            )
        for lineno, row in enumerate(reader, start=2):
            try:
                text = (
                    (
                        row[fields["datetime"]]
                        if "datetime" in fields
                        else f"{row[fields['date']]} {row[fields['time']]}"
                    )
                    .strip()
                    .replace("/", "-")
                )
                start = datetime.fromisoformat(text)
                start = start.replace(tzinfo=JST) if start.tzinfo is None else start.astimezone(JST)
                bars.append(
                    Bar(
                        symbol=symbol,
                        start=start,
                        end=start + timedelta(minutes=minutes),
                        open=Decimal(row[fields["open"]]),
                        high=Decimal(row[fields["high"]]),
                        low=Decimal(row[fields["low"]]),
                        close=Decimal(row[fields["close"]]),
                        volume=int(Decimal(row[fields["volume"]] or "0")),
                    )
                )
            except (ValueError, InvalidOperation, KeyError) as exc:
                problems.append(f"{lineno}行目: {exc}")
    return bars, problems


def cmd_import_bars(args: argparse.Namespace) -> int:
    config, _ = _load(args, allow_example=True)
    setup_logging("INFO", None, "import")
    bars, problems = parse_bars_csv(Path(args.file), args.symbol)
    for p in problems[:20]:
        log.warning("読み飛ばしました: %s", p)
    repo = Repository(config.storage.path)
    try:
        saved = repo.save_bars(bars)
    finally:
        repo.close()
    days = sorted({b.start.date() for b in bars})
    span = f"{days[0]}〜{days[-1]}" if days else "なし"
    print(
        f"{args.symbol}: {saved} 本の足を取り込みました(期間 {span}、読み飛ばし {len(problems)} 行)"
    )
    return 0 if saved else 1


def _parse_params(items: Sequence[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError(f"--param は key=value の形式で指定してください: {item}")
        params[key.strip()] = yaml.safe_load(value)
    return params


def cmd_backtest(args: argparse.Namespace) -> int:
    config, _ = _load(args, allow_example=True)
    setup_logging("ERROR", None, "backtest")
    overrides: dict[str, Any] = {}
    if args.param:
        overrides["strategy"] = {
            **config.strategy.model_dump(mode="json"),
            "params": {**config.strategy.params, **_parse_params(args.param)},
        }
    if args.symbols:
        known = {s.symbol: s for s in config.universe}
        overrides["universe"] = [
            known[s].model_dump(mode="json") if s in known else {"symbol": s}
            for s in args.symbols.split(",")
        ]
    if overrides:
        try:
            config = AppConfig.model_validate({**config.model_dump(mode="json"), **overrides})
        except ValidationError as exc:
            raise CliError(f"指定したパラメータに誤りがあります:\n{exc}") from exc
    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    repo = Repository(config.storage.path)
    try:
        runner = BacktestRunner(config, repo, start, end, source=args.source)
        try:
            result = asyncio.run(runner.run())
        except NoDataError as exc:
            raise CliError(
                f"{exc}\n`dtbot collect` でティックを貯めるか、"
                "`dtbot import-bars` で足を取り込んでください"
            ) from exc
        repo.save_backtest(result.to_record(datetime.now(JST)))
    finally:
        repo.close()
    s = result.summary
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "-"
    wr = f"{s.win_rate:.1%}" if s.win_rate is not None else "-"
    sharpe = f"{s.sharpe:.2f}" if s.sharpe is not None else "-"
    print(
        "\n".join(
            [
                f"バックテスト {result.run_id}({start}〜{end}、{', '.join(result.symbols)})",
                f"  損益の合計      {s.net_pnl:+,.0f}円(資金に対して {s.return_pct:+.2%})",
                f"  取引回数        {s.trades}回(勝率 {wr}、プロフィットファクター {pf})",
                f"  最大ドローダウン {s.max_drawdown:,.0f}円",
                f"  シャープレシオ   {sharpe}(取引日 {s.trading_days}日)",
                "  結果は `dtbot dashboard` の「バックテスト」で見られます。",
            ]
        )
    )
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    config, secrets_ = _load(args, allow_example=True)
    _setup_logs(config, "dashboard")
    _check_port_free(config.dashboard.host, config.dashboard.port)
    repo = Repository(config.storage.path)
    try:
        asyncio.run(_serve(None, config, repo, secrets_, open_browser=not args.no_browser))
    finally:
        repo.close()
    return 0


def cmd_mock_kabu(args: argparse.Namespace) -> int:
    from daytrade_bot.devtools.mock_kabu import MockKabuStation  # noqa: PLC0415

    load_dotenv(Path(args.env))
    secrets_ = load_secrets()
    password = (
        secrets_.api_password.get_secret_value() if secrets_.api_password else "mock-password"
    )
    setup_logging("INFO", None, "mock")
    mock = MockKabuStation(password=password)
    print(
        f"kabuステーションAPI のモックを http://127.0.0.1:{args.port}/kabusapi で起動します。\n"
        f"設定の kabu.port を {args.port} にすると、paper や live をモックに対して試せます。"
    )
    uvicorn.run(
        mock.app, host="127.0.0.1", port=args.port, log_level="warning", ws="websockets-sansio"
    )
    return 0


# ---- 引数 --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dtbot", description="kabuステーションAPI 対応のデイトレードボット"
    )
    parser.add_argument("--version", action="version", version=f"dtbot {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="設定ファイル(既定: config/settings.yaml)"
    )
    common.add_argument("--env", default=str(DEFAULT_ENV), help="秘密情報のファイル(既定: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str, func: Callable[[argparse.Namespace], int], help_: str
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(func=func)
        return p

    add("init", cmd_init, "設定ファイルと .env のひな形を作る")
    p = add("demo", cmd_demo, "合成データで動かす(口座不要)")
    p.add_argument("--speed", type=float, default=30.0, help="時間の速さ(既定: 30倍速)")
    p.add_argument("--seed", type=int, default=None, help="乱数の種(同じ値なら同じ値動き)")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    add("check", cmd_check, "kabuステーションAPI への接続を確かめる")
    add("collect", cmd_collect, "時価を受信して保存する")
    p = add("import-bars", cmd_import_bars, "1分足の CSV を取り込む")
    p.add_argument("file", help="CSV ファイル")
    p.add_argument("--symbol", required=True, help="銘柄コード")
    p = add("backtest", cmd_backtest, "保存したデータでバックテストする")
    p.add_argument("--from", dest="date_from", required=True, help="開始日(例: 2026-10-01)")
    p.add_argument("--to", dest="date_to", required=True, help="終了日(例: 2026-10-31)")
    p.add_argument(
        "--source", choices=["bars", "ticks"], default="bars", help="使うデータ(既定: bars)"
    )
    p.add_argument("--symbols", help="銘柄をカンマ区切りで指定(既定: 設定の universe)")
    p.add_argument(
        "--param",
        action="append",
        default=[],
        help="戦略のパラメータを上書き(例: range_minutes=30)",
    )
    p = add("paper", cmd_paper, "本物の時価で、約定だけシミュレーションする")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    p = add("live", cmd_live, "本番の発注をする")
    p.add_argument("--confirm-live", action="store_true", help="本番の発注をすることを確認した")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    p = add("dashboard", cmd_dashboard, "結果を見る(売買はしない)")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    p = add("mock-kabu", cmd_mock_kabu, "kabuステーションAPI のモックを起動する")
    p.add_argument("--port", type=int, default=18090)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except CliError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
