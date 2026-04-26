# Polymarket Copy-Trading Bot (MVP)

Python-бот, который следит за кошельками на [Polymarket](https://polymarket.com), ищет группы
трейдеров, делающих синхронные ставки с хорошим историческим winrate, и повторяет их сделки.
Архитектура описана в `../NEW_APP.md`.

## Стек

| Компонент | Технология                            |
| --------- | ------------------------------------- |
| Runtime   | Python 3.11                           |
| Очередь   | Celery + Redis                        |
| БД        | PostgreSQL 16 + TimescaleDB           |
| API       | FastAPI + Uvicorn                     |
| Dashboard | Grafana (provisioning)                |
| Бот       | python-telegram-bot (async, v21)      |
| Анализ    | scikit-learn DBSCAN, pandas, numpy    |
| Execution | py-clob-client (live) или paper в БД  |

## Быстрый старт

```bash
cd polymarket-bot
cp .env.example .env
# отредактируй TELEGRAM_BOT_TOKEN и TRADER_NOTIFY_CHAT_ID
docker compose up --build
```

Сервисы:

- `app` — Telegram + ingestion (CLOB REST/WS) + Polygon listener + FastAPI на `:8000`
- `worker` — Celery worker для фоновых задач
- `beat` — Celery beat (расписание)
- `postgres` — TimescaleDB на `:5432`
- `redis` — `:6379`
- `grafana` — `:3000` (admin/admin)

Миграции Alembic прогоняются автоматически в `entrypoint` контейнера `app`.

### Smoke-тест

1. `docker compose up --build`
2. В Telegram пишем боту `/status` — должно ответить `Mode: PAPER, Balance: $10,000.00`
3. Через 1–2 минуты: `docker compose exec postgres psql -U polybot -d polybot -c "SELECT COUNT(*) FROM trades;"` — строки появились
4. Grafana: http://localhost:3000 → Dashboards → **Polymarket** → *Overview*
5. FastAPI health: http://localhost:8000/health → `{"status":"ok"}`

## Режимы

| Режим  | `.env`                           | Исполнение                                        |
| ------ | -------------------------------- | ------------------------------------------------- |
| PAPER  | `SIMULATION_MODE=true` (default) | сделки пишутся в таблицу `bot_positions`, ордера не уходят |
| LIVE   | `SIMULATION_MODE=false`          | ордера отправляются через `py-clob-client`, нужны `POLYMARKET_PK`, `POLY_API_*`, `POLYMARKET_PROXY_ADDRESS` |

Перед переключением в live:

1. Прогнать не меньше 30 дней в paper.
2. Проверить `/stats` — winrate ≥ 0.58, Sharpe ≥ 1, trades ≥ 50.
3. Выставить `MAX_POSITION_PCT=0.02`, `STARTING_BALANCE` = сколько реально кладёшь.
4. `SIMULATION_MODE=false` и `docker compose up -d --build`.

## Telegram команды

Установи `TELEGRAM_ALLOWED_IDS` списком разрешённых Telegram user ID (пусто — без ACL).

| Команда     | Описание                                                     |
| ----------- | ------------------------------------------------------------ |
| `/start`    | Справка                                                      |
| `/help`     | Справка                                                      |
| `/status`   | Режим, баланс, число открытых позиций                        |
| `/positions`| До 20 открытых позиций с live P&L (по CLOB `/price`)          |
| `/clusters` | Топ-10 кластеров за последние 24 часа по score                |
| `/stats`    | Закрытые сделки, winrate, Sharpe, 24h/7d PnL, баланс          |

Уведомления приходят в `TRADER_NOTIFY_CHAT_ID`:

- Вход в позицию (рынок, цена, размер, лидеры)
- Выход (причина: `leader_exit` / `timeout` / `stop_loss`, P&L)
- Пропуски из-за риска (`⛔`)
- Дневной отчёт в `DAILY_REPORT_HOUR_LOCAL` (по умолчанию 09:00 UTC)

## Потоки данных (коротко)

- **Layer 1 (ingestion):** CLOB REST (`/markets`, `/data/trades`) каждые 30 с + CLOB WebSocket для live trades + Polygon RPC listener для `ConditionResolution`. Все трейды всех maker'ов пишутся в `trades` (TimescaleDB hypertable).
- **Layer 2 (analysis):** `PlayerTracker` ежечасно агрегирует winrate/volume/activity_score по каждому адресу; кандидаты — те, кто прошёл пороги. `ClusterDetector` каждые 5 минут прогоняет DBSCAN по BUY-трейдам кандидатов (окно 60 минут). `BehaviorScorer` выдаёт score кластеру.
- **Layer 3 (signal):** `SignalEngine` применяет фильтры из `NEW_APP.md` → `RiskManager` считает максимально допустимый размер позиции → `TradeExecutor` размещает ордер. `ExitWatcher` каждую минуту проверяет позиции: выход по выходу лидера, по таймауту, по стоп-лоссу.
- **Layer 4 (execution + observability):** `PaperExecutor` или `LiveExecutor` (py-clob-client, slippage-guard по глубине стакана). Уведомления в Telegram, FastAPI эндпоинты для Grafana.

## Ключевые env

См. `.env.example`. Наиболее важные:

| Переменная                 | Что                                              |
| -------------------------- | ------------------------------------------------ |
| `SIMULATION_MODE`          | paper (true) / live (false)                      |
| `STARTING_BALANCE`         | база для расчёта риск-лимитов                    |
| `MIN_WINRATE`              | мин. winrate кандидата (PlayerTracker)           |
| `MIN_TOTAL_VOLUME_USD`     | мин. объём кандидата                             |
| `DBSCAN_EPS`               | радиус кластеризации                             |
| `MIN_CLUSTER_SIZE`         | мин. уникальных игроков в кластере               |
| `MIN_CLUSTER_SCORE`        | порог score кластера                             |
| `MAX_POSITION_PCT`         | макс. % депо на позицию                          |
| `MAX_OPEN_POSITIONS`       | макс. открытых позиций одновременно              |
| `MAX_DAILY_LOSS_PCT`       | daily kill-switch                                |
| `POSITION_STOP_LOSS_PCT`   | стоп по позиции                                  |
| `MAX_HOLD_DAYS`            | таймаут позиции                                  |
| `ONCHAIN_BACKFILL_ENABLED` | включить backfill трейдоподобных on-chain событий |
| `ONCHAIN_BACKFILL_DAYS`    | глубина backfill по блокчейну (по умолчанию 30)  |
| `ONCHAIN_BACKFILL_CHUNK_SIZE` | размер чанка блоков для `eth_getLogs`         |

## FastAPI эндпоинты

- `GET /health` — healthcheck
- `GET /api/status` — режим, баланс, открытые позиции
- `GET /api/positions` — открытые позиции
- `GET /api/positions/closed?limit=100` — последние закрытые
- `GET /api/clusters/recent?hours=24&limit=50` — кластеры
- `GET /api/stats` — агрегаты
- `GET /api/players/top?limit=50` — кандидаты-лидеры
- `GET /api/trades/latest?limit=100` — последние трейды из ingestion

## Тесты

Внутри контейнера:

```bash
docker compose run --rm app pytest
```

Локально (если есть виртуальное окружение):

```bash
cd polymarket-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

Тесты покрывают чистые функции: нормализацию трейдов, агрегаты PlayerTracker,
DBSCAN-группировку, helpers BehaviorScorer.

## Deploy / CI-CD

Готовый SSH-пайплайн деплоя и чеклист эксплуатации: `DEPLOY.md`.

## Что отложено в v2

- The Graph Gateway интеграция с API-ключом (флаг `USE_THE_GRAPH`).
- Backtester с корректным look-ahead контролем.
- Advanced Sybil-detection (shared funding sources, временные корреляции).
- Multi-strategy, несколько копи-пресетов параллельно.

## Лицензия / ответственность

Софт предоставляется «как есть». Prediction-рынки — высокорисковые, потери возможны. Не запускайте live-режим, пока не откатали paper-trading и не понимаете каждую из строчек `.env`.
