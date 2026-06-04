# Crypto Tracker

Real-time cross-exchange price tracking, daily/weekly turnover analytics,
funding-rate arbitrage and new-listing surveillance across **Binance, OKX,
Bybit, MEXC, Hyperliquid** + MOEX FORTS turnover data integration.

The app title (window/tab and FastAPI metadata) is "Crypto Tracker".  The
package was previously named "arbi-tracker"; the rename is reflected in
`frontend/package.json`, `frontend/index.html`, `backend/app/main.py`.

## Architecture

```
frontend (React/Vite)  ←websocket→  backend (FastAPI)
                                          ↕ pub/sub
                                       Redis
                                          ↕
                                     collector (ccxt/ws)
                                          ↕
                                    TimescaleDB
```

- **collector** (`backend/worker/main.py`) — AsyncIO process that connects to exchange WebSockets via ccxt, publishes ticks to Redis channel `prices:{symbol}`
- **backend** (`backend/app/main.py`) — FastAPI app, subscribes to Redis, serves REST + WebSocket to frontend, writes price history to TimescaleDB
- **arbitrage detector** (`backend/app/arbitrage/detector.py`) — compares latest prices across exchanges, emits alerts when spread > threshold
- **MOEX ETL** (`backend/app/moex/etl.py`) — background loop that fetches USDRUBF rates and FORTS turnover per asset from ISS
- **frontend** (`frontend/src/`) — React + Vite, TradingView Lightweight Charts + Plotly stacked bars, WebSocket-driven price/alert feeds

## Local dev

```bash
cp .env.example .env          # fill in API keys (read-only recommended)
docker compose up --build     # starts all services
```

Services:
| Service     | Port  |
|-------------|-------|
| frontend    | 5173  |
| backend API | 8000  |
| Redis       | 6379  |
| TimescaleDB | 5432  |

Backend docs: http://localhost:8000/docs

## Deployment (AWS Lightsail)

```bash
docker compose -f docker-compose.prod.yml up -d
```

Nginx terminates TLS and reverse-proxies to backend:8000 and frontend:80. See `nginx/lightsail.conf` — replace `YOUR_DOMAIN` before deploy.

## Key files

| Path | Purpose |
|------|---------|
| `backend/app/config.py` | All env-var settings via Pydantic |
| `backend/app/exchanges.py` | Single source of truth for exchange ccxt classes / perp types |
| `backend/app/collector/base.py` | Base WebSocket collector |
| `backend/app/arbitrage/detector.py` | Spread detection logic |
| `backend/app/db/timescale.py` | asyncpg pool + all SQL queries |
| `backend/app/db/migrations/001_init.sql` | TimescaleDB schema |
| `backend/app/moex/{config,fetcher,etl,calendar}.py` | MOEX FORTS integration |
| `backend/app/api/routes/launches.py` | Hourly cache of non-crypto perp listings |
| `frontend/src/components/Chart.tsx` | TradingView chart wrapper |
| `frontend/src/components/SectionHeading.tsx` | Shared section divider |
| `frontend/src/components/ExchangeSourceBadges.tsx` | "Data from …" strip (Analytics / DailyVolume / Launches) |
| `frontend/src/utils/format.ts` | daysAgo / timeAgo / fmtVolume helpers |
| `frontend/src/hooks/useWebSocket.ts` | Reconnecting WebSocket with exponential backoff (1s → 30s) |
| `frontend/src/pages/Analytics.tsx` | Weekly ADTV stacked-bar charts |
| `frontend/src/pages/DailyVolume.tsx` | Daily volume stacked-bar charts (30d) |
| `frontend/src/pages/Launches.tsx` | Non-crypto perp futures listings page |

## Exchange list pattern

Two arrays live in `frontend/src/types/index.ts`:

```ts
EXCHANGES         = ['binance', 'okx', 'bybit', 'mexc', 'hyperliquid']
VOLUME_EXCHANGES  = [...EXCHANGES, 'moex']
```

Rule:
- **EXCHANGES** — pages that touch real-time prices, OHLCV history, or
  exchange connection stats (Realtime Prices, Historical, Exchanges,
  Instruments).  MOEX is a data-only source (no WebSocket / no fetch_ohlcv),
  so it does NOT belong here.
- **VOLUME_EXCHANGES** — Weekly Performance and Daily Volume, where MOEX
  FORTS turnover is stacked alongside crypto volumes.

