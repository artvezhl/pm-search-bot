# ТЗ: Модуль аналитики и копи-трейдинга Polymarket

> Документ предназначен для интеграции в существующий торговый бот.  
> Стек: Node.js (TypeScript) + PostgreSQL + Redis.  
> Все ссылки на внешние сервисы и контракты актуальны на апрель 2026.

---

## 1. Цель модуля

Автоматически выявлять кошельки на Polymarket, которые:

- входят в позиции **рано** (при низкой цене исхода, < 0.35)
- закрывают позиции **в прибыль** стабильно (win rate > 60%)
- возможно действуют **скоординированно** (сети кошельков)

После идентификации — копировать их сделки в реальном времени.

---

## 2. Какие рынки анализировать

### Приоритет 1 — Спортивные рынки (рекомендуется начать с них)

**Почему:** высокая ликвидность, чёткие даты начала/конца, повторяющиеся события (еженедельно), паттерны умных денег легче отслеживать.

- Football (UEFA Champions League, La Liga, EPL, Serie A)
- Basketball (NBA playoffs/regular season)
- American football (NFL)

**Фильтр при выборке:** `event_market_name LIKE '%Champions League%' OR '%NBA%' OR '%NFL%'`

### Приоритет 2 — Крипто-рынки

**Почему:** очень высокий объём, много активных участников, есть явные инсайдеры.

- "Will BTC exceed $X by date?"
- "Will ETH exceed $X by date?"

### Приоритет 3 — Макро/политика

Реже, но огромные объёмы. Оставить на 2-ю итерацию.

### Что НЕ брать на старте

- Рынки с объёмом < $10,000 — мало участников, шум
- Рынки длиннее 30 дней — слишком долгий цикл для копи-трейдинга

---

## 3. Исторический интервал


| Параметр                               | Значение                   | Обоснование                                                                     |
| -------------------------------------- | -------------------------- | ------------------------------------------------------------------------------- |
| Глубина истории для первичного анализа | **6 месяцев**              | Минимум для статистической значимости                                           |
| Минимум завершённых сделок на кошелёк  | **15 рынков**              | Меньше — не отличить скилл от удачи                                             |
| Период обновления статистики кошельков | **каждые 6 часов**         | Баланс между актуальностью и нагрузкой                                          |
| Хранение сырых трейдов                 | **6 месяцев**, потом архив | Нужны для пересчёта метрик                                                      |
| Окно для обнаружения сети кошельков    | **5 минут**                | Если два кошелька входят в одну позицию с разницей < 5 мин — потенциальная сеть |


---

## 4. Источники данных

### Рекомендуемая гибридная схема

```
Bootstrap (исторические данные, разово):
  └─ Alchemy free tier → eth_getLogs → NegRisk Exchange
     → все OrderFilled за всё время (пагинация по 2000 блоков)

Real-time (постоянно):
  └─ Polymarket CLOB API → /trades (polling каждые 30 сек)
     + WebSocket на Alchemy → подписка на новые OrderFilled события

Метаданные рынков:
  └─ Polymarket Gamma API → /markets, /events (бесплатно, без ключа)
```

### Почему НЕ только блокчейн

Прямой on-chain трекинг через ethers.js — максимально точно, но:

- Нужен архивный RPC-узел (~$50+/мес на Alchemy)
- Медленно при первоначальной загрузке 6 месяцев истории
- CLOB API уже отдаёт `maker`/`taker` адреса — дублировать смысла нет

### Почему НЕ только Dune

- Обновление данных раз в 24 часа — не подходит для real-time копирования

### Итог: Dune для истории + CLOB API для real-time

---

## 5. Архитектура системы

