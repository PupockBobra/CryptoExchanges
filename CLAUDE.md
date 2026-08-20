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
| `backend/app/spb/{config,fetcher,etl}.py` | SPB Exchange perp turnover via Finam TradeAPI |
| `backend/app/spb/{spb_api,oi_etl}.py` | SPB Exchange open interest via the exchange's own public API (no token) |
| `backend/app/spb/orderbook.py` | Live order-book cache + lazy background poller (Finam) |
| `backend/app/api/routes/spb.py` | SPB REST endpoints (daily-volume / weekly-adtv / open-interest / orderbook / refresh / oi-refresh) |
| `frontend/src/pages/SPBOrderBook.tsx` | Live стакан page (all 25 perps, terminal-style flash) |
| `backend/app/stocks/{config,etl}.py` | Equity (stock) perps on crypto exchanges — classification + volume ETL |
| `backend/app/api/routes/stocks.py` | Stock REST endpoints (`/volume?period=daily\|weekly`, `/refresh`) |
| `backend/app/api/routes/launches.py` | Hourly cache of non-crypto perp listings |
| `backend/app/api/routes/reports.py` | Custom Report endpoints (`/tree`, `/options`, `/data`) |
| `frontend/src/components/Chart.tsx` | TradingView chart wrapper |
| `frontend/src/components/SectionHeading.tsx` | Shared section divider |
| `frontend/src/components/ExchangeSourceBadges.tsx` | "Data from …" strip (Analytics / DailyVolume / Launches) |
| `frontend/src/utils/format.ts` | daysAgo / timeAgo / fmtVolume helpers |
| `frontend/src/hooks/useWebSocket.ts` | Reconnecting WebSocket with exponential backoff (1s → 30s) |
| `frontend/src/pages/Analytics.tsx` | Weekly ADTV stacked-bar charts |
| `frontend/src/pages/DailyVolume.tsx` | Daily volume stacked-bar charts (30d) |
| `frontend/src/pages/Launches.tsx` | Non-crypto perp futures listings page |
| `frontend/src/pages/CustomReport.tsx` | Ad-hoc report builder (tree picker, 4 metrics, Chart/Stacked/Table/Pie) |

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
  messages to protobuf encoding. `protobuf` must be in
  `backend/requirements.txt` — without it the collector logs
  `NotSupported: mexc requires protobuf` on every tick and MEXC
  real-time prices are silently dropped.  Pinned to `6.33.5`: the 6.x
  runtime still accepts ccxt's MEXC gencode 5.29.x (one-major-back
  guarantee, verified by import) and is REQUIRED by `finam-sdk` stubs
  (gencode 6.33) — do not downgrade below 6.x.
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
- **BTC/ETH/SOL volume + OI = PERPETUAL, prices = SPOT**: these stay
  `type='spot'` instruments (real-time price feed uses the spot symbol), but
  their daily *trading volume* and *open interest* are sourced from perpetual
  futures via `CRYPTO_PERP_OVERRIDES` in `backend/app/exchanges.py` (the single
  source of truth, consumed by `backfill/ohlcv.py` and `oi/etl.py`) — the perp
  contract (`BTC/USDT:USDT` on binance/okx/bybit/mexc, `BTC/USDC:USDC` on
  hyperliquid) is fetched and stored under the same canonical `BTC/USDT`. So
  every volume chart (Analytics / Daily Volume / History / TradFi crypto group)
  and the Open Interest page show perp data while Realtime Prices is unchanged.
  These three symbols always re-fetch their FULL OHLCV history from
  `BACKFILL_SINCE` (not the incremental `latest − 2d` path) so any pre-existing
  spot rows are overwritten with perp — this self-heals existing DBs, so the
  `004_reset_crypto_majors_perp_volume.sql` DELETE is optional (a no-op on fresh
  deploys). The display section was renamed `Spot Crypto` → `Crypto Perps`.
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
- NASD → `NASD` → QQQ/USDT:USDT (NASDAQ-100 → Invesco QQQ)
- SPYF → `SPYF` → SPY/USDT:USDT (S&P 500 → SPDR SPY)
- **Крипто-индексные фьючерсы (добавлено 08.07.2026)** — ISS ASSETCODE = тикеру:
  BTC → `BTC` → BTC/USDT, ETH → `ETH` → ETH/USDT, SOL → `SOL` → SOL/USDT,
  XRP → `XRP` → XRP/USDT, TRX → `TRX` → TRX/USDT.  Canonical БЕЗ `:USDT`, чтобы
  MOEX-оборот стекался с крипто-перпами на volume-графиках (BTC/ETH/SOL уже есть
  в `ohlcv_daily`; XRP/TRX — только MOEX, показываются в секции Crypto Perps).
  ⚠️ На MOEX есть отдельный ASSETCODE `ETHA` (ETH-9.26) — НЕ подключён (нужен `ETH`).

