from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest

from daytrade_bot import cli
from daytrade_bot.core.clock import JST, AcceleratedClock, ManualClock, OffsetClock, SystemClock
from daytrade_bot.core.session import MarketCalendar, Phase, TradingSchedule
from daytrade_bot.core.strategy import load_strategy_class
from daytrade_bot.core.ticksize import TickTable
from daytrade_bot.feeds.demo import DemoFeed
from daytrade_bot.logging_setup import JSTFormatter, reset_logging, setup_logging
from daytrade_bot.storage.db import Repository
from daytrade_bot.strategies.orb import OpeningRangeBreakout
from tests.conftest import TRADING_DAY, D, at
from tests.helpers import orb_day


class TestClocks:
    async def test_system_and_offset(self) -> None:
        now = SystemClock().now()
        assert now.tzinfo is not None
        await SystemClock().sleep(0)
        c = OffsetClock(at(10))
        assert at(10) <= c.now() < at(10, 0, 5)
        await c.sleep(0)
        with pytest.raises(ValueError, match="タイムゾーン"):
            OffsetClock(at(10).replace(tzinfo=None))

    async def test_accelerated(self) -> None:
        c = AcceleratedClock(at(10), speed=1000)
        assert c.speed == 1000
        await c.sleep(10)  # 実時間では0.01秒
        assert c.now() >= at(10, 0, 10)
        c.jump_to(at(12, 30))
        assert at(12, 30) <= c.now() < at(12, 31)
        with pytest.raises(ValueError, match="speed"):
            AcceleratedClock(at(10), 0)
        with pytest.raises(ValueError, match="タイムゾーン"):
            AcceleratedClock(at(10).replace(tzinfo=None), 1)
        with pytest.raises(ValueError, match="タイムゾーン"):
            c.jump_to(at(10).replace(tzinfo=None))

    async def test_manual(self) -> None:
        c = ManualClock(at(10))
        await c.sleep(30)
        assert c.now() == at(10, 0, 30)
        with pytest.raises(ValueError, match="戻す"):
            c.set(at(9))
        with pytest.raises(ValueError, match="タイムゾーン"):
            c.set(at(11).replace(tzinfo=None))
        with pytest.raises(ValueError, match="タイムゾーン"):
            ManualClock(at(10).replace(tzinfo=None))


class TestDemoFeed:
    def make(self, start: Any) -> tuple[DemoFeed, AcceleratedClock, list[Any]]:
        clock = AcceleratedClock(start, speed=10_000)
        feed = DemoFeed(
            clock,
            TradingSchedule(MarketCalendar()),
            {"7203": D(1500), "6758": D(2400)},
            {"6758": TickTable.TOPIX500},
            seed=1,
        )
        ticks: list[Any] = []
        feed.set_tick_handler(ticks.append)
        return feed, clock, ticks

    async def test_emits_valid_ticks(self) -> None:
        feed, _, ticks = self.make(at(10))
        await feed.start(["7203", "6758", "9999"])
        for _ in range(50):
            feed.emit_once(at(10))
        await feed.stop()
        assert {t.symbol for t in ticks} == {"7203", "6758"}
        for t in ticks:
            assert t.ask is not None and t.bid is not None and t.ask > t.bid == t.price
        vols = [t.cum_volume for t in ticks if t.symbol == "7203"]
        assert vols == sorted(vols)
        assert feed.last_message_at == at(10)

    def test_skips_lunch_and_night(self) -> None:
        feed, clock, _ = self.make(at(11, 45))
        assert feed.skip_closed_time(at(11, 45))
        assert at(12, 30) <= clock.now() < at(12, 31)
        assert feed.last_message_at == at(12, 30)
        assert feed.skip_closed_time(at(15, 31))
        assert clock.now().date() == date(2026, 11, 10)
        assert feed.skip_closed_time(at(10, day=date(2026, 11, 7)))  # 土曜
        assert clock.now().date() == date(2026, 11, 9)
        assert not feed.skip_closed_time(at(10))
        assert not feed.skip_closed_time(at(7))  # 早朝は寄付きまで待つ

    async def test_runs_in_background(self) -> None:
        feed, _, ticks = self.make(at(9, 0, 1))
        await feed.start(["7203"])
        assert feed.connected
        await asyncio.sleep(0.05)
        await feed.stop()
        assert ticks and not feed.connected


def test_strategy_loader() -> None:
    assert load_strategy_class("orb") is OpeningRangeBreakout
    assert (
        load_strategy_class("daytrade_bot.strategies.orb:OpeningRangeBreakout")
        is OpeningRangeBreakout
    )
    with pytest.raises(ValueError, match="見つかりません"):
        load_strategy_class("nope")
    with pytest.raises(TypeError, match="サブクラス"):
        load_strategy_class("daytrade_bot.core.models:Tick")
    with pytest.raises(ValueError, match="銘柄が1つも"):
        OpeningRangeBreakout([])
    with pytest.raises(ValueError, match="min_range_pct"):
        OpeningRangeBreakout(["7203"], {"min_range_pct": 5, "max_range_pct": 1})


