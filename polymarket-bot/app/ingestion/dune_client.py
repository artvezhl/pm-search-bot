from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings


class DuneClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.dune_api_host.rstrip("/")
        self._api_key = s.dune_api_key
        self._client = httpx.AsyncClient(timeout=45.0)

    async def __aenter__(self) -> "DuneClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
    async def execute_query(self, query_id: int, parameters: dict[str, Any] | None = None) -> str:
        resp = await self._client.post(
            f"{self._base}/query/{query_id}/execute",
            headers={"X-Dune-API-Key": self._api_key},
            json={"query_parameters": parameters or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["execution_id"])

    async def get_results(self, execution_id: str, limit: int = 50_000, offset: int = 0) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}/execution/{execution_id}/results",
            headers={"X-Dune-API-Key": self._api_key},
            params={"limit": str(limit), "offset": str(offset)},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_results_by_next_uri(self, next_uri: str) -> dict[str, Any]:
        target = next_uri
        if not next_uri.startswith("http"):
            target = f"{self._base}{next_uri}"
        resp = await self._client.get(
            target,
            headers={"X-Dune-API-Key": self._api_key},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_execution_status(self, execution_id: str) -> str:
        resp = await self._client.get(
            f"{self._base}/execution/{execution_id}/status",
            headers={"X-Dune-API-Key": self._api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state") or data.get("execution_state") or data.get("status") or ""
        return str(state).upper()

    async def wait_until_completed(
        self,
        execution_id: str,
        *,
        timeout_sec: int = 900,
        poll_interval_sec: float = 2.0,
    ) -> None:
        terminal_success = {"QUERY_STATE_COMPLETED", "COMPLETED"}
        terminal_failed = {
            "QUERY_STATE_FAILED",
            "QUERY_STATE_CANCELLED",
            "QUERY_STATE_EXPIRED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
        }
        waited = 0.0
        while waited < timeout_sec:
            state = await self.get_execution_status(execution_id)
            if state in terminal_success:
                return
            if state in terminal_failed:
                raise RuntimeError(f"Dune execution {execution_id} failed with state={state}")
            await asyncio.sleep(poll_interval_sec)
            waited += poll_interval_sec
        raise TimeoutError(f"Dune execution {execution_id} did not complete within {timeout_sec}s")