Маппинг в `backend/app/moex/config.py`: `ASSET_ISS_CODE`, `ASSET_TO_CANONICAL`.
Эти же `asset_code → canonical` дублируются в CASE-блоках
`fetch_weekly_adtv_rub` / `fetch_daily_volume_rub` / `fetch_tradfi_daily_volume` /
`fetch_weekly_volume_rub` + константа `_MOEX_CASE` (Custom Report) + `_MOEX_TREE_MAP`
(дерево Custom Report) в `backend/app/db/timescale.py` — при добавлении инструмента
править ВСЕ места.
- **Крипто-коды исключены из TradFi**: `fetch_tradfi_daily_volume` и
  `fetch_weekly_volume_rub(tradfi_only=True)` фильтруют
  `asset_code NOT IN ('BTC','ETH','SOL','XRP','TRX')` — крипта не должна попадать
  на TradFi Market Share (там только сырьё/металлы/US Market).

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
- MOEX сегмент показывается только если у символа реально есть данные MOEX
  (металлы, сырьё, и NASD/SPYF → QQQ/SPY) — проверка `rows.some(r => r.exchange === 'moex')`,
  а не по секции. Поэтому акции US Market без FORTS-контракта не получают пустой moex-сегмент.
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

## SPB Integration (Finam TradeAPI, добавлено 28.06.2026)

Обороты вечных фьючерсов **СПБ Биржи** (`*perpA`), получаемые через **Finam
TradeAPI**.  Отдельная группа вкладок **SPB** в боковом меню (рядом с группой
**Cryptoexchanges**).  Только обороты, не цены.  Вся секция в рублях.

### Источник данных
- Finam TradeAPI, base `https://api.finam.ru`.  Двухступенчатая авторизация:
  секрет (`tapi_sk_…`) → JWT (`tapi_ak_…`, ~15 мин) через `POST /v1/sessions`,
  далее JWT в заголовке `Authorization`.
- Перпы лежат под MIC **`RUSX`**, символ = `<TICKER>@RUSX` (напр.
  `AMZNperpA@RUSX`).  Их **нет** в дампе `/v1/assets`, но `/bars` и
  `/quotes/latest` резолвятся.
- **Токен — только на бэкенде** (`settings.finam_api_token` из `.env`,
  `FINAM_API_TOKEN`).  Даже `readonly`-токен открывает доступ к брокерскому
  счёту владельца (сделки, движения денег) → **никогда не во фронтенд**.  ETL
  делает no-op, если токен не задан.

### Инструменты и лоты
`SPB_INSTRUMENTS: {ticker → (name, lot, group)}` в `backend/app/spb/config.py` —
единый источник правды; из него выводятся `SPB_NAMES`/`SPB_LOTS`/`SPB_GROUPS`/
`SPB_TICKERS`.  Группы (`SPB_GROUP_ORDER`): **US Market** и **Crypto**.
- 20 акций США — лот **1** (AMD, AMZN, TSLA, COIN, NFLX, …).
- 5 крипто-индексов — дробные/крупные лоты: `BTCUSDperpA` 0.0001,
  `ETHUSDperpA` 0.001, `SOLUSDperpA` 0.1, `XRPUSDperpA` 10, `TRXUSDperpA` 10.
