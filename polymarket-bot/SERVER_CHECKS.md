# Server Checks (trades, wallets, signals)

Ниже набор команд для быстрой диагностики на сервере.

## 0) Подготовка

```bash
cd ~/pm-search-bot
export APP_IMAGE=ghcr.io/artvezhl/pm-search-bot/polymarket-bot:latest
docker compose -f docker-compose.server.yml config >/dev/null && echo "compose ok"
```

## 1) Статус сервисов и health

```bash
docker compose -f docker-compose.server.yml ps
curl -fsS http://127.0.0.1:8000/health
docker compose -f docker-compose.server.yml logs --tail=100 app worker beat
```

Проверить отдельный worker для backfill:

```bash
docker compose -f docker-compose.server.yml ps worker worker_backfill beat
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app inspect active_queues
docker compose -f docker-compose.server.yml exec -T worker_backfill celery -A app.tasks.celery_app inspect active_queues
```

Планировщик: основной + резервный (failover):

```bash
# Обычно запущен только beat
docker compose -f docker-compose.server.yml ps beat beat_standby

# Поднять standby-планировщик только при аварии основного
docker compose -f docker-compose.server.yml --profile standby up -d beat_standby
docker compose -f docker-compose.server.yml logs --tail=100 beat beat_standby
```

Важно: не держать `beat` и `beat_standby` одновременно активными, иначе периодические задачи будут запускаться дублями.

## 2) База данных: быстрые проверки

```bash
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT now();"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "\dt pm_*"
```

## 3) Трейды (объем, диапазон, последние записи)

```bash
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT COUNT(*) AS trades_total FROM pm_trades;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT MIN(block_time) AS min_time, MAX(block_time) AS max_time FROM pm_trades;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT block_time, maker_address, condition_id, action, amount_usdc, price FROM pm_trades ORDER BY block_time DESC LIMIT 20;"
```

Почасовой приток за 24 часа:

```bash
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "
SELECT date_trunc('hour', block_time) AS hour, COUNT(*) AS trades
FROM pm_trades
WHERE block_time >= now() - interval '24 hours'
GROUP BY 1
ORDER BY 1;"
```

## 4) Кошельки (сколько, smart, топ)

```bash
# Общая картина:
# wallets_total  - сколько адресов вообще посчитано анализатором.
# smart_wallets  - сколько адресов прошли hard-фильтры + порог smart_score.
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT COUNT(*) AS wallets_total, COUNT(*) FILTER (WHERE is_smart_wallet) AS smart_wallets FROM pm_wallets;"

# Топ по smart_score:
# - smart_score: итоговый скор качества кошелька (выше = лучше).
# - win_rate: доля выигранных resolved-маркетов.
# - total_volume_usdc: общий объем торговли.
# - resolved_markets: сколько resolved рынков попало в расчет.
# - is_smart_wallet: финальный флаг для копитрейдинга.
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT address, smart_score, win_rate, total_volume_usdc, resolved_markets, is_smart_wallet FROM pm_wallets ORDER BY smart_score DESC NULLS LAST LIMIT 20;"
```

Подробный разбор "почему 0 smart-wallets":

```bash
# 1) Есть ли вообще кандидаты по базовым данным:
# Если здесь 0 строк -> проблема не в пороге smart_score, а в исходных трейдах/резолвах.
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "
SELECT
  COUNT(*) AS wallets_total,
  COUNT(*) FILTER (WHERE resolved_markets >= 1) AS wallets_with_resolved,
  COUNT(*) FILTER (WHERE total_volume_usdc >= 500) AS wallets_with_min_volume
FROM pm_wallets;"

# 2) Распределение score:
# Нужно понять, насколько близко кандидаты к порогу PM_SMART_WALLET_THRESHOLD.
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "
SELECT
  percentile_disc(0.5) WITHIN GROUP (ORDER BY smart_score) AS p50,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY smart_score) AS p90,
  MAX(smart_score) AS max_score
FROM pm_wallets
WHERE smart_score IS NOT NULL;"

# 3) Кандидаты около порога:
# Полезно видеть адреса, которые почти проходят, чтобы оценить настройку threshold.
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "
SELECT address, smart_score, win_rate, total_volume_usdc, resolved_markets
FROM pm_wallets
WHERE smart_score IS NOT NULL
ORDER BY smart_score DESC
LIMIT 30;"
```

## 5) Сигналы и сети

```bash
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT id, created_at, trigger_wallet, condition_id, token_outcome, status, suggested_amount, execution_amount FROM pm_copy_signals ORDER BY created_at DESC LIMIT 20;"
docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -c "SELECT wallet_a, wallet_b, co_trade_count, correlation_score, last_co_trade_at FROM pm_wallet_networks ORDER BY correlation_score DESC NULLS LAST LIMIT 20;"
```

