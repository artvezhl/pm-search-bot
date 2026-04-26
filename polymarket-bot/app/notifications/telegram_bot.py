"""Telegram bot — read-only command handlers.

Commands:
    /start, /help                general help
    /status                       mode, balance, open positions
    /positions                    open positions (live P&L)
    /clusters                     5 most recent clusters with score >= 0.5
    /stats                        aggregated metrics (trades, winrate, PnL, Sharpe)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import BotPosition, Cluster, PMCopySignal, PMWallet
from app.ingestion.clob_client import CLOBClient
from app.logger import logger


HELP_TEXT = (
    "*Polymarket copy-trading bot*\n"
    "/status — mode, balance, open positions count\n"
    "/positions — list open positions with live PnL\n"
    "/clusters — recent detected clusters\n"
    "/stats — aggregated performance\n"
    "/smartwallets — top smart wallets\n"
    "/signals — latest copy signals\n"
)


BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "help"),
    BotCommand("help", "help"),
    BotCommand("status", "mode, balance, open positions"),
    BotCommand("positions", "open positions with live PnL"),
    BotCommand("clusters", "recent detected clusters"),
    BotCommand("stats", "aggregated performance metrics"),
    BotCommand("smartwallets", "top smart wallets"),
    BotCommand("signals", "latest copy signals"),
]


def _is_authorised(user_id: int | None) -> bool:
    allowed = get_settings().allowed_telegram_user_ids
    if not allowed:
        return True
    return user_id is not None and int(user_id) in allowed


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


async def _current_balance() -> tuple[float, float]:
    """Return (balance, realised_pnl). balance = starting + realized."""
    s = get_settings()
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed"
            )
        )
        realised = float(r.scalar() or 0)
    return s.starting_balance + realised, realised


async def _live_price(token_id: str | None) -> float | None:
    if not token_id:
        return None
    try:
        async with CLOBClient() as clob:
            return await clob.get_price(token_id, side="sell")
    except Exception:  # noqa: BLE001
        return None


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return

    s = get_settings()
    balance, realised = await _current_balance()

    async with AsyncSessionLocal() as session:
        open_count = int(
            (
                await session.execute(
                    select(func.count(BotPosition.id)).where(BotPosition.status == "open")
                )
            ).scalar()
            or 0
        )

    mode = "PAPER" if s.simulation_mode else "LIVE"
    text = (
        f"Mode: *{mode}*\n"
        f"Balance: {_fmt_money(balance)} (realised PnL {_fmt_money(realised)})\n"
        f"Open positions: *{open_count}* / {s.max_open_positions}\n"
    )
    await update.message.reply_markdown(text)


async def cmd_positions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BotPosition).where(BotPosition.status == "open").order_by(
                    desc(BotPosition.opened_at)
                )
            )
        ).scalars().all()

    if not rows:
        await update.message.reply_text("No open positions.")
        return

    lines: list[str] = []
    for p in rows[:20]:
        current = await _live_price(p.token_id)
        entry = float(p.entry_price)
        if current is not None and entry > 0:
            pnl = (current - entry) * float(p.token_amount)
            pnl_pct = (current - entry) / entry
            live = f" | now {current:.4f} ({pnl_pct:+.1%}) PnL {_fmt_money(pnl)}"
        else:
            live = ""
        lines.append(
            f"{p.market_id[:10]}… {p.outcome} @ {entry:.4f} "
            f"size={_fmt_money(float(p.size_usd))}{live}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_clusters(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return

    async with AsyncSessionLocal() as session:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        rows = (
            await session.execute(
                select(Cluster)
                .where(Cluster.created_at >= since)
                .order_by(desc(Cluster.score))
                .limit(10)
            )
        ).scalars().all()

    if not rows:
        await update.message.reply_text("No clusters detected in the last 24h.")
        return

    lines = []
    for c in rows:
        lines.append(
            f"#{c.id} market={c.market_id[:10]}… outcome={c.outcome} "
            f"score={float(c.score):.2f} size={c.size} vol={_fmt_money(float(c.total_volume_usd))}"
        )
    await update.message.reply_text("\n".join(lines))


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(252)


async def cmd_stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return

    async with AsyncSessionLocal() as session:
        closed = (
            await session.execute(
                select(BotPosition).where(BotPosition.status == "closed")
            )
        ).scalars().all()

    total = len(closed)
    wins = sum(1 for p in closed if float(p.pnl_usd or 0) > 0)
    winrate = wins / total if total else 0.0
    pnl_total = sum(float(p.pnl_usd or 0) for p in closed)
    returns = [
        float(p.pnl_usd or 0) / float(p.size_usd) for p in closed if float(p.size_usd) > 0
    ]
    sharpe = _sharpe(returns)

    s = get_settings()
    balance = s.starting_balance + pnl_total

    last24h = sum(
        float(p.pnl_usd or 0)
        for p in closed
        if p.closed_at and p.closed_at >= datetime.now(tz=timezone.utc) - timedelta(hours=24)
    )
    last7d = sum(
        float(p.pnl_usd or 0)
        for p in closed
        if p.closed_at and p.closed_at >= datetime.now(tz=timezone.utc) - timedelta(days=7)
    )

    text = (
        f"Closed trades: *{total}*  | wins: {wins}\n"
        f"Winrate: *{winrate:.1%}*  |  Sharpe≈{sharpe:.2f}\n"
        f"Total PnL: {_fmt_money(pnl_total)}\n"
        f"24h PnL: {_fmt_money(last24h)}  |  7d PnL: {_fmt_money(last7d)}\n"
        f"Balance: {_fmt_money(balance)}\n"
    )
    await update.message.reply_markdown(text)


async def cmd_smartwallets(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(PMWallet)
                .where(PMWallet.is_smart_wallet.is_(True))
                .order_by(desc(PMWallet.smart_score))
                .limit(10)
            )
        ).scalars().all()
    if not rows:
        await update.message.reply_text("No smart wallets yet.")
        return
    await update.message.reply_text(
        "\n".join(
            f"{w.address[:10]}… score={float(w.smart_score or 0):.3f} win={float(w.win_rate or 0):.1%}"
            for w in rows
        )
    )


async def cmd_signals(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Access denied.")
        return
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(PMCopySignal).order_by(desc(PMCopySignal.created_at)).limit(10)
            )
        ).scalars().all()
    if not rows:
        await update.message.reply_text("No copy signals yet.")
        return
    await update.message.reply_text(
        "\n".join(
            f"#{r.id} {r.status} {r.condition_id[:10] if r.condition_id else '-'} "
            f"{r.token_outcome or '-'} ${float(r.suggested_amount or 0):.2f}"
            for r in rows
        )
    )


def build_application() -> Application | None:
    s = get_settings()
    if not s.telegram_bot_token:
        logger.info("Telegram bot disabled (TELEGRAM_BOT_TOKEN not set)")
        return None

    app = (
        ApplicationBuilder()
        .token(s.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("clusters", cmd_clusters))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("smartwallets", cmd_smartwallets))
    app.add_handler(CommandHandler("signals", cmd_signals))
    return app


async def _sync_bot_commands(app: Application) -> None:
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Telegram commands synced: {} commands", len(BOT_COMMANDS))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to sync Telegram commands: {}", e)


async def run_bot() -> None:
    app = build_application()
    if app is None:
        return
    await app.initialize()
    await _sync_bot_commands(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=["message"])
    try:
        # Block forever
        import asyncio

        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
