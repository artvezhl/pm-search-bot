# Polymarket Copy-Trading Bot — Архитектура

## Стек технологий

| Компонент     | Технология                               |
| ------------- | ---------------------------------------- |
| Язык          | Python 3.11+                             |
| Очередь задач | Celery + Redis                           |
| База данных   | PostgreSQL (TimescaleDB extension)       |
| Кэш           | Redis                                    |
| Оркестрация   | Docker Compose (dev) / Kubernetes (prod) |

---

## Обзор слоёв

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1 — DATA COLLECTION                                  │
│  Polymarket API · The Graph · Polygon RPC                   │
│                    ↓                                        │
│            Data Ingestion Service                           │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2 — ANALYSIS                                         │
│  Player Tracker · Cluster Detector · Behavior Scorer        │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3 — SIGNAL ENGINE                                    │
│  Signal Engine · Risk Manager · Exit Watcher · Portfolio    │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 4 — EXECUTION                                        │
│  Trade Executor · Telegram · PostgreSQL · Grafana/FastAPI   │
└─────────────────────────────────────────────────────────────┘
```

---

## Слой 1 — Data Collection

### Polymarket CLOB API

**Base URL:** `https://clob.polymarket.com`

Эндпоинты:

- `GET /markets` — список всех активных рынков
- `GET /trades?maker_address=<addr>` — история сделок конкретного адреса
- `GET /orders?market=<market_id>` — текущие ордера на рынке
- WebSocket `wss://clob.polymarket.com/ws` — real-time обновления ордербука и трейдов

### The Graph (GraphQL)

**Endpoint субграфа Polymarket:**
`https://api.thegraph.com/subgraphs/name/polymarket/polymarket-matic`

Используется для:

- Исторических данных о позициях по кошелькам
- P&L по завершённым рынкам
- Первичного построения профилей игроков

Пример запроса:

```graphql
query GetUserTrades($address: String!) {
  trades(
    where: { maker: $address }
    orderBy: timestamp
    orderDirection: desc
    first: 500
  ) {
    id
    market {
      id
      question
    }
    outcome
    price
    size
    timestamp
    type
  }
}
```

### Polygon RPC

**Провайдер:** Alchemy или QuickNode (через `web3.py`)

Используется для:

- Прослушивания on-chain событий: transfer conditional-токенов
- Событий разрешения рынков (`ConditionResolution`)
- Верификации балансов кошельков

### Data Ingestion Service

Python-сервис, который:

- Каждые **30 секунд** пуллит новые трейды через REST
- Держит постоянное **WebSocket-соединение** для real-time потока
- Нормализует данные в единую схему и пишет в PostgreSQL
- Кэширует горячие данные (последние N трейдов по игроку) в Redis

**Схема таблицы `trades`:**

```sql
CREATE TABLE trades (
    id              TEXT PRIMARY KEY,
    market_id       TEXT NOT NULL,
    maker_address   TEXT NOT NULL,
    outcome         TEXT NOT NULL,       -- "YES" / "NO"
    price           NUMERIC NOT NULL,    -- 0..1
    size            NUMERIC NOT NULL,    -- USDC
    side            TEXT NOT NULL,       -- "BUY" / "SELL"
    timestamp       TIMESTAMPTZ NOT NULL,
    tx_hash         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- TimescaleDB hypertable для быстрых time-range запросов
SELECT create_hypertable('trades', 'timestamp');
CREATE INDEX ON trades (maker_address, timestamp DESC);
CREATE INDEX ON trades (market_id, timestamp DESC);
```

---

## Слой 2 — Analysis

### Player Tracker

Для каждого уникального `maker_address` считает скользящие метрики за последние 90 дней:

| Метрика             | Описание                                       |
| ------------------- | ---------------------------------------------- |
| `winrate`           | доля позиций, закрытых в плюс                  |
| `avg_profit_usd`    | средний P&L на одну закрытую позицию           |
| `total_volume_usd`  | суммарный объём — фильтр на минимальный порог  |
| `avg_position_size` | средний размер ставки                          |
| `activity_score`    | равномерность активности (не один раз повезло) |
| `markets_count`     | количество уникальных рынков                   |

Обновляется **каждый час** через Celery-задачу.

**Схема таблицы `player_stats`:**