When adding a new exchange that streams prices, append it to `EXCHANGES`.
When adding a new MOEX-style "turnover only" data source, append to
`VOLUME_EXCHANGES` and update the relevant DB queries.

## Environment variables

See `.env.example` for full list. Minimum required:
- `DATABASE_URL` — TimescaleDB connection string
- `REDIS_URL` — Redis URL
- Exchange API keys are optional for public ticker feeds (ccxt uses public endpoints for prices)

## Arbitrage threshold

Default spread threshold: **0.3%** — edit `ARBI_THRESHOLD_PCT` in `.env`.

## Adding a new exchange

1. Add collector in `backend/app/collector/<exchange>.py` extending `BaseCollector`
2. Register it in `backend/worker/main.py` (`COLLECTORS` dict)
3. Add the ccxt class + perp type to `backend/app/exchanges.py`
4. Add exchange name to `EXCHANGES` list in `backend/app/config.py`
5. Add color + label to `frontend/src/types/index.ts` (`EXCHANGE_COLORS`, `EXCHANGES`,
   `EXCHANGE_LABEL` inside `ExchangeSourceBadges.tsx`)

## Futures Launches page

Hourly cached scan of `load_markets()` on every crypto exchange,
filtered to a hard-coded `NON_CRYPTO_BASES` allow-list (commodities,
metals, indices, US stocks).  Listing date is read from exchange-specific
metadata fields: `onboardDate` (Binance), `listTime` (OKX), `launchTime`
(Bybit), `createTime` (MEXC).  Hyperliquid does not expose a reliable
listing date — its rows are shown without dates.

Two distinct "new" sections:

1. **New Products** — the EARLIEST listed_at across every exchange is
   within 7 days AND we have no older OHLCV data for that base.  This is
   a brand-new contract on the market (e.g. DELL/IBM on OKX).
2. **New on Exchange** — flat list of (base × exchange) pairs where a
   single exchange picked up an existing instrument within 7 days
   (e.g. XAU on MEXC, but Binance has had XAU for a year).

A group can appear in at most one section.

## Known gotchas

- **MEXC daily klines return placeholder future bars** out to year 2063
  for some symbols (e.g. `UKOIL_USDT`).  The OHLCV backfill in
  `backend/app/backfill/ohlcv.py` MUST skip bars with `ts_ms > now_ms`
  AND stop pagination when a page contains any future bar — otherwise
  tens of thousands of bogus rows hit the hypertable and explode chunk
  count.
- **TimescaleDB chunk locking**: queries on `ohlcv_daily` without an
  upper time bound lock every chunk in the hypertable, hitting
  `max_locks_per_transaction` (default 64) → "out of shared memory" →
  500.  All history aggregate queries (`fetch_history_metrics`,
  `fetch_history_metrics_by_exchange`, `fetch_weekly_adtv_rub`) include
  `ts < CURRENT_DATE + INTERVAL '1 day'` so the planner prunes to chunks
  that actually contain data.
- **FX conversion**: crypto volumes are joined to `moex_fx_rates` via
  `LEFT JOIN LATERAL` (most recent date ≤ ohlcv day) — NOT a plain
  `INNER JOIN`, which would drop crypto volume on every weekend/holiday
  / ISS-outage day.
- **nginx upstream DNS cache**: nginx resolves upstream container IPs
  once at startup and caches them. After `docker compose up -d backend`
  the backend container gets a new IP but nginx keeps hitting the old one
  → 502. Fixed in `nginx/http-only.conf` via `resolver 127.0.0.11
  valid=10s` + `set $backend_upstream "backend:8000"` so nginx
  re-resolves every 10 s. Always restart nginx after recreating any
  upstream container: `docker compose -f docker-compose.prod.yml restart
  nginx`.
- **MEXC WebSocket requires protobuf**: ccxt ≥ 4.4 switched MEXC WS
  messages to protobuf encoding. `protobuf==5.29.5` must be in
  `backend/requirements.txt` — without it the collector logs
  `NotSupported: mexc requires protobuf` on every tick and MEXC
  real-time prices are silently dropped.