```
┌─────────────────────────────────────────────────────────┐
│                    POLYMARKET MODULE                    │
│                                                         │
│  ┌──────────────┐    ┌──────────────┐                  │
│  │HistoryLoader │    │  TradePoller │                  │
│  │ (Dune API)   │    │ (CLOB API)   │                  │
│  │ разово/6мес  │    │  каждые 30с  │                  │
│  └──────┬───────┘    └──────┬───────┘                  │
│         │                   │                           │
│         └─────────┬─────────┘                          │
│                   ▼                                     │
│          ┌────────────────┐                             │
│          │  TradeIngester │  ← нормализует, дедуплицир. │
│          └────────┬───────┘                             │
│                   │                                     │
│         ┌─────────┴──────────┐                         │
│         ▼                    ▼                          │
│  ┌─────────────┐    ┌────────────────┐                 │
│  │  PostgreSQL │    │  WalletAnalyzer│                 │
│  │  (trades,   │    │  (scoring,     │                 │
│  │   wallets,  │    │   networks)    │                 │
│  │   markets)  │    └───────┬────────┘                 │
│  └─────────────┘            │                          │
│                             ▼                           │
│                   ┌──────────────────┐                 │
│                   │  SignalGenerator  │                 │
│                   │  (умный кошелёк  │                 │
│                   │  открыл позицию) │                 │
│                   └────────┬─────────┘                 │
│                            │                            │
│                   ┌────────▼─────────┐                 │
│                   │  Redis Queue     │                  │
│                   │  (copy_signals)  │                  │
│                   └────────┬─────────┘                 │
└────────────────────────────┼────────────────────────────┘
                             │
                    ┌────────▼─────────┐
                    │  Основной бот    │
                    │  (CopyExecutor)  │
                    │  → выставляет    │
                    │    ордер на PM   │
                    └──────────────────┘
```

---

## 6. База данных — схема