- Лот **критичен**: исторический оборот = `volume × цена × lot`; без множителя
  крипто-индексы завышаются в тысячи раз.  Лоты проверены эмпирически
  (`turnover / (volume × price)` == lot).

### Методология оборота
- Хранение в USD: `spb_daily_volume(date, ticker, volume, turnover_usd)`,
  PK `(date, ticker)`.  Создаётся в `init_db` + миграция `005_spb_tables.sql`.
- **Текущий день** — точный `turnover` из живой котировки (`/quotes/latest`).
- **История (бэкфилл)** — приближённо из дневных свечей (`/bars`), где есть
  только `volume`: `turnover_usd ≈ volume × (H+L+C)/3 × lot`.
- **USD → RUB** на этапе запроса через тот же `LEFT JOIN LATERAL moex_fx_rates`
  (курс USDRUBF, forward-fill), что и крипта/MOEX → графики сравнимы.

### ETL
- `spb_etl_loop` каждые 6 ч (`backend/app/spb/etl.py`).  Бэкфилл 180 дней на
  первом запуске, инкремент `latest − 7d`.
- Finam **жёстко лимитит** частые запросы → JWT кешируется, троттлинг `0.4s`
  между вызовами, ретраи с backoff (`backend/app/spb/fetcher.py`,
  async `httpx`).
- В дни без сделок `volume`/`turnover` в котировке могут отсутствовать →
  парсить через `num_value()` (отсутствие = 0).

### Эндпоинты / запросы
- `GET /api/spb/daily-volume` → `fetch_spb_daily_volume()` (30 дней, ₽ на день).
- `GET /api/spb/weekly-adtv`  → `fetch_spb_weekly_adtv()` (ADTV по ISO-неделям).
- `POST /api/spb/refresh` — ручной прогон ETL.
- `upsert_spb_daily_volume` / `get_spb_latest_date` в `timescale.py`.

### Фронтенд (группа SPB)
| Страница | Файл | Что |
|----------|------|-----|
| Weekly Performance | `frontend/src/pages/SPBWeekly.tsx` | Недельный ADTV, ₽M |
| SPB Volume | `frontend/src/pages/SPBVolume.tsx` | Дневной оборот 30 дней, ₽M |
| Open Interest | `frontend/src/pages/SPBOpenInterest.tsx` | Дневной OI 30 дней по каждому инструменту, ₽M (см. раздел ниже) |
| Market Share | `frontend/src/pages/SPBMarketShare.tsx` | Два раздела — **Volume** и **Open Interest** — по 4 графика (инструмент/группа × абсолют ₽B/доля %) |

- Навигация сгруппирована в `Header.tsx` (`NAV_GROUPS`: Cryptoexchanges / SPB),
  страницы — `'spb-weekly'`, `'spb-volume'`, `'spb-open-interest'`, `'spb-market-share'`.
- Группа берётся из бэкенда (поле `group` в ответе API), фронт раскладывает по
  секциям через `SectionHeading` — новый инструмент достаточно завести в
  `SPB_INSTRUMENTS`.
- Market Share: подписи итогов над абсолютными столбцами — Plotly `annotations`
  (надёжный `textangle`, в отличие от text-трейса).

### Статус (на 28.06.2026)
- ✅ 25 инструментов (20 US + 5 crypto), ~3 мес. истории, обороты в ₽
- ✅ 3 страницы + группировка меню Cryptoexchanges / SPB
- ⏳ Российские акции (ВТБ как `VTBperp@RUSX` — суффикс `perp`, не `perpA`) — не добавлены
- ⚠️ Локальный `.env` содержит засвеченный демо-токен Finam — перевыпустить

## SPB Open Interest (официальный API СПБ Биржи, добавлено 06.07.2026)

Открытый интерес по тем же 25 перпам — из **собственного публичного API СПБ
Биржи** (`https://spbexchange.ru/api`), НЕ из Finam.  Без токена и без
авторизации.  Этот же фид отдаёт точный оборот и официальный курс, поэтому в
перспективе может заменить Finam целиком (пока используется только для OI).

