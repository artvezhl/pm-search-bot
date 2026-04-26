"""Live executor — sends real orders through py-clob-client.

Safety nets implemented:
    * slippage guard: `ask_depth_usd(5) >= size_usd * 1.5` before entering
    * order size reduced to available depth if the check would fail
    * price sanity: reject if price <= 0 or price >= 1
    * all trade intents are pushed through a per-execution semaphore
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import BotPosition
from app.ingestion.clob_client import CLOBClient, Orderbook
from app.logger import logger
from app.signal.portfolio_state import close_position, open_position

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    PY_CLOB_AVAILABLE = True
except Exception:  # noqa: BLE001
    PY_CLOB_AVAILABLE = False


@dataclass(slots=True)
class LiveFill:
    success: bool
    entry_price: float
    size_usd: float
    token_amount: float
    reason: str = "ok"
    order_id: str | None = None


class LiveExecutor:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._semaphore = asyncio.Semaphore(1)

    def _build_clob(self) -> Any:
        if not PY_CLOB_AVAILABLE:
            raise RuntimeError(
                "py-clob-client is not installed; install requirements or switch to paper mode"
            )
        s = self._settings
        if not s.polymarket_pk:
            raise RuntimeError("POLYMARKET_PK missing; cannot submit live orders")
        client = ClobClient(
            host=s.clob_host,
            key=s.polymarket_pk,
            chain_id=137,
            signature_type=2 if s.polymarket_wallet_type == "safe" else 1,
            funder=s.polymarket_proxy_address or None,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client

    async def _check_slippage(
        self, token_id: str, side: str, size_usd: float
    ) -> tuple[float, Orderbook | None]:
        """Return (max_allowed_size_usd, orderbook). If the book is empty, returns (0, None)."""
        async with CLOBClient() as rest:
            book = await rest.get_orderbook(token_id)
        if side == "BUY":
            depth = book.ask_depth_usd(5)
        else:
            depth = book.bid_depth_usd(5)
        if depth <= 0:
            return 0.0, book
        # spec: available_liquidity >= size * 1.5
        max_allowed = depth / 1.5
        return max_allowed, book

    async def open(
        self,
        *,
        market_id: str,
        outcome: str,
        token_id: str,
        price: float,
        size_usd: float,
        cluster_id: int | None,
        leader_addrs: list[str],
    ) -> tuple[LiveFill, BotPosition | None]:
        if price <= 0 or price >= 1:
            return LiveFill(False, price, size_usd, 0.0, "invalid price"), None

        max_allowed, _ = await self._check_slippage(token_id, "BUY", size_usd)
        effective_size = min(size_usd, max_allowed * 0.95)
        if effective_size < 1:
            return LiveFill(False, price, size_usd, 0.0, "insufficient book depth"), None

        token_amount = effective_size / price

        async with self._semaphore:
            loop = asyncio.get_running_loop()
            client = self._build_clob()

            def _submit() -> dict[str, Any]:
                args = OrderArgs(
                    price=price,
                    size=token_amount,
                    side=BUY,
                    token_id=token_id,
                )
                signed = client.create_order(args)
                return client.post_order(signed, OrderType.GTC)

            try:
                response = await loop.run_in_executor(None, _submit)
            except Exception as e:  # noqa: BLE001
                logger.error("LIVE OPEN failed: {}", e)
                return LiveFill(False, price, effective_size, 0.0, f"submit error: {e}"), None

        order_id = str(response.get("orderID") or response.get("order_id") or "")
        logger.info(
            "LIVE OPEN market={} outcome={} @ {:.4f} size=${:.2f} order_id={}",
            market_id,
            outcome,
            price,
            effective_size,
            order_id,
        )

        async with AsyncSessionLocal() as session:
            pos = await open_position(
                session=session,
                market_id=market_id,
                outcome=outcome,
                token_id=token_id,
                entry_price=price,
                size_usd=effective_size,
                token_amount=token_amount,
                cluster_id=cluster_id,
                leader_addrs=leader_addrs,
                simulation=False,
            )
            await session.commit()

        return (
            LiveFill(True, price, effective_size, token_amount, "ok", order_id),
            pos,
        )

    async def close(
        self, *, position: BotPosition, price: float, reason: str
    ) -> BotPosition:
        token_id = position.token_id
        if not token_id:
            raise RuntimeError("cannot submit SELL: token_id missing on position")

        max_allowed, _ = await self._check_slippage(token_id, "SELL", float(position.size_usd))
        effective_tokens = min(
            float(position.token_amount), max_allowed * 0.95 / max(price, 1e-6)
        )
        if effective_tokens < 1e-6:
            logger.warning(
                "LIVE CLOSE for {}: book empty, closing only in DB", position.market_id
            )
        else:
            async with self._semaphore:
                loop = asyncio.get_running_loop()
                client = self._build_clob()

                def _submit() -> dict[str, Any]:
                    args = OrderArgs(
                        price=price,
                        size=effective_tokens,
                        side=SELL,
                        token_id=token_id,
                    )
                    signed = client.create_order(args)
                    return client.post_order(signed, OrderType.GTC)

                try:
                    await loop.run_in_executor(None, _submit)
                except Exception as e:  # noqa: BLE001
                    logger.error("LIVE CLOSE submit failed: {}", e)

        async with AsyncSessionLocal() as session:
            merged = await session.merge(position)
            updated = await close_position(
                session=session, position=merged, exit_price=price, exit_reason=reason
            )
            await session.commit()
            await session.refresh(updated)
        return updated
