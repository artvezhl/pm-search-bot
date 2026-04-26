"""Polymarket data-api client.

`data-api.polymarket.com` exposes user-level aggregates that replace the
deprecated The Graph subgraph. We use:

    GET /positions?user=<addr>                    open positions
    GET /activity?user=<addr>&limit=...            recent trades + transfers
    GET /leaderboard?type=volume&window=all        top wallets (bootstrap seeds)
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logger import logger


class DataAPIClient:
    def __init__(self, host: str | None = None, timeout: float = 15.0) -> None:
        self._host = (host or get_settings().data_api_host).rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DataAPIClient":
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
        resp = await self._client.get(url, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def get_positions(self, address: str) -> list[dict[str, Any]]:
        try:
            data = await self._get("/positions", params={"user": address})
        except httpx.HTTPStatusError as e:
            logger.warning("data-api /positions failed for {}: {}", address, e)
            return []
        if not data:
            return []
        return data if isinstance(data, list) else data.get("positions") or []

    async def get_activity(
        self, address: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        try:
            data = await self._get(
                "/activity",
                params={"user": address, "limit": str(limit)},
            )
        except httpx.HTTPStatusError as e:
            logger.warning("data-api /activity failed for {}: {}", address, e)
            return []
        if not data:
            return []
        return data if isinstance(data, list) else data.get("activity") or []

    async def get_leaderboard(
        self, metric: str = "volume", window: str = "week", limit: int = 100
    ) -> list[dict[str, Any]]:
        try:
            data = await self._get(
                "/leaderboard",
                params={"type": metric, "window": window, "limit": str(limit)},
            )
        except httpx.HTTPStatusError as e:
            logger.warning("data-api /leaderboard failed: {}", e)
            return []
        if not data:
            return []
        return data if isinstance(data, list) else data.get("leaderboard") or []
