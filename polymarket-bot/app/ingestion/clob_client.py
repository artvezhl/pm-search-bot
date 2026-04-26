"""Thin async wrapper around Polymarket CLOB REST endpoints.

Public endpoints used:
    GET  /markets                         list tradable markets
    GET  /book?token_id=...               orderbook depth
    GET  /price?token_id=...&side=buy|sell spot quote
    GET  /data/trades                     historical trades (public, paginated)

We intentionally avoid the py-clob-client here — it is used only in
execution/live_executor.py when we need signed orders. Ingestion is read-only
and the raw REST surface is simpler to operate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logger import logger

try:
    from py_clob_client.clob_types import ApiCreds, RequestArgs
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.signer import Signer

    PY_CLOB_AUTH_AVAILABLE = True
except Exception:  # noqa: BLE001
    PY_CLOB_AUTH_AVAILABLE = False


@dataclass(slots=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass(slots=True)
class Orderbook:
    token_id: str
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]

    @property
    def best_bid(self) -> float | None:
        return max((lv.price for lv in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((lv.price for lv in self.asks), default=None)

    def ask_depth_usd(self, levels: int = 5) -> float:
        top = sorted(self.asks, key=lambda lv: lv.price)[:levels]
        return sum(lv.price * lv.size for lv in top)

    def bid_depth_usd(self, levels: int = 5) -> float:
        top = sorted(self.bids, key=lambda lv: lv.price, reverse=True)[:levels]
        return sum(lv.price * lv.size for lv in top)


class CLOBClient:
    def __init__(self, host: str | None = None, timeout: float = 15.0) -> None:
        self._host = (host or get_settings().clob_host).rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._trades_endpoint_available = True
        self._settings = get_settings()
        self._l2_auth_enabled = all(
            [
                self._settings.polymarket_pk,
                self._settings.poly_api_key,
                self._settings.poly_api_secret,
                self._settings.poly_api_passphrase,
            ]
        )
        self._auth_warned = False

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build L2-auth headers for CLOB private routes using .env creds."""
        if not self._l2_auth_enabled:
            return {}
        if not PY_CLOB_AUTH_AVAILABLE:
            if not self._auth_warned:
                logger.warning(
                    "py-clob-client auth helpers are unavailable; requests remain unauthenticated"
                )
                self._auth_warned = True
            return {}
        try:
            signer = Signer(self._settings.polymarket_pk, 137)
            creds = ApiCreds(
                api_key=self._settings.poly_api_key,
                api_secret=self._settings.poly_api_secret,
                api_passphrase=self._settings.poly_api_passphrase,
            )
            req = RequestArgs(method=method.upper(), request_path=path)
            return create_level_2_headers(signer, creds, req)
        except Exception as e:  # noqa: BLE001
            if not self._auth_warned:
                logger.warning("Failed to build CLOB auth headers: {}", e)
                self._auth_warned = True
            return {}

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CLOBClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._host}{path}"
        headers = self._auth_headers("GET", path)
        resp = await self._client.get(url, params=params, headers=headers or None)
        if resp.status_code == 429:
            logger.warning("CLOB rate-limited on {}", path)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def fetch_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """`GET /markets` — paginated list of markets.

        Returns the raw response. The caller handles pagination via `next_cursor`.
        """
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self._get("/markets", params=params)

    async def fetch_active_markets(self, max_pages: int = 20) -> list[dict[str, Any]]:
        """Iterate through `/markets` pages and return ingestible markets.

        We intentionally keep this broad (not only "active/open"), because the
        ingestion layer should index as much trade history as possible and the
        CLOB flags are inconsistent across old/new markets.
        """
        markets: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max_pages):
            page = await self.fetch_markets(cursor)
            # CLOB may return one of:
            #   {"data": [...], "next_cursor": "..."}
            #   {"markets": [...], "next_cursor": "..."}
            #   [...]  (plain list)
            if isinstance(page, list):
                data = page
                next_cursor = ""
            elif isinstance(page, dict):
                data = page.get("data") or page.get("markets") or []
                next_cursor = page.get("next_cursor") or page.get("nextCursor") or ""
            else:
                data = []
                next_cursor = ""

            for m in data:
                # Keep all non-archived markets that have identifiers/tokens.
                if m.get("archived") is True:
                    continue
                if not (m.get("condition_id") or m.get("conditionId")):
                    continue
                if not (m.get("tokens") or m.get("tokenIds")):
                    continue
                markets.append(m)
            cursor = next_cursor
            if not cursor or cursor == "LTE=":
                break
        return markets

    async def get_orderbook(self, token_id: str) -> Orderbook:
        data = await self._get("/book", params={"token_id": token_id})
        bids = [
            OrderbookLevel(price=float(lv["price"]), size=float(lv["size"]))
            for lv in data.get("bids", [])
        ]
        asks = [
            OrderbookLevel(price=float(lv["price"]), size=float(lv["size"]))
            for lv in data.get("asks", [])
        ]
        return Orderbook(token_id=token_id, bids=bids, asks=asks)

    async def get_price(self, token_id: str, side: str = "sell") -> float | None:
        data = await self._get("/price", params={"token_id": token_id, "side": side})
        if "price" not in data:
            return None
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None

    async def fetch_trades(
        self,
        market_id: str | None = None,
        maker_address: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch recent trades with graceful fallback.

        Primary: `/data/trades` (may require auth in some environments).
        Fallback: `/trades` (public, payload shape may vary).
        """
        params: dict[str, Any] = {"limit": str(limit)}
        if market_id:
            params["market"] = market_id
        if maker_address:
            params["maker_address"] = maker_address

        # Try /data/trades unless we already confirmed it's unauthorized.
        if self._trades_endpoint_available:
            try:
                data = await self._get("/data/trades", params=params)
                if isinstance(data, dict):
                    return data.get("data") or data.get("trades") or []
                return data or []
            except httpx.HTTPStatusError as e:
                if e.response is not None and e.response.status_code == 401:
                    self._trades_endpoint_available = False
                    logger.warning(
                        "CLOB /data/trades returned 401, switching to /trades fallback"
                    )
                else:
                    logger.warning("CLOB /data/trades failed: {}", e)

        # Fallback to /trades (public route in many deployments).
        try:
            data = await self._get("/trades", params=params)
        except httpx.HTTPStatusError as e:
            logger.warning("CLOB /trades fallback failed: {}", e)
            return []
        if isinstance(data, dict):
            return data.get("data") or data.get("trades") or []
        return data or []


def parse_trade_timestamp(value: Any) -> datetime:
    """Polymarket timestamps can be seconds (int/str) or ISO strings."""
    if value is None:
        return datetime.now(tz=timezone.utc)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value)
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=timezone.utc)