### Источник и методология
- `GET /im/v1/tradingResults/futuresDay/all?date=YYYY-MM-DD&page=0&size=500` —
  Spring-пагинация: без `date`+`page`+`size` отдаёт 500.  Одна строка на
  инструмент × сессию.
- **Три сессии в день**: утренняя (`session=2`), основная (`session=1`),
  вечерняя (`session=3`).  Берём **только `session=3`** — это накопленный итог
  на конец дня (проверено: его `totalQty` == дневной бар Finam; суммировать
  сессии НЕЛЬЗЯ — задвоит).
- **Маппинг тикеров**: `futuresCode` = тикер конфига без хвостовой `A`
  (`BTCUSDperpA` → `BTCUSDperp`).  `_CODE_TO_TICKER` в `oi_etl.py`.
- **OI хранится с ДВУХ сторон (long + short) = `_SIDES = 2`**: сайт СПБ Биржи
  публикует суммарные позиции обеих сторон, а API-поля `totalOpenPosition` /
  `totalOpenPositionVolume` односторонние → умножаем на 2, чтобы цифры на
  странице совпадали с таблицей биржи (сверено с данными коллеги на 30.06 —
  контракты до единицы, сумма до рубля).  Фронт помечает: «Открытый интерес
  учитывается с двух сторон (long + short)».
- Хранение в USD: `spb_open_interest(date, ticker, oi_contracts, oi_usd)`, PK
  `(date, ticker)`.  `oi_usd` = `totalOpenPositionVolume × 2`.  Миграция
  `006_spb_open_interest.sql` + inline в `init_db`.
- **USD → RUB** на этапе запроса через тот же `LEFT JOIN LATERAL moex_fx_rates`
  (USDRUBF), что и оборот/крипта.  ⚠️ Из-за этого рублёвая сумма расходится с
  сайтом на ~0.8%: сайт считает по своему `localCourse` (есть в ответе API), а
  app — по курсу MOEX (единый курс для сопоставимости всех страниц).  Контракты
  совпадают точно.

### Нюансы
- **TLS**: сайт отдаёт неполную цепочку сертификатов (curl нужен `-k`) →
  `httpx.AsyncClient(verify=False)` в `spb_api.py`.  Допустимо для публичного
  read-only GET рыночных данных.
- **Нерабочие дни**: официальный фид НЕ публикует итоги за праздники/выходные
  (напр. 12–14 июня → 0 строк), поэтому в `spb_open_interest` этих дат нет.  А
  Finam-оборот их содержит (у СПБ Биржи есть сессии выходного дня) → оси X у
  разделов Volume и Open Interest могут различаться.  Это не баг ETL.

### ETL / файлы
- `backend/app/spb/spb_api.py` — клиент официального API (`fetch_futures_day_eod`).
- `backend/app/spb/oi_etl.py` — `spb_oi_etl_loop` (каждые 6 ч), итерирует по
  датам от `latest − 7d` (или 180 д на первом запуске) до сегодня, один HTTP на
  день = все тикеры.  Зарегистрирован в `main.py` (`_spawn(spb_oi_etl_loop())`).
- `timescale.py`: `upsert_spb_open_interest`, `get_spb_oi_latest_date` (по всей
  таблице), `fetch_spb_oi_daily` (30 дней, ₽).
- `GET /api/spb/open-interest` → `fetch_spb_oi_daily()`; `POST /api/spb/oi-refresh`.
- Изменение `_SIDES` требует полного пересчёта: `TRUNCATE spb_open_interest` +
  прогон `run_spb_oi_etl` (иначе смешаются старые/новые значения).

## SPB Order Book (живой стакан, Finam TradeAPI, добавлено 08.07.2026)

Вкладка **Order Book** в группе **SPB** — текущий стакан заявок по всем 25
перпам, обновляется непрерывно (терминальный стиль).  Цена в **USD** (валюта
котировки), объём — в **контрактах**.  В отличие от остальных SPB-страниц это
**не БД и не ETL**, а живой снимок из Finam, поэтому в рубли НЕ конвертируется
(стакан про уровни цен, а не про сопоставимость оборотов).