```sql
-- Рынки Polymarket
CREATE TABLE pm_markets (
    condition_id        VARCHAR(66) PRIMARY KEY,
    event_market_name   TEXT,
    question            TEXT,
    token_outcome       VARCHAR(10),   -- YES / NO
    asset_id            NUMERIC,
    neg_risk            BOOLEAN,
    polymarket_link     TEXT,
    resolved            BOOLEAN DEFAULT FALSE,
    resolution_value    DECIMAL(10,4), -- 1.0 или 0.0 после резолюции
    close_time          TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Сырые трейды (хранить 6 месяцев)
CREATE TABLE pm_trades (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             VARCHAR(66) NOT NULL,
    block_time          TIMESTAMP NOT NULL,
    condition_id        VARCHAR(66) REFERENCES pm_markets(condition_id),
    token_outcome       VARCHAR(10),
    maker_address       VARCHAR(42) NOT NULL,
    taker_address       VARCHAR(42),
    price               DECIMAL(10,6) NOT NULL,  -- 0.0 to 1.0
    amount_usdc         DECIMAL(18,6) NOT NULL,
    shares              DECIMAL(18,6),
    action              VARCHAR(20),             -- CLOB / MINT / MERGE
    raw_data            JSONB,
    ingested_at         TIMESTAMP DEFAULT NOW(),
    UNIQUE(tx_hash, condition_id, maker_address)
);

CREATE INDEX idx_pm_trades_maker    ON pm_trades(maker_address, block_time DESC);
CREATE INDEX idx_pm_trades_market   ON pm_trades(condition_id, block_time DESC);
CREATE INDEX idx_pm_trades_time     ON pm_trades(block_time DESC);

-- Статистика кошельков (агрегированная, обновляется каждые 6 часов)
CREATE TABLE pm_wallets (
    address             VARCHAR(42) PRIMARY KEY,
    -- Основные метрики
    total_markets       INTEGER DEFAULT 0,       -- кол-во рынков где участвовал
    resolved_markets    INTEGER DEFAULT 0,       -- из них завершённых
    winning_markets     INTEGER DEFAULT 0,       -- выиграл (исход совпал)
    win_rate            DECIMAL(5,4),            -- winning / resolved
    -- Финансовые метрики
    total_volume_usdc   DECIMAL(18,2) DEFAULT 0,
    total_pnl_usdc      DECIMAL(18,2) DEFAULT 0,
    avg_roi             DECIMAL(8,4),            -- средний ROI на сделку
    -- Тайминг (ключевой показатель для "умного кошелька")
    avg_entry_price     DECIMAL(6,4),            -- чем ниже — тем раньше вошёл
    avg_entry_days_before_close DECIMAL(8,2),    -- среднее дней до закрытия
    -- Активность
    first_trade_at      TIMESTAMP,
    last_trade_at       TIMESTAMP,
    active_last_30d     BOOLEAN DEFAULT FALSE,
    -- Скоринг
    smart_score         DECIMAL(6,4),            -- итоговый балл 0-1
    is_smart_wallet     BOOLEAN DEFAULT FALSE,   -- прошёл порог
    is_blacklisted      BOOLEAN DEFAULT FALSE,   -- маркет-мейкер / бот-шум
    -- Служебное
    stats_updated_at    TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_pm_wallets_score   ON pm_wallets(smart_score DESC) WHERE is_smart_wallet = TRUE;
CREATE INDEX idx_pm_wallets_active  ON pm_wallets(last_trade_at DESC);

-- Сети кошельков (кошельки, торгующие скоординированно)
CREATE TABLE pm_wallet_networks (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_a            VARCHAR(42) NOT NULL,
    wallet_b            VARCHAR(42) NOT NULL,
    co_trade_count      INTEGER DEFAULT 0,       -- сколько раз торговали одновременно
    correlation_score   DECIMAL(5,4),            -- 0-1, насколько скоординированы
    last_co_trade_at    TIMESTAMP,
    UNIQUE(wallet_a, wallet_b)
);

-- Сигналы для копирования
CREATE TABLE pm_copy_signals (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMP DEFAULT NOW(),
    -- Источник сигнала
    trigger_wallet      VARCHAR(42) NOT NULL,
    trigger_tx_hash     VARCHAR(66),
    trigger_trade_id    BIGINT REFERENCES pm_trades(id),
    -- Что копировать
    condition_id        VARCHAR(66) REFERENCES pm_markets(condition_id),
    token_outcome       VARCHAR(10),
    action              VARCHAR(10),             -- BUY / SELL
    suggested_price     DECIMAL(10,6),
    suggested_amount    DECIMAL(18,2),           -- в USDC
    -- Исполнение
    status              VARCHAR(20) DEFAULT 'PENDING',  -- PENDING/EXECUTED/SKIPPED/FAILED
    executed_at         TIMESTAMP,
    execution_tx_hash   VARCHAR(66),
    execution_price     DECIMAL(10,6),
    execution_amount    DECIMAL(18,2),
    skip_reason         TEXT,
    -- P&L сигнала (заполняется после резолюции)
    pnl_usdc            DECIMAL(18,2),
    pnl_pct             DECIMAL(8,4)
);

CREATE INDEX idx_signals_status ON pm_copy_signals(status, created_at DESC);
CREATE INDEX idx_signals_wallet ON pm_copy_signals(trigger_wallet, created_at DESC);
```

---

## 7. Сервисы — детальное описание

### 7.1 HistoryLoader (разовый bootstrap)

**Файл:** `src/polymarket/history-loader.ts`

**Задача:** загрузить 6 месяцев трейдов через Dune Analytics API и наполнить `pm_trades` и `pm_markets`.

**Алгоритм:**