## 6) Проверка роста трейдов (до/после задачи)

```bash
before=$(docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -t -A -c "SELECT COUNT(*) FROM pm_trades;")
echo "before=$before"

docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_trade_poller

after=$(docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -t -A -c "SELECT COUNT(*) FROM pm_trades;")
echo "after=$after"
echo "delta=$((after-before))"
```

## 7) Ручной запуск задач ingestion / analysis

```bash
# рынки
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_market_sync

# история on-chain (день за днем, newest -> oldest) в очередь backfill
docker compose -f docker-compose.server.yml exec -T worker python - <<'PY'
from app.tasks.celery_app import celery_app
r = celery_app.send_task(
    "app.tasks.ingestion_tasks.pm_history_load_daily_range",
    args=[None, "2025-10-27 00:00:00", "2026-04-26 00:00:00"],
    queue="backfill",
)
print(r.id)
PY

# история из Dune по Query ID
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_history_load_dune --args='[7372649]'

# метаданные торгуемых рынков
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.ingestion_tasks.pm_market_sync_traded_metadata

# анализ кошельков и сетей
# update_pm_wallets:
#   - агрегирует pm_trades + pm_markets
#   - обновляет win_rate/volume/roi/smart_score
#   - выставляет is_smart_wallet
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.analysis_tasks.update_pm_wallets

# update_pm_networks:
#   - ищет ко-трейдинг пар кошельков
#   - пересчитывает correlation_score и связи
docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.analysis_tasks.update_pm_networks
```

Проверка эффекта после update_pm_wallets:

```bash
# Снимок до/после, чтобы понять был ли фактический пересчет.
before=$(docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -t -A -c "SELECT COUNT(*) FROM pm_wallets WHERE is_smart_wallet = true;")
echo "smart_before=$before"

docker compose -f docker-compose.server.yml exec -T worker celery -A app.tasks.celery_app call app.tasks.analysis_tasks.update_pm_wallets

after=$(docker compose -f docker-compose.server.yml exec -T postgres psql -U polybot -d polybot -t -A -c "SELECT COUNT(*) FROM pm_wallets WHERE is_smart_wallet = true;")
echo "smart_after=$after"
echo "smart_delta=$((after-before))"
```

## 8) API/Telegram быстрые проверки

```bash
curl -fsS http://127.0.0.1:8000/api/pm/wallets/top | jq '.[0:10]'
curl -fsS "http://127.0.0.1:8000/api/trades/latest?limit=20" | jq
curl -fsS "http://127.0.0.1:8000/api/pm/signals?limit=20" | jq
```

Telegram команды:

- `/status`
- `/stats`
- `/smartwallets`
- `/signals`
- `/clusters`
- `/positions`

## 9) Тюнинг Celery для 2 CPU / 4 GB

Что уже выставлено в compose/worker:

- `--concurrency=2`
- `--prefetch-multiplier=1`
- `--max-tasks-per-child=200`

Применить тюнинг после деплоя:

```bash
docker compose -f docker-compose.server.yml up -d --force-recreate worker worker_backfill beat
docker compose -f docker-compose.server.yml ps
```

Проверка нагрузки:

```bash
docker stats --no-stream
docker compose -f docker-compose.server.yml logs --tail=120 worker worker_backfill
```

## 10) Celery: как проверить задачу по `task_id`

Запустить задачу асинхронно и получить `task_id`:

```bash
docker compose -f docker-compose.server.yml exec -T worker \
  python - <<'PY'
from app.tasks.celery_app import celery_app
r = celery_app.send_task(
    "app.tasks.ingestion_tasks.pm_history_load_daily_range",
    args=[None, "2025-10-27 00:00:00", "2026-04-26 00:00:00"],
    queue="backfill",
)
print(r.id)
PY
```

Проверить статус и результат:

```bash
# Подробно через AsyncResult
docker compose -f docker-compose.server.yml exec -T worker \
  python - <<'PY'
from app.tasks.celery_app import celery_app
task_id = '<TASK_ID>'  # замени на реальный id
r = celery_app.AsyncResult(task_id)
print('id=', r.id)
print('state=', r.state)
print('ready=', r.ready())
print('successful=', r.successful() if r.ready() else None)
print('result=', r.result)
print('traceback=', r.traceback)
PY
```

Расшифровка состояний:
- `PENDING` — задача ещё не взята в работу (или id не найден).
- `STARTED`/`RETRY` — выполняется или ретраится.
- `SUCCESS` — завершилась успешно (`result` содержит return-value задачи).
- `FAILURE` — завершилась с ошибкой (смотри `traceback`).
