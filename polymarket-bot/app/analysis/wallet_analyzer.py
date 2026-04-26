from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PMMarket, PMTrade, PMWallet
from app.logger import logger


@dataclass(slots=True)
class WalletMetrics:
    win_rate: float
    avg_entry_price: float
    avg_roi: float
    total_volume_usdc: float
    resolved_markets: int
    active_last_30d: bool


def calculate_smart_score(m: WalletMetrics, min_resolved: int) -> float:
    if m.resolved_markets < min_resolved:
        return 0.0
    if m.win_rate < 0.55:
        return 0.0
    if m.total_volume_usdc < 500:
        return 0.0
    if not m.active_last_30d:
        return 0.0

    win_rate_score = min((m.win_rate - 0.55) / 0.35, 1)
    entry_score = max(1 - m.avg_entry_price / 0.45, 0)
    roi_score = min(m.avg_roi / 2.0, 1)
    volume_score = min(max(__import__("math").log10(max(m.total_volume_usdc / 500, 1e-6)) / 3, 0), 1)
    score = win_rate_score * 0.4 + entry_score * 0.3 + roi_score * 0.2 + volume_score * 0.1
    return round(score, 4)


class WalletAnalyzer:
    async def refresh(self) -> int:
        s = get_settings()
        now = datetime.now(tz=timezone.utc)
        active_cutoff = now - timedelta(days=30)
        async with AsyncSessionLocal() as session:
            winners = case(
                (PMMarket.resolution_value == 1, PMTrade.token_outcome == "YES"),
                (PMMarket.resolution_value == 0, PMTrade.token_outcome == "NO"),
                else_=False,
            )
            q = (
                select(
                    PMTrade.maker_address.label("address"),
                    func.count(func.distinct(PMTrade.condition_id)).label("total_markets"),
                    func.count(func.distinct(case((PMMarket.resolved.is_(True), PMTrade.condition_id)))).label("resolved_markets"),
                    func.count(
                        func.distinct(
                            case(
                                (
                                    PMMarket.resolved.is_(True) & winners,
                                    PMTrade.condition_id,
                                )
                            )
                        )
                    ).label("winning_markets"),
                    func.coalesce(func.sum(PMTrade.amount_usdc), 0).label("volume"),
                    func.coalesce(func.avg(PMTrade.price), 0).label("avg_entry_price"),
                    func.min(PMTrade.block_time).label("first_trade_at"),
                    func.max(PMTrade.block_time).label("last_trade_at"),
                )
                .select_from(PMTrade)
                .join(PMMarket, PMMarket.condition_id == PMTrade.condition_id, isouter=True)
                .group_by(PMTrade.maker_address)
            )
            rows = (await session.execute(q)).all()
            upserts: list[dict] = []
            for r in rows:
                resolved_markets = int(r.resolved_markets or 0)
                winning_markets = int(r.winning_markets or 0)
                win_rate = (winning_markets / resolved_markets) if resolved_markets else 0.0
                win_rate = max(0.0, min(win_rate, 1.0))
                total_volume = float(r.volume or 0.0)
                avg_entry_price = float(r.avg_entry_price or 0.0)
                # Proxy ROI in first iteration: expected value on resolved markets.
                avg_roi = ((win_rate * (1 - avg_entry_price)) - ((1 - win_rate) * avg_entry_price)) / max(avg_entry_price, 1e-6)
                active_last_30d = bool(r.last_trade_at and r.last_trade_at >= active_cutoff)
                metrics = WalletMetrics(
                    win_rate=win_rate,
                    avg_entry_price=avg_entry_price,
                    avg_roi=avg_roi,
                    total_volume_usdc=total_volume,
                    resolved_markets=resolved_markets,
                    active_last_30d=active_last_30d,
                )
                score = calculate_smart_score(metrics, s.pm_min_resolved_markets)
                upserts.append(
                    {
                        "address": r.address,
                        "total_markets": int(r.total_markets or 0),
                        "resolved_markets": resolved_markets,
                        "winning_markets": winning_markets,
                        "win_rate": Decimal(str(round(win_rate, 4))),
                        "total_volume_usdc": Decimal(str(round(total_volume, 2))),
                        "total_pnl_usdc": Decimal("0"),
                        "avg_roi": Decimal(str(round(avg_roi, 4))),
                        "avg_entry_price": Decimal(str(round(avg_entry_price, 4))),
                        "avg_entry_days_before_close": None,
                        "first_trade_at": r.first_trade_at,
                        "last_trade_at": r.last_trade_at,
                        "active_last_30d": active_last_30d,
                        "smart_score": Decimal(str(score)),
                        "is_smart_wallet": score >= s.pm_smart_wallet_threshold,
                        "is_blacklisted": int(r.total_markets or 0) > 200,
                        "stats_updated_at": now,
                    }
                )
            if not upserts:
                return 0
            chunk_size = 500
            for i in range(0, len(upserts), chunk_size):
                chunk = upserts[i : i + chunk_size]
                stmt = pg_insert(PMWallet).values(chunk)
                update_cols = {
                    c.name: getattr(stmt.excluded, c.name)
                    for c in PMWallet.__table__.columns
                    if c.name != "address"
                }
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["address"],
                        set_=update_cols,
                    )
                )
            await session.commit()
            logger.info("WalletAnalyzer: updated {} wallets", len(upserts))
            return len(upserts)