- **Hyperliquid aiohttp "Unclosed session" warnings**: ccxt hyperliquid
  creates lazy internal aiohttp sessions that emit `__del__` warnings
  when GC'd, even though FDs are released correctly. Suppressed via a
  `logging.Filter` on `ccxt.base.exchange` and `asyncio` loggers in
  `backend/app/main.py` (`_CcxtNoiseFilter`). Not a real leak (verified
  via `/proc/1/fd` count).
- **WTI symbol names differ per exchange**: canonical is
  `WTI/USDT:USDT`; each exchange uses a different ccxt symbol. Aliases
  stored in `instruments.aliases` JSONB column:
  `binance/okx/bybit → CL/USDT:USDT`, `mexc → USOIL/USDT:USDT`,
  `hyperliquid → XYZ-CL/USDC:USDC` (launched 2026-01-06, id 110029).
  WTI is NOT on MOEX FORTS (no CL contract there).
## MOEX Integration (завершено 31.05.2026)

Данные MOEX FORTS встроены в страницу Weekly Performance как отдельный
сегмент `moex` (красный) в stacked-bar диаграммах. Только обороты, не цены.
Вся страница в рублях.

### Инструменты (внутренний код → ISS ASSETCODE → canonical symbol)
- BR → `BR`   → BRN/USDT:USDT  (Brent)
- NG → `NG`   → NATGAS/USDT:USDT (газ)
- GD → `GOLD` → XAU/USDT:USDT  (золото)
- SV → `SILV` → XAG/USDT:USDT  (серебро)
- PT → `PLT`  → XPT/USDT:USDT  (платина)
- PD → `PLD`  → XPD/USDT:USDT  (палладий)

Маппинг в `backend/app/moex/config.py`: `ASSET_ISS_CODE`, `ASSET_TO_CANONICAL`.

### Методология ADTV
- Поле VALUE (рубли) из ISS history-эндпоинта
- SECID'ы определяются динамически через `_discover_secids_for_assetcode()`:
  сэмплирует ISS market-level endpoint (`date=`) на начало каждого месяца
  + последние 5 дней → собирает все SECID включая истёкшие контракты
- Спреды (len > len(assetcode)+2) отсекаются автоматически
- Затем история тянется по каждому SECID отдельно и суммируется

### Конвертация крипты в рубли
- Подневно: `quote_volume_USDT × moex_fx_rates.usdrub` за тот же день
- USDRUBF — вечный фьючерс (SECID=USDRUBF), курс forward-filled на выходные

### Фронтенд (Analytics.tsx / DailyVolume.tsx)
- Ось Y: всегда миллиарды (₽B), 1 знак после запятой. Авто-масштаб в миллионы убран.
- `automargin: true` + `standoff: 14` на yaxis предотвращают наложение заголовка `Volume (₽B)` на числа тиков.
- MOEX сегмент скрыт для секций `US Market` и `Spot Crypto`
- X-ось: диапазон недели `May 18 – May 24` вместо одной даты
- Hover: `₽83.2B`

### Ключевые файлы
| Путь | Назначение |
|------|-----------|
| `backend/app/moex/config.py` | ASSET_ISS_CODE, ASSET_TO_CANONICAL |
| `backend/app/moex/fetcher.py` | ISS HTTP-клиент, dynamic SECID discovery |
| `backend/app/moex/etl.py` | Планировщик ETL, upsert в БД |
| `backend/app/db/timescale.py` | fetch_weekly_adtv_rub() |
| `backend/app/api/routes/history.py` | GET /api/history/weekly-adtv |
| `frontend/src/pages/Analytics.tsx` | Stacked-bar диаграммы |

### Статус (на 04.06.2026)
- ✅ ETL с динамическим discovery (нет хардкода серий)
- ✅ Таблицы moex_fx_rates, moex_daily_value заполнены (117 торг. дней с 01.12.2025)
- ✅ /weekly-adtv возвращает RUB-данные с MOEX как exchange='moex'
- ✅ Analytics.tsx: рубли, всегда ₽B (1 знак), диапазоны на оси X
- ✅ DailyVolume.tsx добавлен — за последние 30 дней
- ✅ MOEX исключён со страниц Realtime Prices / Exchanges / Historical /
  Instruments через разделение EXCHANGES vs VOLUME_EXCHANGES
- ✅ ETL интервал снижен с 24h до 6h (`asyncio.sleep(21_600)`)
- ✅ WTI/USDT:USDT добавлен как инструмент (MOEX FORTS WTI не существует)