```sql
CREATE TABLE player_stats (
    address         TEXT PRIMARY KEY,
    winrate         NUMERIC,
    avg_profit_usd  NUMERIC,
    total_volume    NUMERIC,
    activity_score  NUMERIC,
    trades_count    INTEGER,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### Cluster Detector

Ключевой модуль. Выявляет группы игроков, делающих **схожие ставки синхронно**.

**Алгоритм:** DBSCAN из `scikit-learn`

**Пространство признаков для кластеризации:**

```python
features = [
    unix_timestamp_normalized,   # время входа (нормализовано в [0..1])
    market_id_encoded,           # рынок (label encoding или embedding)
    outcome_encoded,             # YES=1 / NO=0
    price_normalized,            # цена входа
    log_position_size            # log(USDC) — сглаживает выбросы
]
```

**Параметры DBSCAN (конфигурируемые):**

```python
DBSCAN_EPS = 0.05          # радиус соседства (подбирается на истории)
DBSCAN_MIN_SAMPLES = 3     # минимум 3 игрока в кластере
TIME_WINDOW_MINUTES = 60   # окно для поиска кластеров
```

**Логика работы:**

1. Раз в 5 минут берём все трейды за последние `TIME_WINDOW_MINUTES`
2. Фильтруем по игрокам с `winrate >= MIN_WINRATE` и `total_volume >= MIN_VOLUME`
3. Запускаем DBSCAN — получаем кластеры
4. Для каждого кластера проверяем: один и тот же рынок + исход + примерно одна цена

### Behavior Scorer

Оценивает каждый обнаруженный кластер по историческим данным:

```python
def score_cluster(cluster: Cluster) -> float:
    # Какой % игроков кластера имеет winrate > порога
    elite_ratio = count(p.winrate > MIN_WINRATE) / len(cluster.players)

    # Насколько синхронно они выходили из прошлых совместных позиций
    sync_exit_score = compute_exit_synchrony(cluster.players, history)

    # Размер ставок (крупные ставки = больше уверенности)
    size_score = normalize(mean(cluster.position_sizes))

    # Итоговый score
    return 0.4 * elite_ratio + 0.4 * sync_exit_score + 0.2 * size_score
```

---

## Слой 3 — Signal Engine

### Signal Engine (фильтры входа)

```python
SIGNAL_FILTERS = {
    "min_cluster_size":     3,       # минимум игроков в кластере
    "min_winrate":          0.60,    # минимальный winrate лидеров
    "min_position_usd":     500,     # минимальная ставка лидера
    "max_entry_lag_min":    15,      # макс. время после первого входа лидера
    "min_cluster_score":    0.65,    # минимальный confidence score
    "min_market_liquidity": 10000,   # минимальная ликвидность рынка в USDC
}
```

**Поток сигнала:**

1. `Cluster Detector` обнаруживает новый кластер
2. `Behavior Scorer` считает `confidence_score`
3. `Signal Engine` применяет все фильтры
4. Если всё ОК → передаёт сигнал в `Risk Manager`

### Risk Manager

```python
RISK_CONFIG = {
    "max_position_pct":         0.05,   # макс. 5% депозита на одну позицию
    "max_open_positions":       10,     # макс. открытых позиций одновременно
    "max_daily_loss_pct":       0.10,   # стоп на день при -10% депозита
    "position_stop_loss_pct":   0.50,   # выход если позиция упала на 50%
    "max_same_market_exposure": 0.10,   # макс. 10% на один рынок
}
```

### Exit Watcher

Отслеживает кошельки лидеров каждого открытого кластера.

**Логика:**

- Подписывается на WebSocket-события трейдов для адресов всех лидеров
- Как только **первый** из лидеров закрывает свою позицию — генерирует сигнал `EXIT`
- Также генерирует `EXIT` если истёк `max_hold_time` (конфигурируемый, например 7 дней)
- Независимо от лидеров — выходит при срабатывании `stop_loss`

```python
EXIT_CONFIG = {
    "exit_on_first_leader":  True,   # выходить при первом выходе любого лидера
    "max_hold_days":         7,      # максимальное время удержания позиции
    "exit_lag_seconds":      30,     # задержка перед исполнением выхода
}
```

### Portfolio State

Хранит все открытые позиции бота.

**Схема таблицы `bot_positions`:**

```sql
CREATE TABLE bot_positions (
    id              SERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    entry_price     NUMERIC NOT NULL,
    size_usd        NUMERIC NOT NULL,
    token_amount    NUMERIC NOT NULL,
    cluster_id      TEXT NOT NULL,          -- ссылка на кластер-источник
    leader_addrs    TEXT[] NOT NULL,        -- адреса лидеров кластера
    status          TEXT DEFAULT 'open',    -- open / closed
    exit_price      NUMERIC,
    pnl_usd         NUMERIC,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ
);
```

---

## Слой 4 — Execution

### Trade Executor

Исполняет ордера через Polymarket CLOB API.

**Библиотека:** `py-clob-client` (официальный SDK Polymarket)

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,         # EOA private key
    chain_id=137             # Polygon mainnet
)

# Создание и отправка лимитного ордера
order_args = OrderArgs(
    token_id=token_id,       # ID conditional token
    price=price,             # 0..1
    size=size,               # количество токенов
    side=BUY,
    order_type=OrderType.GTC
)
signed_order = client.create_order(order_args)
response = client.post_order(signed_order)
```

