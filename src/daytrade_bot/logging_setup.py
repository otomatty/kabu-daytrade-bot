"""ログの設定(コンソールと、日本時間のタイムスタンプ付きのファイル)。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from daytrade_bot.core.clock import JST


class JSTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 - 標準ライブラリの名前
        moment = datetime.fromtimestamp(record.created, JST)
        return moment.strftime(datefmt) if datefmt else moment.isoformat(timespec="milliseconds")


_MARK = "_dtbot_handler"


def _own(handler: logging.Handler) -> None:
    setattr(handler, _MARK, True)


def reset_logging() -> None:
    """このモジュールが付けたハンドラを外して閉じる(ほかのハンドラには触らない)。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()


def setup_logging(level: str, directory: Path | None, name: str) -> Path | None:
    """ルートロガーを設定し、ログファイルのパスを返す。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # Windows のコンソール(cp932)で表示できない文字があっても落ちないようにする
            reconfigure(errors="replace")
    reset_logging()
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(JSTFormatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    _own(console)
    root.addHandler(console)
    path: Path | None = None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"dtbot-{name}.log"
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
        handler.setFormatter(JSTFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _own(handler)
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
