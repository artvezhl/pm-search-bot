from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PMWalletNetwork
from app.logger import logger


NETWORK_QUERY = """
SELECT
  a.maker_address AS wallet_a,
  b.maker_address AS wallet_b,
  COUNT(*) AS co_trades,
  MAX(GREATEST(a.block_time, b.block_time)) AS last_co_trade_at
FROM pm_trades a
JOIN pm_trades b
  ON a.condition_id = b.condition_id
 AND a.token_outcome = b.token_outcome
 AND a.maker_address < b.maker_address
 AND ABS(EXTRACT(EPOCH FROM (a.block_time - b.block_time))) < :window_seconds
WHERE a.block_time >= :since_time
GROUP BY wallet_a, wallet_b
HAVING COUNT(*) >= 3
"""


class NetworkDetector:
    async def refresh(self) -> int:
        s = get_settings()
        since = datetime.now(tz=timezone.utc) - timedelta(days=s.pm_network_lookback_days)
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text(NETWORK_QUERY),
                    {"window_seconds": s.pm_network_window_minutes * 60, "since_time": since},
                )
            ).mappings().all()
            if not rows:
                return 0
            upserts = []
            for r in rows:
                co = int(r["co_trades"])
                corr = correlation_from_count(co)
                upserts.append(
                    {
                        "wallet_a": r["wallet_a"],
                        "wallet_b": r["wallet_b"],
                        "co_trade_count": co,
                        "correlation_score": Decimal(str(round(corr, 4))),
                        "last_co_trade_at": r["last_co_trade_at"],
                    }
                )
            stmt = pg_insert(PMWalletNetwork).values(upserts)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["wallet_a", "wallet_b"],
                    set_={
                        "co_trade_count": stmt.excluded.co_trade_count,
                        "correlation_score": stmt.excluded.correlation_score,
                        "last_co_trade_at": stmt.excluded.last_co_trade_at,
                    },
                )
            )
            await session.commit()
            logger.info("NetworkDetector: updated {} wallet links", len(upserts))
            return len(upserts)


def correlation_from_count(co_trade_count: int) -> float:
    return min(max(co_trade_count, 0) / 10.0, 1.0)