**Проверки перед исполнением:**

1. Проверка глубины ордербука (`GET /book?token_id=...`) — не входить если слайппедж > 2%
2. Проверка allowance USDC на контракте обмена
3. Расчёт реального размера позиции с учётом Risk Manager

### Observability

**Telegram Bot** (`python-telegram-bot`):

- Уведомление о входе: рынок, исход, цена, размер, лидеры кластера
- Уведомление о выходе: P&L, причина выхода (лидер вышел / стоп-лосс / таймаут)
- Ежедневный отчёт: суммарный P&L, открытые позиции, топ-кластеры

**Grafana + FastAPI дашборд:**

- Метрики в реальном времени: открытые позиции, дневной P&L
- График winrate по времени
- Таблица топ-игроков по `activity_score`
- Latency входа (lag между лидером и ботом)

---

## Ключевые библиотеки

```txt
# Polymarket
py-clob-client          # официальный SDK CLOB API

# Blockchain
web3==6.x               # Polygon RPC, EIP-712

# GraphQL
gql[aiohttp]            # клиент для The Graph

# Анализ данных
scikit-learn            # DBSCAN кластеризация
pandas                  # обработка временных рядов
numpy                   # математика

# Инфраструктура
celery[redis]           # очереди фоновых задач
redis                   # кэш
sqlalchemy[asyncio]     # ORM
asyncpg                 # async PostgreSQL драйвер
alembic                 # миграции БД

# API и уведомления
fastapi                 # REST API для дашборда
uvicorn                 # ASGI сервер
python-telegram-bot     # Telegram уведомления

# Утилиты
pydantic-settings       # конфиг из env-переменных
loguru                  # логирование
pytest / pytest-asyncio # тесты
```

---

## Структура проекта

```
polymarket-bot/
├── config/
│   └── settings.py             # Pydantic Settings, все конфиги из .env
├── ingestion/
│   ├── clob_client.py          # обёртка над Polymarket CLOB API
│   ├── graph_client.py         # GraphQL клиент для The Graph
│   ├── polygon_listener.py     # web3 on-chain события
│   └── ingestion_service.py    # координация, запись в БД
├── analysis/
│   ├── player_tracker.py       # расчёт метрик игроков
│   ├── cluster_detector.py     # DBSCAN кластеризация
│   └── behavior_scorer.py      # скоринг кластеров
├── signal/
│   ├── signal_engine.py        # фильтры, генерация сигналов
│   ├── risk_manager.py         # лимиты, стоп-лоссы
│   ├── exit_watcher.py         # слежение за выходом лидеров
│   └── portfolio_state.py      # управление открытыми позициями
├── execution/
│   └── trade_executor.py       # размещение ордеров CLOB
├── notifications/
│   └── telegram_bot.py         # Telegram уведомления
├── api/
│   └── dashboard.py            # FastAPI эндпоинты для Grafana
├── db/
│   ├── models.py               # SQLAlchemy модели
│   └── migrations/             # Alembic
├── tasks/
│   └── celery_app.py           # определение Celery задач
├── tests/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Ключевые риски и как их обработать в коде

### 1. Entry latency

Между входом лидеров и ботом проходит время. Логировать `entry_lag` на каждой сделке; если `entry_lag > max_entry_lag_min` — не входить.

```python
entry_lag = datetime.now() - cluster.first_leader_entry_time
if entry_lag.total_seconds() / 60 > settings.MAX_ENTRY_LAG_MIN:
    logger.warning(f"Skipping cluster {cluster.id}: lag {entry_lag} too high")
    return
```

### 2. Недостаточная ликвидность

Большой ордер двигает цену на маленьких рынках. Проверять глубину ордербука:

```python
orderbook = await clob.get_orderbook(token_id)
available_liquidity = sum(level.size for level in orderbook.asks[:5])
if available_liquidity < desired_size * 1.5:
    logger.warning("Insufficient liquidity, reducing position size")
    desired_size = available_liquidity * 0.5
```

### 3. Sybil-атаки (один человек — несколько кошельков)

Кластеризовать адреса по поведенческим паттернам: одинаковое время активности, похожие суммы, общие источники финансирования. Если несколько адресов в кластере — вероятно один игрок, уменьшать вес кластера.

### 4. Look-ahead bias в бэктесте

При бэктесте не использовать данные о разрешении рынка до момента имитируемого входа. Добавить параметр `simulation_timestamp` во все аналитические функции.

### 5. Rate limits API

Polymarket CLOB API имеет ограничения. Использовать `asyncio.Semaphore` для ограничения параллельных запросов + exponential backoff при 429.