```typescript
// Dune API endpoint
const DUNE_QUERY = `
  SELECT
    cast(maker AS VARCHAR)     AS maker_address,
    cast(taker AS VARCHAR)     AS taker_address,
    cast(condition_id AS VARCHAR) AS condition_id,
    event_market_name,
    question,
    token_outcome,
    neg_risk,
    asset_id,
    price,
    amount,
    shares,
    block_time,
    cast(tx_hash AS VARCHAR)   AS tx_hash,
    action
  FROM polymarket_polygon.market_trades
  WHERE block_time >= NOW() - INTERVAL '180' DAY
    AND (
      event_market_name LIKE '%Champions League%'
      OR event_market_name LIKE '%NBA%'
      OR event_market_name LIKE '%NFL%'
      OR event_market_name LIKE '%Bitcoin%'
      OR event_market_name LIKE '%Ethereum%'
      OR event_market_name LIKE '%Premier League%'
    )
  ORDER BY block_time DESC
`;

// POST /api/1/query/execute → poll /api/1/execution/{id}/results
// Paginate by 50,000 rows
```

**Параметры запуска:** `npm run history:load` (запускается один раз при первом деплое, затем не нужен — данные приходят через TradePoller)

---

### 7.2 TradePoller (real-time)

**Файл:** `src/polymarket/trade-poller.ts`

**Задача:** каждые 30 секунд запрашивать новые трейды по отслеживаемым рынкам.

```typescript
const CLOB_BASE = "https://clob.polymarket.com";

// Интерфейс трейда из CLOB API
interface ClobTrade {
  market: string; // condition_id
  outcome: string; // YES / NO
  price: string;
  size: string;
  side: "BUY" | "SELL";
  maker_address: string;
  taker_address: string;
  transaction_hash: string;
  timestamp: number; // unix ms
  trade_id: string;
}

// Запрос: GET /trades?market={conditionId}&after={lastTimestamp}
// Ответ: { data: ClobTrade[], next_cursor: string }

// Алгоритм:
// 1. Загрузить список активных рынков из pm_markets (resolved = FALSE)
// 2. Для каждого рынка — запросить /trades?market=... после последнего известного трейда
// 3. Сохранить новые трейды в pm_trades
// 4. Вызвать SignalChecker для каждого нового трейда
// 5. Повторить через 30 секунд
```

**Ограничение:** не более 5 параллельных запросов к CLOB API (rate limit ~10 req/sec).

---

### 7.3 MarketSync

**Файл:** `src/polymarket/market-sync.ts`

**Задача:** каждые 2 часа обновлять список активных рынков и их статус.

```typescript
// GET https://gamma-api.polymarket.com/markets?active=true&limit=100
// Фильтрация по тегам: sports, crypto
// Сохранить новые рынки в pm_markets
// Обновить resolved=TRUE для закрытых рынков
// Заполнить resolution_value (1.0 или 0.0)
```

---

### 7.4 WalletAnalyzer

**Файл:** `src/polymarket/wallet-analyzer.ts`

**Задача:** каждые 6 часов пересчитывать метрики всех кошельков и обновлять `pm_wallets`.

#### Алгоритм scoring (smart_score)

```typescript
interface WalletMetrics {
  win_rate: number; // 0-1
  avg_entry_price: number; // 0-1 (чем ниже, тем лучше)
  avg_roi: number; // может быть > 1
  total_volume_usdc: number;
  resolved_markets: number;
  active_last_30d: boolean;
}

function calculateSmartScore(m: WalletMetrics): number {
  // Фильтры-отсечки (если не проходит — score = 0)
  if (m.resolved_markets < 15) return 0;
  if (m.win_rate < 0.55) return 0;
  if (m.total_volume_usdc < 500) return 0; // отсеиваем мусор
  if (!m.active_last_30d) return 0; // неактивные не интересны

  // Взвешенная сумма компонентов (все нормализованы 0-1)
  const winRateScore = Math.min((m.win_rate - 0.55) / 0.35, 1); // 0.55→0, 0.90→1
  const entryScore = Math.max(1 - m.avg_entry_price / 0.45, 0); // 0.0→1, 0.45→0
  const roiScore = Math.min(m.avg_roi / 2.0, 1); // 200%→1
  const volumeScore = Math.min(Math.log10(m.total_volume_usdc / 500) / 3, 1); // log scale

  const score =
    winRateScore * 0.4 + // главный показатель
    entryScore * 0.3 + // ранний вход — ключевой паттерн
    roiScore * 0.2 +
    volumeScore * 0.1;

  return Math.round(score * 10000) / 10000; // 4 знака
}

// Порог для is_smart_wallet:
const SMART_WALLET_THRESHOLD = 0.55;
```