### Архитектура: gRPC-стрим (основной) + REST-поллер (fallback) + тёплый кэш
- `backend/app/spb/orderbook.py` — `spb_orderbook_poll_loop()` держит снимок в
  модульном `_cache` (пред-заполнен плейсхолдерами, чтобы API сразу отдавал все
  25 карточек «загрузка…»).  Эндпоинт `GET /api/spb/orderbook` отдаёт кэш
  **мгновенно**, НЕ ходит в Finam на каждый запрос.  Зарегистрирован в `main.py`
  (`_spawn(spb_orderbook_poll_loop())`).
- **Основной фид — gRPC-стриминг** (добавлено 08.07.2026): официальный SDK
  `finam-sdk` (import `finam_trade_api`), `AsyncFinamClient` +
  `MarketDataService.SubscribeOrderBook` — **по одному server-stream на тикер,
  25 параллельных стримов на одном канале** (`_stream_session`).  Первое
  сообщение — полный снапшот (до 50 bid + 50 ask, ACTION_ADD), дальше дельты
  ACTION_ADD/UPDATE/REMOVE по ценовым уровням → книга ведётся в памяти
  (`_consume_stream`) и рендерится в кэш на каждом сообщении.  Это настоящий
  tick-by-tick (ликвидные тикеры — до ~7 сообщений/с).  Проверено вживую:
  **стримы НЕ попадают под REST-лимиты 429** — 25 параллельных подписок ок.
  Упавший стрим переподписывается через `_STREAM_RETRY_SEC=3s`, книга при этом
  не обнуляется; переподписка начинает с чистого снапшота сервера.
- **Fallback — прежний REST-поллер** (`_rest_poll_window`): если finam-sdk не
  установлен или gRPC-сессия упала, окно доживает на последовательном REST-обходе
  (~15 с на цикл), следующее окно снова пробует gRPC.
- **Ленивый**: фид живёт только пока эндпоинт читали в последние
  `_ACTIVE_WINDOW_SEC=30s` (кто-то на странице); иначе спит `_IDLE_SLEEP_SEC`,
  стримы и канал сворачиваются.  Каждое чтение API продлевает окно
  (`note_access`).  Так Finam не бомбардируется 24/7 (важно — токен брокерский).
- Публичный API самой биржи стакан НЕ отдаёт — только Finam.

### ⚠️ Дисциплина рейт-лимита REST (актуально для fallback и ETL)
Finam жёстко лимитит частоту **unary/REST** вызовов.  Первая версия с
параллельными запросами (concurrency=6) + ретраями ×4 на каждый 429 давала
лавину `429 Too Many Requests` на ВСЕХ инструментах.  Правила REST-фолбэка:
- тикеры обходятся **строго последовательно** с троттлингом `_THROTTLE_SEC=0.5`
  между вызовами (как в проверенном SPB ETL) — НИКАКОГО параллелизма;
- только один цикл за раз (`_cycle_lock`), пауза `_CYCLE_PAUSE_SEC=2s` между
  циклами;
- `fetch_orderbook(..., retries=1)` — повтор 429 с backoff лишь усугубляет, тикер
  пропускается до следующего цикла;
- сбой по тикеру сохраняет предыдущие уровни + пишет `error` (книга не
  обнуляется); карточка показывает ошибку/«загрузка», только пока уровней нет.

### Фронтенд (`frontend/src/pages/SPBOrderBook.tsx`)
- Опрашивает кэш каждые `POLL_MS=1000` (guard `inFlight` от наложения),
  индикатор **● Live** + кнопка **Pause/Resume**.
- Раскладка стакана: asks (красные) сверху → mid + spread% → bids (зелёные)
  снизу, depth-бар по объёму относительно макс. уровня.
- **Вспышка на изменившихся уровнях** (терминальный стиль): уровни keyed по
  `price+size`, изменение → remount строки → проигрывается CSS-анимация
  `.ob-flash-ask`/`.ob-flash-bid` (в `styles/index.css`).  bid-обновления мигают
  зелёным, ask — красным; на первом заполнении не мигает (пустой `prevRef`).

