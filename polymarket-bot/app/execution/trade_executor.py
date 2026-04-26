"""Trade Executor — the glue between signals and executors.

Given an ENTER SignalDecision, it:
    1. asks RiskManager for a legal position size
    2. routes to PaperExecutor or LiveExecutor depending on SIMULATION_MODE
    3. emits a Telegram notification on success/failure

The `handle_exit` path does the same for ExitIntent objects from ExitWatcher.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import BotPosition, PMCopySignal, PMMarket
from app.execution.live_executor import LiveExecutor
from app.execution.paper_executor import PaperExecutor
from app.logger import logger
from app.notifications.notifier import get_notifier
from app.signal.exit_watcher import ExitIntent
from app.signal.risk_manager import RiskManager
from app.signal.signal_engine import SignalDecision


@dataclass(slots=True)
class TradeResult:
    accepted: bool
    position_id: int | None
    entry_price: float
    size_usd: float
    reason: str


class TradeExecutor:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._risk = RiskManager()
        self._paper = PaperExecutor()
        self._live = LiveExecutor() if not self._settings.simulation_mode else None

    def _pick_token_id(self, decision: SignalDecision) -> str | None:
        market = decision.market
        if market is None:
            return None
        return market.token_id_yes if decision.cluster.outcome == "YES" else market.token_id_no

    async def handle_entry(self, decision: SignalDecision) -> TradeResult:
        if decision.decision != "ENTER":
            return TradeResult(False, None, 0.0, 0.0, f"decision={decision.decision}")

        sized = await self._risk.size_for(decision.cluster.market_id)
        notifier = get_notifier()
        if not sized.accepted:
            await notifier.send(f"⛔ Skipped {decision.cluster.market_id}: {sized.reason}")
            return TradeResult(False, None, 0.0, 0.0, sized.reason)

        token_id = self._pick_token_id(decision)
        entry_price = decision.cluster.avg_price

        if self._settings.simulation_mode:
            fill, pos = await self._paper.open(
                market_id=decision.cluster.market_id,
                outcome=decision.cluster.outcome,
                token_id=token_id,
                price=entry_price,
                size_usd=sized.size_usd,
                cluster_id=decision.cluster_id,
                leader_addrs=decision.cluster.leader_addrs,
            )
        else:
            if self._live is None:
                self._live = LiveExecutor()
            if not token_id:
                return TradeResult(False, None, 0.0, 0.0, "live mode needs token_id")
            fill, pos = await self._live.open(
                market_id=decision.cluster.market_id,
                outcome=decision.cluster.outcome,
                token_id=token_id,
                price=entry_price,
                size_usd=sized.size_usd,
                cluster_id=decision.cluster_id,
                leader_addrs=decision.cluster.leader_addrs,
            )

        if not fill.success or pos is None:
            await notifier.send(f"❌ Entry failed: {decision.cluster.market_id}: {fill.reason}")
            return TradeResult(False, None, fill.entry_price, fill.size_usd, fill.reason)

        mode = "PAPER" if self._settings.simulation_mode else "LIVE"
        msg = (
            f"✅ {mode} ENTER\n"
            f"Market: {decision.cluster.market_id}\n"
            f"Outcome: {decision.cluster.outcome} @ {fill.entry_price:.4f}\n"
            f"Size: ${fill.size_usd:.2f} ({fill.token_amount:.2f} tokens)\n"
            f"Cluster score: {decision.score:.3f} | leaders: {len(decision.cluster.leader_addrs)}"
        )
        await notifier.send(msg)
        return TradeResult(True, int(pos.id), fill.entry_price, fill.size_usd, "ok")

    async def handle_exit(self, intent: ExitIntent) -> BotPosition:
        notifier = get_notifier()
        exit_price = intent.current_price
        if exit_price is None:
            exit_price = float(intent.position.entry_price)

        if self._settings.simulation_mode:
            updated = await self._paper.close(
                position=intent.position, price=exit_price, reason=intent.reason
            )
        else:
            if self._live is None:
                self._live = LiveExecutor()
            try:
                updated = await self._live.close(
                    position=intent.position, price=exit_price, reason=intent.reason
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Live close failed: {}", e)
                await notifier.send(
                    f"❌ Exit failed market={intent.position.market_id}: {e}"
                )
                raise

        mode = "PAPER" if self._settings.simulation_mode else "LIVE"
        msg = (
            f"🏁 {mode} EXIT ({intent.reason})\n"
            f"Market: {updated.market_id}\n"
            f"Outcome: {updated.outcome} @ {exit_price:.4f}\n"
            f"PnL: ${float(updated.pnl_usd or 0):.2f}"
        )
        await notifier.send(msg)
        return updated

    async def execute_pm_signal(self, signal_id: int) -> bool:
        """Execute `pm_copy_signals` item (paper/live) and update lifecycle status."""
        async with AsyncSessionLocal() as session:
            signal = (
                await session.execute(select(PMCopySignal).where(PMCopySignal.id == signal_id))
            ).scalars().first()
            if signal is None or signal.status != "PENDING":
                return False
            market = (
                await session.execute(select(PMMarket).where(PMMarket.condition_id == signal.condition_id))
            ).scalars().first()
            if market is None or market.resolved:
                signal.status = "SKIPPED"
                signal.skip_reason = "market_unavailable_or_resolved"
                await session.commit()
                return False

            token_id = str(market.asset_id or "")
            if not token_id:
                signal.status = "SKIPPED"
                signal.skip_reason = "missing_token_id"
                await session.commit()
                return False
            try:
                if self._settings.simulation_mode:
                    fill, _ = await self._paper.open(
                        market_id=signal.condition_id or "",
                        outcome=signal.token_outcome or "YES",
                        token_id=token_id,
                        price=float(signal.suggested_price or 0),
                        size_usd=float(signal.suggested_amount or 0),
                        cluster_id=None,
                        leader_addrs=[signal.trigger_wallet],
                    )
                else:
                    if self._live is None:
                        self._live = LiveExecutor()
                    fill, _ = await self._live.open(
                        market_id=signal.condition_id or "",
                        outcome=signal.token_outcome or "YES",
                        token_id=token_id,
                        price=float(signal.suggested_price or 0),
                        size_usd=float(signal.suggested_amount or 0),
                        cluster_id=None,
                        leader_addrs=[signal.trigger_wallet],
                    )
                if not fill.success:
                    signal.status = "FAILED"
                    signal.skip_reason = fill.reason
                else:
                    signal.status = "EXECUTED"
                    signal.execution_price = fill.entry_price
                    signal.execution_amount = fill.size_usd
                await session.commit()
                return fill.success
            except Exception as e:  # noqa: BLE001
                signal.status = "FAILED"
                signal.skip_reason = str(e)
                await session.commit()
                return False
