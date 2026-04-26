"""Celery bootstrap.

Run modes:
    # worker
    celery -A app.tasks.celery_app worker --loglevel=info -Q default,ingestion,analysis,exit
    # beat
    celery -A app.tasks.celery_app beat --loglevel=info

Celery is used for *orchestration* — scheduling player tracker / cluster
detection / exit-watcher scans. Live ingestion (CLOB WebSocket) runs inside
the `app` process, not Celery, because WebSocket-driven streams are poorly
modelled as Celery jobs.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "polybot",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=[
        "app.tasks.ingestion_tasks",
        "app.tasks.analysis_tasks",
        "app.tasks.exit_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_concurrency=4,
    task_default_queue="default",
    task_routes={
        "app.tasks.ingestion_tasks.*": {"queue": "ingestion"},
        "app.tasks.analysis_tasks.*": {"queue": "analysis"},
        "app.tasks.exit_tasks.*": {"queue": "exit"},
    },
)

celery_app.conf.beat_schedule = {
    "pm-market-sync": {
        "task": "app.tasks.ingestion_tasks.pm_market_sync",
        "schedule": crontab(minute=0, hour="*/2"),
    },
    "pm-trade-poller": {
        "task": "app.tasks.ingestion_tasks.pm_trade_poller",
        "schedule": _settings.ingestion_poll_interval_sec,
    },
    "pm-execute-signal-queue": {
        "task": "app.tasks.ingestion_tasks.pm_execute_signal_queue",
        "schedule": 5.0,
    },
    "pm-wallet-analyzer": {
        "task": "app.tasks.analysis_tasks.update_pm_wallets",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    "pm-network-detector": {
        "task": "app.tasks.analysis_tasks.update_pm_networks",
        "schedule": crontab(minute=0, hour="*/12"),
    },
    "refresh-markets-and-trades": {
        "task": "app.tasks.ingestion_tasks.refresh_markets_and_trades",
        "schedule": _settings.ingestion_poll_interval_sec,
    },
    "backfill-onchain-trades": {
        "task": "app.tasks.ingestion_tasks.backfill_onchain_trades",
        # Heavy task; run periodically, conflict-safe via trade PK dedupe.
        "schedule": crontab(minute=15, hour="*/6"),
    },
    "update-player-stats": {
        "task": "app.tasks.analysis_tasks.update_player_stats",
        "schedule": _settings.player_tracker_interval_sec,
    },
    "detect-clusters-and-signal": {
        "task": "app.tasks.analysis_tasks.detect_clusters_and_signal",
        "schedule": _settings.cluster_detect_interval_sec,
    },
    "scan-exits": {
        "task": "app.tasks.exit_tasks.scan_exits",
        "schedule": _settings.exit_check_interval_sec,
    },
    "daily-report": {
        "task": "app.tasks.analysis_tasks.send_daily_report",
        "schedule": crontab(
            hour=_settings.daily_report_hour_local, minute=0
        ),
    },
    "cleanup-old-pm-trades": {
        "task": "app.tasks.analysis_tasks.cleanup_old_pm_trades",
        "schedule": crontab(minute=0, hour=3, day_of_week=0),
    },
}
