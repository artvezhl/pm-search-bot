# Deploy и эксплуатация (GitHub Build + GHCR + SSH)

## 1) Что нужно на сервере

- Ubuntu/Debian сервер с установленными `git`, `docker`, `docker compose plugin`.
- Клон репозитория в постоянную папку, например: `/opt/pm-search-bot`.
- Заполненный `polymarket-bot/.env` (обязательно: `ALCHEMY_POLYGON_URL`, `POSTGRES_DSN*`, `REDIS_URL`, Telegram-переменные).
- Открыт порт API (по необходимости) и доступ к Telegram API.

## 2) GitHub Secrets для автодеплоя

В репозитории добавь Secrets:

- `SSH_HOST` — IP/домен сервера
- `SSH_PORT` — обычно `22`
- `SSH_USER` — пользователь SSH
- `SSH_PRIVATE_KEY` — приватный ключ для доступа
- `DEPLOY_PATH` — путь к репозиторию на сервере (например `/opt/pm-search-bot`)
- `GHCR_USERNAME` — GitHub username для `docker login ghcr.io` на сервере
- `GHCR_TOKEN` — GitHub token (PAT) с правом `read:packages`

Workflow: `.github/workflows/deploy-ssh.yml`

Триггеры:
- push в `main` (если изменились файлы в `polymarket-bot/**`)
- ручной запуск (`workflow_dispatch`)

Что делает pipeline:
- билдит Docker image в GitHub Actions
- пушит image в GHCR (`ghcr.io/<repo>/polymarket-bot:<sha>`)
- по SSH делает `docker pull` этого тега на сервере и перезапускает сервисы

## 3) Первый запуск на сервере (вручную)

```bash
cd /opt/pm-search-bot/polymarket-bot
export APP_IMAGE=ghcr.io/<owner>/<repo>/polymarket-bot:latest
docker compose -f docker-compose.server.yml up -d --remove-orphans
docker compose -f docker-compose.server.yml exec -T app alembic upgrade head
curl -fsS http://127.0.0.1:8000/health
```

## 4) Что запустить, чтобы гарантированно подтягивались трейды

Базовый прогрев/проверка ingestion:

```bash
cd /opt/pm-search-bot/polymarket-bot

# 1) sync рынков
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_market_sync

# 2) backfill истории (день за днем от нового к старому)
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_history_load_daily_range --args='[null, "2025-10-27 00:00:00", "2026-04-26 00:00:00"]'

# 3) дотянуть метаданные только по торгуемым рынкам
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_market_sync_traded_metadata

# 4) разовый poller
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_trade_poller
```

Дальше `beat` будет запускать задачи по расписанию автоматически.

## 5) Как проверять, что анализ проходит корректно

Проверки в БД:

```bash
cd /opt/pm-search-bot/polymarket-bot

docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT COUNT(*) AS trades FROM pm_trades;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT MIN(block_time), MAX(block_time) FROM pm_trades;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT COUNT(*) AS resolved FROM pm_markets WHERE resolved = true;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT COUNT(*) AS wallets, COUNT(*) FILTER (WHERE is_smart_wallet) AS smart_wallets FROM pm_wallets;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT id, status, created_at FROM pm_copy_signals ORDER BY created_at DESC LIMIT 20;"
```

Проверки через API:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/stats
curl -fsS "http://127.0.0.1:8000/api/trades/latest?limit=20"
curl -fsS "http://127.0.0.1:8000/api/players/top?limit=20"
```

Проверки логов:

```bash
cd /opt/pm-search-bot/polymarket-bot
docker compose -f docker-compose.server.yml logs -f --tail=200 app worker beat
```

## 6) Проверка через Telegram-бота

Команды для оперативного контроля:

- `/status` — режим, баланс, число позиций
- `/stats` — агрегаты (winrate, PnL, Sharpe)
- `/smartwallets` — топ smart-wallet кошельков
- `/signals` — последние copy-сигналы
- `/clusters` — последние кластеры
- `/positions` — текущие открытые позиции

Если команды не отвечают:
- проверь `TELEGRAM_BOT_TOKEN`
- проверь `TELEGRAM_ALLOWED_IDS`
- проверь `TRADER_NOTIFY_CHAT_ID`
- проверь логи `app` контейнера
