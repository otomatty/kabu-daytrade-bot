"""kabuステーションAPI の REST クライアント。

- トークンは初回に発行し、401(トークン無効)が返ったら1回だけ発行し直して再送する。
  kabuステーションは早朝に強制ログアウトするため、日をまたいで動かすとトークンが無効になる
- 流量制限(発注系・情報系ともに秒間10件)を超えないよう、種類ごとに送信間隔を空ける
- 発注は「送れたか分からない」状態を区別する。自動の再送は、確実に処理されていない
  (接続できなかった、429、401)場合に限る。応答が途切れた発注を再送すると二重発注になりうる
- 数値は Decimal として読み込む(価格の丸め誤差を防ぐ)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal

import httpx

from daytrade_bot.brokers.base import (
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderStateUnknown,
)
from daytrade_bot.brokers.kabu import codes
from daytrade_bot.config import KabuConfig

log = logging.getLogger(__name__)

Category = Literal["order", "info"]


class KabuApiError(BrokerError):
    """kabuステーションAPI がエラーを返した。"""

    def __init__(self, status: int, code: int | None, message: str) -> None:
        super().__init__(f"HTTP {status} / コード {code}: {message}")
        self.status = status
        self.code = code
        self.api_message = message


class KabuAuthError(BrokerUnavailable):
    """トークンを発行できない(kabuステーション未ログイン、APIパスワード違いなど)。"""


class RateLimiter:
    """種類ごとに、最低限の送信間隔を守る。"""

    def __init__(self, per_second: float) -> None:
        if per_second <= 0:
            raise ValueError("per_second は正の値である必要があります")
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    def penalize(self, seconds: float) -> None:
        """流量制限のエラーを受けたら、しばらく送信を止める。"""
        self._next = max(self._next, time.monotonic() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + self._interval


def _loads(content: bytes) -> Any:
    return json.loads(content, parse_float=Decimal) if content else None


class KabuRestClient:
    def __init__(
        self,
        config: KabuConfig,
        api_password: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
    ) -> None:
        if not api_password:
            raise ValueError("APIパスワードが設定されていません(環境変数 KABU_API_PASSWORD)")
        self.config = config
        self._password = api_password
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.request_timeout_sec, connect=3.0),
            # kabuステーションはローカルで動くので、プロキシの設定は使わない
            trust_env=False,
        )
        self._owns_client = client is None
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._limiters: dict[Category, RateLimiter] = {
            "order": RateLimiter(config.order_rate_per_sec),
            "info": RateLimiter(config.info_rate_per_sec),
        }
        self._max_retries = max_retries
        self.requests = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def has_token(self) -> bool:
        return self._token is not None

    # ---- 認証 -------------------------------------------------------------------------------

    async def authenticate(self) -> str:
        async with self._token_lock:
            return await self._issue_token()

    async def _ensure_token(self) -> str:
        """トークンが無ければ発行する(同時に呼ばれても発行は1回だけ)。"""
        async with self._token_lock:
            if self._token is None:
                return await self._issue_token()
            return self._token

    async def _reauthenticate(self, failed: str | None) -> None:
        """``failed`` のトークンが拒否されたので発行し直す。

        別のリクエストがすでに発行し直していれば、そのトークンを使う。発行のたびに前のトークンが
        無効になるので、同時に401を受けた複数のリクエストがそれぞれ発行すると、互いに無効にしてしまう。
        """
        async with self._token_lock:
            if self._token is not None and self._token != failed:
                return
            self._token = None
            await self._issue_token()

    async def _issue_token(self) -> str:
        """トークンを発行する(呼び出し側で _token_lock を取っていること)。"""
        try:
            resp = await self._client.post("/token", json={"APIPassword": self._password})
        except httpx.HTTPError as exc:
            raise KabuAuthError(
                f"kabuステーションに接続できません({self.config.base_url})。"
                "kabuステーションが起動しているか確認してください"
            ) from exc
        body = _loads(resp.content) or {}
        if resp.status_code != 200 or not body.get("Token"):
            code = body.get("Code")
            if code == codes.BAD_PASSWORD_CODE:
                raise KabuAuthError("APIパスワードが違います")
            if code in codes.NOT_LOGGED_IN_CODES:
                raise KabuAuthError("kabuステーションにログインしていません")
            raise KabuAuthError(
                f"トークンを発行できませんでした(HTTP {resp.status_code} / {code}: "
                f"{body.get('Message', '')})"
            )
        self._token = str(body["Token"])
        log.info("kabuステーションAPIのトークンを発行しました")
        return self._token

    # ---- 送信 -------------------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        category: Category,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> Any:
        """リクエストを送り、JSON を返す。

        ``idempotent`` が偽(発注)の場合、応答が途切れたら ``OrderStateUnknown`` を送出する。
        """
        await self._ensure_token()
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            await self._limiters[category].acquire()
            used_token = self._token
            headers = {"X-API-KEY": used_token or ""}
            self.requests += 1
            try:
                resp = await self._client.request(
                    method, path, json=body, params=params, headers=headers
                )
            except httpx.ConnectError as exc:
                raise BrokerUnavailable(f"kabuステーションに接続できません: {exc}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if idempotent and attempt <= self._max_retries:
                    await asyncio.sleep(0.2 * attempt)
                    continue
                if idempotent:
                    raise BrokerError(f"kabuステーションの応答がありません: {exc}") from exc
                raise OrderStateUnknown(
                    f"発注の応答が途切れました({type(exc).__name__})。照合で確認します"
                ) from exc
            status = resp.status_code
            if status == 200:
                return _loads(resp.content)
            payload = _safe_json(resp.content)
            code = payload.get("Code") if isinstance(payload, dict) else None
            message = str(payload.get("Message", "")) if isinstance(payload, dict) else ""
            if status == 401 and not reauthed:
                # トークンが無効になった(早朝のログアウト、別のトークンの発行など)
                reauthed = True
                await self._reauthenticate(used_token)
                continue
            if status == 429 and attempt <= self._max_retries:
                # 同じ種類のリクエスト全体を少し止めてから再送する(揺らぎを入れて重ならないように)
                delay = 0.5 * 2 ** (attempt - 1)
                self._limiters[category].penalize(delay + random.uniform(0, delay / 2))  # noqa: S311
                continue
            if status >= 500:
                if idempotent and attempt <= self._max_retries:
                    await asyncio.sleep(0.2 * attempt)
                    continue
                if not idempotent:
                    raise OrderStateUnknown(
                        f"kabuステーションが内部エラーを返しました(HTTP {status} / {code}: "
                        f"{message})。照合で確認します"
                    )
            raise KabuApiError(status, int(code) if code is not None else None, message)

    # ---- API --------------------------------------------------------------------------------

    async def send_order(self, payload: dict[str, Any]) -> str:
        try:
            result = await self.request(
                "POST", "/sendorder", category="order", body=payload, idempotent=False
            )
        except KabuApiError as exc:
            raise OrderRejected(exc.api_message or str(exc), code=exc.code) from exc
        if not isinstance(result, dict) or result.get("Result") != 0 or not result.get("OrderId"):
            raise OrderRejected(f"発注が受け付けられませんでした: {result}")
        return str(result["OrderId"])

    async def cancel_order(self, order_id: str) -> None:
        try:
            result = await self.request(
                "PUT",
                "/cancelorder",
                category="order",
                body={"OrderId": order_id},
                idempotent=False,
            )
        except KabuApiError as exc:
            raise OrderRejected(exc.api_message or str(exc), code=exc.code) from exc
        if not isinstance(result, dict) or result.get("Result") != 0:
            raise OrderRejected(f"取消が受け付けられませんでした: {result}")

    async def orders(
        self,
        *,
        product: int = codes.PRODUCT_ALL,
        updtime: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"product": product, "details": "true"}
        if updtime:
            params["updtime"] = updtime
        if symbol:
            params["symbol"] = symbol
        result = await self.request(
            "GET", "/orders", category="info", params=params, idempotent=True
        )
        return list(result or [])

    async def positions(self, *, product: int, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"product": product, "addinfo": "false"}
        if symbol:
            params["symbol"] = symbol
        result = await self.request(
            "GET", "/positions", category="info", params=params, idempotent=True
        )
        return list(result or [])

    async def register(self, symbols: Sequence[str], exchange: int) -> list[dict[str, Any]]:
        if len(symbols) > 50:
            raise ValueError("登録できる銘柄は最大50です")
        body = {"Symbols": [{"Symbol": s, "Exchange": exchange} for s in symbols]}
        result = await self.request("PUT", "/register", category="info", body=body, idempotent=True)
        return list((result or {}).get("RegistList") or [])

    async def unregister(self, symbols: Sequence[str], exchange: int) -> None:
        body = {"Symbols": [{"Symbol": s, "Exchange": exchange} for s in symbols]}
        await self.request("PUT", "/unregister", category="info", body=body, idempotent=True)

    async def board(self, symbol: str, exchange: int) -> dict[str, Any]:
        result = await self.request(
            "GET", f"/board/{symbol}@{exchange}", category="info", idempotent=True
        )
        return dict(result or {})

    async def soft_limit(self) -> dict[str, Any]:
        result = await self.request("GET", "/apisoftlimit", category="info", idempotent=True)
        return dict(result or {})

    async def wallet_margin(self) -> dict[str, Any]:
        result = await self.request("GET", "/wallet/margin", category="info", idempotent=True)
        return dict(result or {})

    async def wallet_cash(self) -> dict[str, Any]:
        result = await self.request("GET", "/wallet/cash", category="info", idempotent=True)
        return dict(result or {})


def _safe_json(content: bytes) -> Any:
    try:
        return _loads(content)
    except ValueError:
        return {"Message": content[:200].decode("utf-8", "replace")}