def test_logging_setup(tmp_path: Path) -> None:
    path = setup_logging("INFO", tmp_path / "logs", "t")
    assert path is not None
    logging.getLogger("daytrade_bot.test").info("日本語のログ")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "日本語のログ" in path.read_text(encoding="utf-8")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    assert "+09:00" in JSTFormatter().formatTime(rec)
    assert setup_logging("INFO", None, "t") is None
    reset_logging()


# ---- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    for name in ("KABU_API_PASSWORD", "DTBOT_WEBHOOK_URL", "DTBOT_DASHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield tmp_path
    reset_logging()


def test_init_creates_templates_once(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init"]) == 0
    assert (workdir / "config" / "settings.yaml").exists()
    assert "KABU_API_PASSWORD=" in (workdir / ".env").read_text(encoding="utf-8")
    assert cli.main(["init"]) == 0
    assert "すでにあります" in capsys.readouterr().out


def test_missing_config_message(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check"]) == 2
    err = capsys.readouterr().err
    assert "見つかりません" in err and "dtbot init" in err


def test_invalid_config_message(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workdir / "config").mkdir()
    (workdir / "config" / "settings.yaml").write_text("universe: []\n", encoding="utf-8")
    assert cli.main(["check"]) == 2
    assert "誤りがあります" in capsys.readouterr().err


def test_live_guard_rails(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["init"])
    assert cli.main(["live", "--confirm-live", "--no-browser"]) == 2
    assert "live_enabled" in capsys.readouterr().err
    cfg = workdir / "config" / "settings.yaml"
    from tests.test_kabu import free_port

    text = cfg.read_text(encoding="utf-8").replace("live_enabled: false", "live_enabled: true")
    cfg.write_text(text.replace("port: 8765", f"port: {free_port()}"), encoding="utf-8")
    assert cli.main(["live", "--no-browser"]) == 2
    assert "--confirm-live" in capsys.readouterr().err
    # APIパスワードが無ければ起動しない
    assert cli.main(["live", "--confirm-live", "--no-browser"]) == 2
    captured = capsys.readouterr()
    assert "本番の発注を開始します" in captured.out
    assert "KABU_API_PASSWORD" in captured.err


def test_import_bars_and_backtest(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["init"])
    csv_path = workdir / "bars.csv"
    rows = ["datetime,open,high,low,close,volume"]
    rows += [
        f"{b.start:%Y/%m/%d %H:%M},{b.open},{b.high},{b.low},{b.close},{b.volume}"
        for b in orb_day("7203")
    ]
    rows.append("not-a-date,1,1,1,1,1")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert cli.main(["import-bars", str(csv_path), "--symbol", "7203"]) == 0
    out = capsys.readouterr().out
    assert "36 本" in out and "読み飛ばし 1 行" in out
    code = cli.main(
        [
            "backtest",
            "--from",
            "2026-11-09",
            "--to",
            "2026-11-09",
            "--symbols",
            "7203",
            "--param",
            "take_profit_r=2.5",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "取引回数        1回" in out
    repo = Repository(workdir / "data" / "dtbot.sqlite3")
    (run,) = repo.list_backtests()
    repo.close()
    assert run["params"]["take_profit_r"] == 2.5 and run["symbols"] == ["7203"]
    # データの無い期間
    assert cli.main(["backtest", "--from", "2026-12-01", "--to", "2026-12-02"]) == 2
    assert "dtbot collect" in capsys.readouterr().err
    assert (
        cli.main(["backtest", "--from", "2026-11-09", "--to", "2026-11-09", "--param", "nokey"])
        == 2
    )
    assert (
        cli.main(
            ["backtest", "--from", "2026-11-09", "--to", "2026-11-09", "--param", "range_minutes=0"]
        )
        == 2
    )


def test_import_bars_rejects_bad_columns(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = workdir / "bad.csv"
    path.write_text("time,price\n09:00,1\n", encoding="utf-8")
    assert cli.main(["import-bars", str(path), "--symbol", "7203"]) == 2
    assert "列が足りません" in capsys.readouterr().err
    path.write_text(
        "date,time,open,high,low,close,volume\n2026-11-09,09:00,100,101,99,100,10\n",
        encoding="utf-8",
    )
    bars, problems = cli.parse_bars_csv(path, "7203")
    assert len(bars) == 1 and not problems and bars[0].start == at(9)


def test_port_check(workdir: Path) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        with pytest.raises(cli.CliError, match="使われています"):
            cli._check_port_free("127.0.0.1", port)


def test_demo_start_time() -> None:
    from daytrade_bot.config import AppConfig

    start = cli._demo_start(AppConfig.model_validate({"universe": [{"symbol": "7203"}]}))
    assert start.time() == time(8, 58) and start.tzinfo is not None
    assert MarketCalendar().is_trading_day(start.date())


async def test_check_against_mock(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from daytrade_bot.devtools.mock_kabu import MockKabuStation
    from tests.test_kabu import free_port

    mock = MockKabuStation(password="secret-pass", auto_ticks=False)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(mock.app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    try:
        cli.main(["init"])
        cfg = workdir / "config" / "settings.yaml"
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace(
                "  host: localhost\n", f"  host: 127.0.0.1\n  port: {port}\n"
            ),
            encoding="utf-8",
        )
        (workdir / ".env").write_text("KABU_API_PASSWORD=secret-pass\n", encoding="utf-8")
        code = await asyncio.to_thread(cli.main, ["check"])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "OK  トークンの発行" in out and "mock-5.0.0" in out and "すべて確認できました" in out
        (workdir / ".env").write_text("KABU_API_PASSWORD=wrong\n", encoding="utf-8")
        monkeypatch.delenv("KABU_API_PASSWORD")
        code = await asyncio.to_thread(cli.main, ["check"])
        assert code == 1
        assert "APIパスワードが違います" in capsys.readouterr().out
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


def test_today_net_qty(tmp_path: Path) -> None:
    from daytrade_bot.core.models import Fill, Side

    repo = Repository(tmp_path / "x.sqlite3")
    from datetime import datetime

    now = datetime.now(JST)
    repo.save_fill("r", "live", Fill("a", "c", "7203", Side.BUY, 300, D(1), D(0), now))
    repo.save_fill("r", "live", Fill("b", "c", "7203", Side.SELL, 100, D(1), D(0), now))
    repo.save_fill("r", "paper", Fill("p", "c", "6758", Side.BUY, 100, D(1), D(0), now))
    assert cli._today_net_qty(repo)() == {"7203": 200}
    repo.close()


def test_phase_labels_cover_all() -> None:
    assert all(p.label for p in Phase)
    assert TRADING_DAY.weekday() == 0


async def test_paper_mode_end_to_end(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`dtbot paper` をモックの kabuステーションに対して起動し、ダッシュボードから停止する。"""
    import httpx
    import uvicorn

    from daytrade_bot.devtools.mock_kabu import MockKabuStation
    from tests.test_kabu import free_port

    mock = MockKabuStation(password="secret-pass", tick_interval_sec=0.05)
    kabu_port, dash_port = free_port(), free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            mock.app, host="127.0.0.1", port=kabu_port, log_level="warning", ws="websockets-sansio"
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    try:
        cli.main(["init"])
        cfg = workdir / "config" / "settings.yaml"
        text = cfg.read_text(encoding="utf-8")
        text = text.replace("  host: localhost\n", f"  host: 127.0.0.1\n  port: {kabu_port}\n")
        text = text.replace("port: 8765", f"port: {dash_port}")
        cfg.write_text(text, encoding="utf-8")
        token = "paper-e2e-token-0123456789"
        (workdir / ".env").write_text(
            f"KABU_API_PASSWORD=secret-pass\nDTBOT_DASHBOARD_TOKEN={token}\n", encoding="utf-8"
        )
        runner = asyncio.create_task(asyncio.to_thread(cli.main, ["paper", "--no-browser"]))
        base = f"http://127.0.0.1:{dash_port}"
        async with httpx.AsyncClient(
            base_url=base, headers={"X-DTBot-Token": token}, trust_env=False
        ) as c:
            state: dict[str, Any] = {}
            for _ in range(200):
                try:
                    state = (await c.get("/api/state")).json()
                    if any(s["last"] for s in state.get("symbols", [])):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            assert state["mode"] == "paper"
            assert any(s["last"] for s in state["symbols"])  # モックの時価を受信している
            assert sorted(mock.registered) == sorted(["7203", "6758", "9984"])
            r = await c.post("/api/control/stop", json={"confirm": True})
            assert r.json()["ok"]
        assert await asyncio.wait_for(runner, 10) == 0
        out = capsys.readouterr().out
        assert f"http://127.0.0.1:{dash_port}/#token={token}" in out
        assert mock.registered == []  # 停止時に銘柄の登録を解除した
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


def test_live_startup_failure_is_reported(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """kabuステーションに接続できないときは、黙って終わらずに理由を表示する。"""
    from tests.test_kabu import free_port

    cli.main(["init"])
    cfg = workdir / "config" / "settings.yaml"
    text = cfg.read_text(encoding="utf-8").replace("live_enabled: false", "live_enabled: true")
    text = text.replace("  host: localhost\n", f"  host: 127.0.0.1\n  port: {free_port()}\n")
    cfg.write_text(text.replace("port: 8765", f"port: {free_port()}"), encoding="utf-8")
    (workdir / ".env").write_text("KABU_API_PASSWORD=x\n", encoding="utf-8")
    assert cli.main(["live", "--confirm-live", "--no-browser"]) == 2
    assert "kabuステーションに接続できません" in capsys.readouterr().err