#### Расчёт P&L для завершённых рынков

```typescript
// Для каждого кошелька, для каждого завершённого рынка:
// PNL = (resolved_value - avg_buy_price) * total_shares - fees
// avg_buy_price = SUM(price * shares) / SUM(shares)
// resolved_value = 1.0 (выиграл) или 0.0 (проиграл)
```

---

### 7.5 NetworkDetector

**Файл:** `src/polymarket/network-detector.ts`

**Задача:** находить кошельки, торгующие скоординированно (потенциальные сети).

```typescript
// Алгоритм:
// 1. Для каждого рынка, найти все трейды за последние 5 минут
// 2. Если два кошелька купили ОДИН исход в пределах 5 минут:
//    → увеличить co_trade_count в pm_wallet_networks
// 3. correlation_score = co_trade_count / min(trades_A, trades_B)
// 4. Если correlation_score > 0.5 И co_trade_count > 5:
//    → считать кошельки связанными

// Запрос для обнаружения:
const NETWORK_QUERY = `
  SELECT a.maker_address AS wallet_a, b.maker_address AS wallet_b,
         COUNT(*) AS co_trades
  FROM pm_trades a
  JOIN pm_trades b ON
    a.condition_id = b.condition_id
    AND a.token_outcome = b.token_outcome
    AND a.maker_address < b.maker_address  -- избегаем дубликатов
    AND ABS(EXTRACT(EPOCH FROM (a.block_time - b.block_time))) < 300  -- 5 минут
  WHERE a.block_time >= NOW() - INTERVAL '30 days'
  GROUP BY wallet_a, wallet_b
  HAVING COUNT(*) >= 3
`;
```

---

### 7.6 SignalChecker (real-time, вызывается из TradePoller)

**Файл:** `src/polymarket/signal-checker.ts`

**Задача:** при каждом новом трейде проверить — нужно ли копировать.

```typescript
async function checkAndEmitSignal(trade: NormalizedTrade): Promise<void> {
  // 1. Проверить: is maker_address в pm_wallets WHERE is_smart_wallet = TRUE?
  const wallet = await getSmartWallet(trade.maker_address);
  if (!wallet) return;

  // 2. Проверить фильтры безопасности:
  const market = await getMarket(trade.condition_id);
  if (!market || market.resolved) return;

  // Не входить если цена уже > 0.75 (поздно, риск высокий)
  if (parseFloat(trade.price) > 0.75) return;

  // Не входить если до закрытия < 2 часов
  const hoursToClose = (market.close_time.getTime() - Date.now()) / 3600000;
  if (hoursToClose < 2) return;

  // 3. Рассчитать размер позиции (пропорционально уверенности бота)
  const suggestedAmount = calculateCopyAmount(
    wallet.smart_score,
    trade.amount_usdc
  );

  // 4. Создать сигнал в pm_copy_signals
  await createSignal({
    trigger_wallet: trade.maker_address,
    trigger_tx_hash: trade.tx_hash,
    condition_id: trade.condition_id,
    token_outcome: trade.token_outcome,
    action: "BUY",
    suggested_price: parseFloat(trade.price) + 0.01, // небольшой slippage
    suggested_amount: suggestedAmount,
    status: "PENDING",
  });

  // 5. Опубликовать в Redis для немедленного исполнения
  await redis.lpush("polymarket:signals", JSON.stringify(signal));
}

function calculateCopyAmount(
  smartScore: number,
  triggerAmount: number
): number {
  // Базовый размер: 5% от доступного капитала бота
  // Масштабирование по smart_score кошелька
  const BASE_ALLOCATION = 0.05; // 5% капитала
  const scaleFactor = 0.5 + smartScore; // 0.55-1.55x при score 0.05-0.55+
  return Math.min(
    BOT_CAPITAL * BASE_ALLOCATION * scaleFactor,
    50 // максимум $50 на одну копию (hardcap на старте)
  );
}
```

