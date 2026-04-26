from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings


class GammaClient:
    def __init__(self) -> None:
        self._base = get_settings().gamma_host.rstrip("/")
        self._client = httpx.AsyncClient(timeout=20.0)

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
    async def get_markets(self, active: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        resp = await self._client.get(
            f"{self._base}/markets",
            params={"active": str(active).lower(), "limit": str(limit)},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data") or data.get("markets") or []
