# Polymarket Copy-Trading Bot

Python MVP, который следит за кошельками на Polymarket, кластеризует их поведение (DBSCAN) и копирует сделки в paper или live режиме.

Полная документация — [`polymarket-bot/README.md`](polymarket-bot/README.md).
Архитектура — [`NEW_APP.md`](NEW_APP.md).

## Запуск

```bash
cd polymarket-bot
cp .env.example .env
docker compose up --build
```

Сервисы после старта:

- `app` — FastAPI + Telegram + ingestion → http://localhost:8000
- `grafana` → http://localhost:3000 (admin/admin)
- `postgres` (TimescaleDB) → localhost:5432
- `redis` → localhost:6379