---

### 7.7 CopyExecutor (интеграция с основным ботом)

**Файл:** `src/polymarket/copy-executor.ts`

**Задача:** читать сигналы из Redis и выставлять ордера через Polymarket CLOB API.

```typescript
// Подписаться на Redis queue: BLPOP polymarket:signals 0
// Для каждого сигнала:
//   1. Проверить — позиция ещё открыта? (не истекла, не разрезолвилась)
//   2. Получить текущую цену через GET /book?token_id={assetId}
//   3. Если цена не ушла более чем на 0.05 от suggested_price — выставить ордер
//   4. Обновить pm_copy_signals.status = 'EXECUTED'/'SKIPPED'

// API для выставления ордера:
// POST https://clob.polymarket.com/order
// Требует подписи через private key (L2 key Polymarket)
// Используется библиотека @polymarket/clob-client
```

---

## 8. Технический стек

```
Runtime:        Node.js 20 LTS + TypeScript 5
БД основная:    PostgreSQL 16 (TimescaleDB extension — опционально, для time-series)
Кэш/очереди:   Redis 7
Scheduler:      node-cron (встроенный, без внешних зависимостей)
HTTP клиент:    axios или fetch (native)
ORM / Query:    Kysely (type-safe SQL builder, без magic)
Blockchain:     viem (если понадобится прямой on-chain доступ)
Polymarket SDK: @polymarket/clob-client (официальный)
Логирование:    pino (structured JSON logs)
Переменные:     dotenv
```

### Почему Kysely вместо Prisma?

Kysely даёт полный контроль над SQL (важно для сложных аналитических запросов), при этом остаётся полностью типобезопасным. Prisma сложно использовать для window functions, CTE, и сложных агрегатов, которые нужны для скоринга кошельков.

---

## 9. Структура файлов

```
src/
  polymarket/
    ├── types.ts                  # интерфейсы Trade, Wallet, Signal, Market
    ├── config.ts                 # все константы и пороги
    ├── db/
    │   ├── schema.sql            # DDL (из раздела 6)
    │   └── queries.ts            # все SQL через Kysely
    ├── services/
    │   ├── history-loader.ts     # bootstrap из Dune
    │   ├── trade-poller.ts       # real-time CLOB polling
    │   ├── market-sync.ts        # обновление рынков
    │   ├── wallet-analyzer.ts    # скоринг кошельков
    │   ├── network-detector.ts   # обнаружение сетей
    │   ├── signal-checker.ts     # генерация сигналов
    │   └── copy-executor.ts      # исполнение копий
    ├── api/
    │   ├── clob.ts               # обёртка над CLOB API
    │   ├── gamma.ts              # обёртка над Gamma API
    │   └── dune.ts               # обёртка над Dune API
    └── index.ts                  # запуск всех сервисов + cron
```

---

## 10. Переменные окружения

```env
# База данных
DATABASE_URL=postgresql://user:pass@localhost:5432/bot_db

# Redis
REDIS_URL=redis://localhost:6379

# Dune Analytics (для bootstrap)
DUNE_API_KEY=your_dune_api_key

# Polymarket (для исполнения)
POLYMARKET_PRIVATE_KEY=0x...          # приватный ключ L2 (не Ethereum key!)
POLYMARKET_PROXY_ADDRESS=0x...        # адрес прокси кошелька на Polymarket

# Alchemy (опционально, только для прямого on-chain)
ALCHEMY_POLYGON_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Настройки бота
BOT_CAPITAL_USDC=1000                  # общий капитал модуля в USDC
MAX_SINGLE_POSITION_USDC=50            # максимум на одну копию
SMART_WALLET_THRESHOLD=0.55            # порог для is_smart_wallet
```

