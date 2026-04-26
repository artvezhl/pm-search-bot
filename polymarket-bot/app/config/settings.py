"""Runtime configuration loaded from environment variables (.env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ---
    simulation_mode: bool = True
    starting_balance: float = 10_000.0

    # --- Polymarket credentials ---
    polymarket_pk: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""
    polymarket_proxy_address: str = ""
    polymarket_wallet_type: str = "safe"  # "safe" | "proxy"
    polygon_rpc_url: str = "https://polygon-bor.publicnode.com"
    alchemy_polygon_url: str = ""

    # --- Polymarket endpoints ---
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    dune_api_host: str = "https://api.dune.com/api/v1"
    dune_api_key: str = ""
    dune_query_id: int | None = None

    # --- Infra ---
    postgres_dsn: str = "postgresql+asyncpg://polybot:polybot@postgres:5432/polybot"
    postgres_dsn_sync: str = "postgresql+psycopg2://polybot:polybot@postgres:5432/polybot"
    redis_url: str = "redis://redis:6379/0"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_allowed_ids: str = ""  # comma-separated
    trader_notify_chat_id: str = ""

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Player tracker ---
    min_winrate: float = 0.60
    min_total_volume_usd: float = 5_000.0
    min_trades_count: int = 10
    player_stats_lookback_days: int = 90

    # --- Cluster detector ---
    dbscan_eps: float = 0.05
    dbscan_min_samples: int = 3
    time_window_minutes: int = 60

    # --- Signal engine ---
    min_cluster_size: int = 3
    min_position_usd: float = 500.0
    max_entry_lag_min: int = 15
    min_cluster_score: float = 0.65
    min_market_liquidity: float = 10_000.0

    # --- Risk manager ---
    max_position_pct: float = 0.05
    max_open_positions: int = 10
    max_daily_loss_pct: float = 0.10
    position_stop_loss_pct: float = 0.50
    max_same_market_exposure: float = 0.10

    # --- Exit watcher ---
    exit_on_first_leader: bool = True
    max_hold_days: int = 7
    exit_lag_seconds: int = 30

    # --- Scheduling (seconds) ---
    ingestion_poll_interval_sec: int = 30
    cluster_detect_interval_sec: int = 300
    player_tracker_interval_sec: int = 3600
    exit_check_interval_sec: int = 60
    daily_report_hour_local: int = 9
    market_sync_cron_hours: int = 2
    wallet_analyzer_cron_hours: int = 6
    network_detector_cron_hours: int = 12
    cleanup_cron_weekday: int = 0
    cleanup_cron_hour_utc: int = 3

    # --- PM analytics module ---
    pm_history_days: int = 180
    pm_min_resolved_markets: int = 15
    pm_smart_wallet_threshold: float = 0.55
    pm_min_wallet_win_rate: float = 0.55
    pm_min_wallet_volume_usdc: float = 500.0
    pm_network_window_minutes: int = 5
    pm_network_lookback_days: int = 30
    pm_max_parallel_market_polls: int = 5
    pm_copy_max_price: float = 0.75
    pm_copy_min_hours_to_close: float = 2.0
    bot_capital_usdc: float = 1000.0
    max_single_position_usdc: float = 50.0

    # --- On-chain backfill ---
    onchain_backfill_enabled: bool = True
    onchain_backfill_days: int = 30
    onchain_backfill_chunk_size: int = 2000

    # --- Optional (v2) ---
    use_the_graph: bool = False
    thegraph_api_key: str = ""

    # --- Logging ---
    log_level: str = "INFO"

    @field_validator("telegram_allowed_ids")
    @classmethod
    def _strip_ids(cls, v: str) -> str:
        return (v or "").strip()

    @property
    def allowed_telegram_user_ids(self) -> set[int]:
        raw = self.telegram_allowed_ids
        if not raw:
            return set()
        out: set[int] = set()
        for chunk in raw.split(","):
            s = chunk.strip()
            if not s:
                continue
            try:
                out.add(int(s))
            except ValueError:
                continue
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