## Stocks Integration (фондовые перпы криптобирж, добавлено 08.07.2026)

Оборот торгов **вечными фьючерсами на акции** (equity perps) на **Binance, OKX,
Bybit, MEXC, Hyperliquid** — недельный и дневной, в рублях.  Показывается двумя
графиками (по биржам / по инструментам топ-20 + «Прочее») **внизу вкладки
Market Share (TradFi)** — они реагируют на существующий переключатель
**Daily/Weekly**.  ~520 инструментов, ~207 уникальных тикеров.

### Классификация фондовых перпов (`backend/app/stocks/config.py`)
Каждая биржа помечает equity по-своему (`is_equity`):
- **Binance**: `info.underlyingType == 'EQUITY'`
- **OKX**: `info.instCategory == '3'`
- **Bybit**: `info.symbolType == 'stock'` (базы вида `AMDSTOCK`/`CATSTOCK`/`NOKIA`)
- **MEXC**: `base` оканчивается на `STOCK` (`<TICKER>STOCK_USDT`, зона `tradfi/Stock`)
- **Hyperliquid**: префикс `XYZ-`, затем **кросс-референс**: оставляем только те
  базы, что есть в списке акций флаговых бирж (иначе под `XYZ-` попадают
  сырьё/FX/индексы).

`canon()` приводит тикеры к общему виду (снимает `XYZ-`/`STOCK`, алиасы
`NOKIA→NOK`, `SMSN→SAMSUNG`, `SKHX→SKHYNIX`).  `EXCLUDE` выкидывает ETF/индексы/
FX/сырьё; **pre-IPO компании включены** (SPCX/SpaceX, OpenAI, Anthropic, MiniMax,
Zhipu — это компании, просто не публичные).

### Данные и методология
- Хранение в USD: `stock_daily_volume(date, exchange, ticker, quote_usd)`, PK
  `(date, exchange, ticker)`.  Миграция `007_stock_tables.sql` + inline в `init_db`.
- `quote_usd = close × volume × contractSize`.  **`contractSize` критичен только
  для MEXC** (0.01 / 0.001) — без него крипто-объёмы MEXC завышаются в десятки раз;
  на остальных биржах `contractSize = 1`.
- **USD → RUB** на этапе запроса через тот же `LEFT JOIN LATERAL moex_fx_rates`
  (USDRUBF, forward-fill), что и у крипты/MOEX/SPB — графики сопоставимы.
- MEXC дневные клайны отдают placeholder-бары в будущее → бары с `ts > now`
  пропускаются (как в `backfill/ohlcv.py`).

### ETL / эндпоинты / фронт
- `backend/app/stocks/etl.py` — `stock_etl_loop` (каждые 6 ч; при пустой таблице
  бэкфилл с `BACKFILL_SINCE=2026-01-01`, далее инкремент `latest − 2d`).
  `_build_universe()` заново сканирует `load_markets()` каждый прогон (ловит новые
  листинги); фетч по биржам параллельно (`asyncio.gather`), upsert одним пакетом
  в конце прохода.  Зарегистрирован в `main.py` (`_spawn(stock_etl_loop())`).
- `timescale.py`: `upsert_stock_daily_volume`, `get_stock_latest_date`,
  `fetch_stock_daily_volume(by)` (30 дней), `fetch_stock_weekly_volume(by)` (ISO-
  недели с 01.01, `by ∈ {exchange, instrument}`).
- `GET /api/stocks/volume?period=daily|weekly` → `{by_exchange, by_instrument}`
  (`backend/app/api/routes/stocks.py`); `POST /api/stocks/refresh`.
- `frontend/src/pages/TradFiMarketShare.tsx`: компоненты `StockByExchange` и
  `StockByInstrument` (топ-20 по обороту + «Прочее»); данные грузятся тем же
  `load(view)`, что и TradFi-графики, из `/api/stocks/volume?period=<view>`.