---

## 11. Расписание (cron)

```typescript
// trade-poller.ts       — каждые 30 секунд (setInterval)
// market-sync.ts        — каждые 2 часа     (0 */2 * * *)
// wallet-analyzer.ts    — каждые 6 часов    (0 */6 * * *)
// network-detector.ts   — каждые 12 часов   (0 */12 * * *)
// cleanup старых трейдов (> 6 мес) — раз в неделю (0 3 * * 0)
```

---

## 12. Фазы реализации

### Фаза 1 — Данные и хранилище (неделя 1)

- Создать схему БД (`schema.sql`)
- Реализовать `api/clob.ts`, `api/gamma.ts`, `api/dune.ts`
- Реализовать `history-loader.ts` (запустить bootstrap)
- Реализовать `market-sync.ts`
- Реализовать `trade-poller.ts`

### Фаза 2 — Аналитика (неделя 2)

- Реализовать `wallet-analyzer.ts` со scoring
- Реализовать `network-detector.ts`
- Запустить первый прогон аналитики, проверить топ кошельков вручную
- Откалибровать пороги (SMART_WALLET_THRESHOLD, минимальные метрики)

### Фаза 3 — Копирование (неделя 3)

- Реализовать `signal-checker.ts`
- Реализовать `copy-executor.ts`
- Первые 2 недели — режим paper trading (сигналы пишутся в БД, но реальных ордеров нет)
- После валидации — включить реальное исполнение с MAX_SINGLE_POSITION_USDC=10

### Фаза 4 — Мониторинг (неделя 4+)

- Дашборд: топ умных кошельков, последние сигналы, P&L копий
- Алерты в Telegram: новый сигнал / провал исполнения / кошелёк резко упал по win_rate
- A/B тест: копировать только топ-10 кошельков vs топ-50

---

## 13. Важные ограничения и риски


| Риск                                     | Митигация                                                      |
| ---------------------------------------- | -------------------------------------------------------------- |
| Polymarket CLOB API rate limit           | Пул с максимум 5 параллельных запросов, exponential backoff    |
| Кошелёк умного трейдера меняется (Sybil) | Отслеживать прокси-адреса Polymarket, а не EOA напрямую        |
| Проскальзывание при копировании          | Лимит: входить только если цена не изменилась > 0.05           |
| Маркет-мейкеры в "умных кошельках"       | Blacklist: если у кошелька > 200 трейдов/мес — скорее всего MM |
| Рынок закрывается до исполнения          | Проверять close_time перед выставлением ордера                 |
| Потеря данных при перезапуске            | Хранить cursor последнего обработанного трейда в Redis         |


---

## 14. API Reference (ключевые эндпоинты)

```
Polymarket CLOB API (real-time трейды):
  GET  https://clob.polymarket.com/trades?market={conditionId}&limit=500
  GET  https://clob.polymarket.com/book?token_id={tokenId}
  GET  https://clob.polymarket.com/orders?maker_address={address}
  POST https://clob.polymarket.com/order   ← требует подписи

Polymarket Gamma API (метаданные):
  GET  https://gamma-api.polymarket.com/markets?active=true&limit=100
  GET  https://gamma-api.polymarket.com/events/slug/{slug}
  GET  https://gamma-api.polymarket.com/positions?user={address}

Dune Analytics API (исторические данные):
  POST https://api.dune.com/api/v1/query/execute
  GET  https://api.dune.com/api/v1/execution/{id}/results?limit=50000
  Headers: X-Dune-API-Key: {DUNE_API_KEY}

Контракты Polymarket на Polygon (для прямого on-chain):
  CTF Token:    0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  CTF Exchange: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
  NegRisk Exc:  0xC5d563A36AE78145C45a50134d48A1215220f80a
  USDC:         0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
```