### Нюансы
- **Регистрация в `main.py` — ДВА места**: `_spawn(stock_etl_loop())` в lifespan
  И `app.include_router(stocks.router, prefix="/api/stocks")`.  Только импорта
  недостаточно — маршрут будет 404, а ETL не запустится.
- `--reload` uvicorn иногда не подхватывает добавление роутера/новых модулей —
  надёжнее `docker compose up -d --force-recreate backend`.
- Weekly включает **текущую неполную неделю**, дневной график — **сегодняшний
  неполный день** (наполняются по мере торгов) — как и на других volume-страницах.
- MEXC: **TSLA деривативом отсутствует** (только спот `TSLAX`/`TSLAON`); зато у
  MEXC самый широкий фондовый ассортимент (~187 тикеров).

## Custom Report (конструктор ad-hoc отчётов, добавлено 08.07.2026)

Вкладка **Reports → Custom Report** (`frontend/src/pages/CustomReport.tsx`) —
самостоятельный мини-BI поверх УЖЕ собранных данных (нового ETL нет).  Юзер
выбирает метрику, инструменты, диапазон, агрегацию и форму отображения.

### Метрики (единый диспетчер по источникам)
`REPORT_METRICS = (volume, open_interest, price, funding)` в `timescale.py`.
Инструменты лежат в разных таблицах, поэтому `metric` определяет источники:
- **volume** → `ohlcv_daily` + `moex_daily_value` + `spb_daily_volume` + `stock_daily_volume`
- **open_interest** → `open_interest` + `spb_open_interest`
- **price** → `ohlcv_daily` (только крипта/tradfi — у SPB/stocks нет цены)
- **funding** → `funding_rates` (только крипта)

Деньги по умолчанию в ₽ (тот же forward-fill `LEFT JOIN LATERAL moex_fx_rates`,
что и везде), `currency=usd` — нативный USD.  Каждый запрос ограничивает верх
времени (`$2::date + 1 day`) → планировщик не локает пустые future-чанки.

### Backend (`backend/app/db/timescale.py` + `api/routes/reports.py`)
- `fetch_report(metric, symbols, exchanges, from, to, agg, currency)` — общий
  запрос, ветвление по метрике; `agg ∈ daily|weekly|monthly`.  **Fallback #3**:
  если у инструмента нет данных в окне (запущен позже `from`) — возвращается его
  последняя доступная точка (иначе пустая серия).
- `fetch_report_options(metric)` — доступные инструменты/биржи/диапазон дат.
- `fetch_report_tree(metric)` — иерархия для пикера: **Cryptoexchanges → биржа →
  класс актива** (Crypto/Commodities/US stocks/Indexes/Korean market),
  **SPB futures → класс**, **MOEX forts → класс**.  Классификация: `_crypto_class`,
  `_MOEX_TREE_MAP`, `SPB_GROUPS`.
- Эндпоинты: `GET /api/reports/{tree,options,data}`.  `/data` принимает
  `pairs=exchange~symbol,...` (точный выбор пары инструмент×биржа — post-filter
  по паре, без кросс-произведения) ИЛИ legacy `symbols`+`exchanges`.
- Регистрация в `main.py`: импорт + `include_router(reports.router, prefix="/api/reports")`.

### Фронтенд (`CustomReport.tsx`)
- **Дерево-пикер** (сворачиваемое) вместо плоского списка; выбор = `Set` пар
  `exchange~symbol`; чекбокс «выбрать все» на уровне класса; фильтр по названию;
  очистка фильтра (× в поиске) и всего выбора («clear selection»).
- **Формы отображения** (`view`): Chart (grouped bar / line), Stacked, Table, Pie.
  Один Plotly-узел ВСЕГДА смонтирован (в Table скрыт `display:none`) + `Plotly.purge`
  перед каждым рендером — иначе смена pie↔cartesian на одном узле падает.
- Цвета серий — общая палитра по индексу (MOEX здесь НЕ красный, в отличие от
  Daily Volume/Weekly, где `EXCHANGE_COLORS`).  Сводка данных для всех форм —
  один `useMemo summary`.

## Behavioral Guidelines

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.