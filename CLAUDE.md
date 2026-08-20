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

## Running tests

120 tests in `backend/tests/`, no DB/network/Docker required (pure unit tests over
parsing, config and spread math).  Фронт: `cd frontend && npm test` (vitest,
`src/**/*.test.ts` — юниты на подбор единицы измерения и классификацию секций).

```bash
cd backend
python3.12 -m venv .venv                  # ⚠️ 3.12 — NOT 3.13, see below
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest                          # 120 passed in ~1.5s
```

Inside Docker: `docker compose exec backend pytest` (pytest ships in
`requirements.txt`, and the prod stage copies `tests/` + `pytest.ini` into the
image).

- **Python must be 3.12**, matching `python:3.12-slim` in the Dockerfile.
  `asyncpg==0.29.0` has no 3.13 wheel and its bundled C code fails to compile
  against the 3.13 C API (`_PyLong_AsByteArray` changed signature) — the install
  dies on `pgproto.c`, which is what makes the suite look "broken" on a stock
  macOS python3.
- `pytest.ini` sets `pythonpath = .` so `app.*` imports resolve from any working
  directory.  Without it the suite collects ONLY via `python -m pytest` launched
  from `backend/` (that form implicitly puts the CWD on `sys.path`); bare
  `pytest` and runs from the repo root fail with `ModuleNotFoundError: app`.
- `backend/.venv` is gitignored **and** dockerignored — 200+ MB of host-platform
  binaries must never reach the image via the prod stage's `COPY . .`.

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
| `backend/app/api/cache.py` | TTL-кеш ответов аналитических эндпоинтов (`@ttl_cache`) |
| `backend/app/db/migrations/001_init.sql` | TimescaleDB schema |
| `backend/app/moex/{config,fetcher,etl,calendar}.py` | MOEX FORTS integration |
| `backend/app/spb/{config,fetcher,etl}.py` | SPB Exchange perp turnover via Finam TradeAPI |
| `backend/app/spb/{spb_api,oi_etl}.py` | SPB Exchange open interest via the exchange's own public API (no token) |
| `backend/app/spb/{funding,funding_tg,funding_exchange}.py` | Фандинг СПБ: парсер CSV канала, ингест из Telegram, фид самой биржи |
| `backend/app/spb/orderbook.py` | Live order-book cache (gRPC stream + REST fallback) + AVG_SPREAD math |
| `backend/app/spb/spread_etl.py` | Trading-hours spread-on-volume collector → 15-min average buckets |
| `backend/app/api/routes/spb.py` | SPB REST endpoints (daily-volume / weekly-adtv / open-interest / orderbook / spread-history / refresh / oi-refresh) |
| `frontend/src/pages/SPBOrderBook.tsx` | Live стакан + spread-on-volume history chart (all 25 perps) |
| `backend/app/stocks/{config,etl}.py` | Equity (stock) perps on crypto exchanges — classification + volume ETL |
| `backend/app/stocks/hourly_etl.py` | Тот же universe с часовой гранулярностью → `stock_hourly_volume` |
| `backend/app/crypto/{config,etl}.py` | Топ-100 криптоперпов по обороту на биржу → `crypto_top_{daily,hourly}_volume` |
| `backend/app/api/routes/stocks.py` | Stock REST endpoints (`/volume?period=daily\|weekly`, `/refresh`) |
| `backend/app/api/routes/launches.py` | Hourly cache of non-crypto perp listings |
| `backend/app/api/routes/reports.py` | Custom Report endpoints (`/tree`, `/options`, `/data`) |
| `backend/app/mm/{config,universe}.py` | MM FORTS groups + ISS front-month/step/currency/liquidity universe |
| `backend/app/mm/{orderbook,spread_etl}.py` | MM live book feed (lazy-per-tab) + 24/7 15-min spread collector |
| `backend/app/api/routes/mm.py` | MM REST endpoints (`/groups`, `/orderbook`, `/spread-history`, `/spread-live`) |
| `frontend/src/components/OrderBookViz.tsx` | Shared LiveBook + spread x-axis helpers (SPB Order Book + MM) |
| `frontend/src/pages/MM.tsx` | MM FORTS page (per group): live book + 15-min spread charts |
| `frontend/src/components/Chart.tsx` | TradingView chart wrapper |
| `frontend/src/components/SectionHeading.tsx` | Shared section divider |
| `frontend/src/components/ExchangeSourceBadges.tsx` | "Data from …" strip (Analytics / DailyVolume / Launches) |
| `frontend/src/utils/format.ts` | daysAgo / timeAgo / fmtVolume helpers |
| `frontend/src/hooks/useWebSocket.ts` | Reconnecting WebSocket with exponential backoff (1s → 30s) |
| `frontend/src/pages/Analytics.tsx` | Weekly ADTV stacked-bar charts |
| `frontend/src/pages/DailyVolume.tsx` | Daily volume stacked-bar charts (30d) |
| `frontend/src/pages/HourlyVolume.tsx` | Hourly volume: ряд по часам + профиль по часам суток |
| `backend/app/backfill/hourly.py` | Часовые свечи криптобирж → `ohlcv_hourly` |
| `frontend/src/pages/Launches.tsx` | Non-crypto perp futures listings page |
| `frontend/src/pages/Funding.tsx` | Funding: тепловая карта (инструменты × дни) + All Rates + History |
| `frontend/src/pages/CustomReport.tsx` | Ad-hoc report builder (tree picker, 4 metrics, Chart/Stacked/Table/Pie) |
| `services/crypto-index/` | Сторонний пакет сборщика криптоиндекса (деплоится на хост прода, НЕ в docker; бывш. `handoff-crypto-index/` в корне) |
| `data/funding-spb/` | Локальные CSV/XLSX с фандингом СПБ (не в git; бывш. `Funding SPB/` в корне) |
| `scripts/export_report.py` | Выгрузка данных с прода → `exports/YYYY-MM-DD/` (Excel + PNG-графики) |
| `exports/` | Готовые выгрузки: report_*.xlsx + графики (не в git) |
| `frontend/src/pages/CryptoIndex.tsx` | Вкладка Crypto Index (данные из `/crypto-index/api/*`) |
| `backend/app/okr/{config,etl}.py` | OKR: корзины зеркальных контрактов MOEX + дневной свип FORTS |
| `backend/app/api/routes/okr.py` | OKR endpoints (`/ratio`, `/refresh`) |
| `frontend/src/pages/OKR.tsx` | Вкладка OKR (KPI + дневная линия отношения) |

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

Hourly cached scan of `load_markets()` on every crypto exchange
(Binance, OKX, Bybit, MEXC, Bitget, Hyperliquid), filtered to a hard-coded
`NON_CRYPTO_BASES` allow-list (commodities, metals, indices, US stocks).
Listing date is read from exchange-specific metadata fields: `onboardDate`
(Binance), `listTime` (OKX), `launchTime` (Bybit), `createTime` (MEXC).

**Bitget и Hyperliquid не публикуют дату листинга** → она выводится из
первой дневной свечи с реальным объёмом (`_first_traded_date`, добавлено
29.07.2026).  Два режима запроса, потому что биржи по-разному отвечают на
`since` раньше листинга:
- Hyperliquid отдаёт всю историю одной страницей → берём первый бар с
  `volume > 0` (у старых рынков история дополнена нулевыми барами с 2020 г.).
- Bitget (как и OKX) на слишком старый `since` отдаёт ПУСТУЮ страницу →
  шагаем назад по 60 дней (страница биржи ~90 баров, launch не перепрыгнуть),
  пока страница не начнётся позже запрошенного `since` — это и есть запуск.
  ⚠️ Поле Bitget `openTime` использовать НЕЛЬЗЯ: у всех equity-перпов там
  одна дата 2026-02-02 (массовая переконфигурация), тогда как реально они
  торгуются с августа 2025; `launchTime` у Bitget всегда пустой.
Результаты кешируются в памяти (`_first_trade_cache`) И **сохраняются в БД**
(`launch_first_trade`, миграция `014` + inline в `init_db`): полный вывод дат
стоит ≈ 2.5 мин, поэтому после рестарта кеш поднимается из таблицы
(`_load_first_trade_cache` на первом рефреше), а новые символы дописываются
(`_save_first_trade_cache`).  ⚠️ Иначе холодный старт упирается в **60-секундный
proxy-таймаут nginx** — `GET /api/launches/` до окончания первого рефреша
отдаёт обрезанный 504.
Если скан биржи упал (Hyperliquid регулярно отдаёт 429 на `load_markets`), в
кеше остаются её строки с прошлого прохода (`_last_good_rows`), а не пустота.

Hyperliquid хостит несколько **builder-DEX** (`xyz:`, `cash:`, `flx:`, `km:`,
`mkts:`, `para:`, `vntl:`, `hyna:`, `abcd:` → базы `XYZ-AAPL`, `FLX-GOLD`, …).
Один и тот же актив может торговаться на нескольких из них, поэтому строки
схлопываются до одной на базовый актив с САМОЙ РАННЕЙ датой запуска
(`_collapse_to_earliest`) — иначе на странице было бы 4 строки GOLD.

**Тикеры-омонимы исключены из `NON_CRYPTO_BASES`** (сверено по цене
29.07.2026): `SPX` — это мемкоин SPX6900 ($0.32), а не S&P 500 (индекс идёт
как `SPX500` на MEXC); `CVX` — Convex Finance ($1.33), а не Chevron; `F` —
токен ($0.0029), а не Ford.  Даты у них были корректные, но инструмент не тот.

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
- **uvicorn `--workers` ДОЛЖЕН быть 1** (prod-target в `backend/Dockerfile`):
  lifespan `_spawn`-циклы (SPB/MOEX/stocks/OI ETL, orderbook-фид, spread-коллектор)
  стартуют в КАЖДОМ воркере.  С `--workers 2` каждый ETL шёл дважды параллельно и
  удваивал REST-нагрузку на Finam (429 → пропуски тикеров → частично пустые
  Volume/OI по утрам), а in-memory кэши стакана/спреда расщеплялись по процессам.
  Исправлено 15.07.2026.
- **WTI symbol names differ per exchange**: canonical is
  `WTI/USDT:USDT`; each exchange uses a different ccxt symbol. Aliases
  stored in `instruments.aliases` JSONB column:
  `binance/okx/bybit → CL/USDT:USDT`, `mexc → USOIL/USDT:USDT`,
  `hyperliquid → XYZ-CL/USDC:USDC` (launched 2026-01-06, id 110029).
  MOEX FORTS's old `CL` series is dead (last trade 2022), but a **new WTI
  contract exists** (ASSETCODE `WTI`, `WTQ6`=WTI-8.26, listed 13.07.2026,
  $/баррель) — оно подключено на вкладке MM → Товары, см. `MM_EXTRA_ASSETS`.
  Оборот/цены WTI с MOEX по-прежнему не собираются (только MM-стакан/спред).
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
- **Мини-контракты суммируются с большими** (31.07.2026): `ASSET_ISS_MINI_CODE`
  (`BR`+`BRM`, `NG`+`NGM`, `GD`=GOLD+`GOLDM`, `SV`=SILV+`SILVM`) — оба ETL
  (обороты и OI) обходят `iss_codes_for(asset)` и складывают тоталы по датам
  ДО upsert'а (upsert перезаписывает, а не суммирует).  SECID-префиксы мини
  другие (BMxx/NRxx/GNxx/S1xx), но discovery фильтрует по ISS-параметру
  `assetcode`, так что дополнительного маппинга не нужно.  ⚠️ Инкрементальный
  ETL пересчитывает только `latest − 7d` — при подключении нового мини-кода
  историю нужно форсированно пересчитать за всё окно (на проде сделано
  31.07.2026: обороты с 01.12.2025, OI — DELETE moex-строк + 180-дневный
  ре-бэкфилл).
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
- Ось Y: **единица подбирается под каждый график** (`pickUnit` в
  `frontend/src/utils/scale.ts`, 30.07.2026) — ₽K/₽M/₽B/₽T по максимальной
  СУММЕ стека (`maxStackedTotal`, а не по отдельному сегменту), 1 знак после
  запятой.  Раньше было жёстко `Korean ? ₽M : ₽B` (Analytics/DailyVolume) и
  всегда `₽M` (OpenInterest), из-за чего BTC OI выглядел как «500,000M», а
  мелкие инструменты схлопывались в «0.0».  Заголовок оси, `ticksuffix` и
  hover берут суффикс из одного места.  Тот же хелпер — на 5 абсолютных
  графиках TradFi Market Share (там же подписи-итоги над столбцами дают
  2/1/0 знаков в зависимости от величины).
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
- `spb_etl_loop` (`backend/app/spb/etl.py`) — wall-clock расписание по МСК
  (15.07.2026): каждые 30 мин в окне 06:00–09:00, каждый час в остальное время
  (живой turnover текущего дня растёт непрерывно).  Бэкфилл 180 дней на
  первом запуске, инкремент `latest − 7d`.  `spb_oi_etl_loop` — та же схема,
  но вне утреннего окна раз в 6 ч.
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
- **⚠️ Watchdog тишины (`_STREAM_SILENCE_SEC=30s`, добавлен 09.07.2026)**: канал
  Finam умеет умирать *молча* — стримы открыты, снапшот пришёл, но дельты
  перестают идти БЕЗ ошибки (наблюдалось вживую: кэш замер на 7+ мин при
  активной странице).  Если ни один из 25 стримов не прислал сообщение за 30с,
  `_stream_session` бросает RuntimeError → окно доживает на REST-фолбэке,
  следующее окно снова пробует gRPC.  В ночные паузы торгов watchdog может
  переключить на REST — это безвредно (REST вернёт те же уровни).
- **⚠️ Таймауты на все фазы сессии (09.07.2026)**: у SDK нет дедлайнов, и
  `AsyncFinamClient.__aenter__` умеет виснуть НАВЕЧНО на битом канале
  (наблюдалось вживую: фид замер без единой строки в логах — watchdog внутри
  сессии недостижим, пока висит connect).  Поэтому connect ограничен
  `_STREAM_CONNECT_TIMEOUT_SEC=15s`, teardown (gather отменённых стримов +
  `__aexit__`) — `_STREAM_CLOSE_TIMEOUT_SEC=10s`.  TimeoutError → тот же
  REST-фолбэк.  В логе `gRPC session failed (TimeoutError())` — это оно.
- **Fallback — прежний REST-поллер** (`_rest_poll_window`): если finam-sdk не
  установлен или gRPC-сессия упала, окно доживает на последовательном REST-обходе
  (~15 с на цикл), следующее окно снова пробует gRPC.
- **Ленивый**: фид живёт только пока эндпоинт читали в последние
  `_ACTIVE_WINDOW_SEC=30s` (кто-то на странице); иначе спит `_IDLE_SLEEP_SEC`,
  стримы и канал сворачиваются.  Каждое чтение API продлевает окно
  (`note_access`).  Так Finam не бомбардируется 24/7 (важно — токен брокерский).
- Публичный API самой биржи стакан НЕ отдаёт — только Finam.
- **Стакан MOEX для 5 крипто-индексов (добавлено 14.07.2026)**: в том же
  gRPC-сеансе (тот же канал) дополнительно подписываются 5 фьючерсов MOEX FORTS
  (`<SECID>@RTSX`, фронт-месяц из `_ensure_moex_secids`, резолв раз в сутки) →
  30 стримов на канале, лимитов нет.  Отдельный кэш `_moex_cache` (ключ = SPB
  крипто-тикер), выдаётся `get_cached_moex_orderbooks()` → `GET
  /api/spb/moex-orderbook`.  Стрим/REST-обход обобщены (`_consume_stream`,
  `_stream_ticker`, `_refresh_cycle` принимают symbol/cache/key).  На фронте
  рисуется 4-й столбец крипто-карточки (справа от графиков).

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

## Spread on Volume — AVG_SPREAD (добавлено 09.07.2026)

История **спреда на объём** (эффективный спред при исполнении заданного объёма,
с учётом прохода по стакану) для всех 25 перпов СПБ — справа от каждого стакана
на странице Order Book.  Одна линия — глубина **1 млн ₽ на сторону** (1 млн на
bid и 1 млн на ask), по 15-мин интервалам; график **$** (абсолют) и **%**.
Линия 10 млн убрана 14.07.2026.

### Методология (абсолют в $ без лота — обновлено 15.07.2026)
- Глубина берётся **полностью на каждую сторону**: **1 млн ₽ на bid И 1 млн ₽ на
  ask**.  Лот и курс нужны только чтобы отмерить эту рублёвую глубину по стакану
  (`_vwap_to_notional`: `level_rub = price × size × lot × usdrub`).
- `P_aver_ask` = VWAP покупки (проход по ask снизу вверх), `P_aver_bid` = VWAP
  продажи (по bid сверху вниз); последний уровень добирается частично.
- **Абсолют = `P_aver_ask − P_aver_bid` в ДОЛЛАРАХ** — сырая разница VWAP-цен,
  **БЕЗ ×лот и БЕЗ ×курс** (15.07.2026, по просьбе).  Это спред «за единицу
  цены», поэтому **сравним между SPB и MOEX** несмотря на разный размер контракта
  (раньше был `×lot×USDRUB` → per-contract ₽, из-за чего абсолют MOEX казался
  больше просто из-за крупного контракта — теперь этого искажения нет).
- **Процент = Абсолют / middle point**, `middle = (лучший bid + лучший ask)/2`
  (середина лучших котировок, НЕ усреднённая цена исполнения).
  `spread_pct = (P_aver_ask − P_aver_bid) / mid × 100`.  Абсолют и процент теперь
  согласованы (оба растут/падают вместе).
- Хранение: `spb_orderbook_spread.spread_1m_usd` = абсолют в $ (без лота/курса);
  на выдаче (`fetch_spb_spread_history`) курс НЕ применяется — отдаётся как есть.
  Функции `avg_spread_on_volume` (возвращает `p_ask − p_bid`) /
  `spread_pct_on_volume` / `_vwap_to_notional` в `orderbook.py`, покрыты
  `tests/test_spread_on_volume.py`.  `None`, если у стороны нет 1 млн глубины.
- ⚠️ **Смена методологии меняет уже накопленные значения** — старые бакеты
  считались `×lot×USDRUB` (₽/контракт).  Бэкфила нет.  Локальные таблицы
  `spb_orderbook_spread` + `moex_orderbook_spread` **уже очищены** (`TRUNCATE`,
  15.07.2026) — копятся заново в $.  ⚠️ На **проде** (176.12.70.128) после деплоя
  так же `TRUNCATE spb_orderbook_spread` + `moex_orderbook_spread`.

### Сбор истории (`backend/app/spb/spread_etl.py`)
- `spb_spread_collector_loop` — свип по 25 тикерам SPB + 5 MOEX раз в минуту,
  **старт свипа привязан к настенным часам** (:00 каждой минуты, epoch-сетка,
  15.07.2026): обе инстанции (локальная и прод) снимают стакан в одни и те же
  секунды, поэтому их графики совпадают (раньше невыровненные сэмплеры давали
  ~10% расхождения на бакет).  Тикеры **последовательно** с троттлингом 0.5s,
  `fetch_orderbook(retries=1)` (та же дисциплина рейт-лимита).
- **Только в торговые часы (МСК)** (добавлено 15.07.2026): сэмплит спред лишь
  когда открыты MOEX/СПБ — будни **07:00–23:45**, выходные **10:00–19:00**
  (`_is_trading_now`, `_MSK=UTC+3`).  Вне окна коллектор спит (бакеты не
  пишутся).  Раньше был 24/7.  Незакрытый бакет перед закрытием флашится при
  следующем открытии (rollover в `_accumulate`), timestamp корректный.
  ⚠️ Будни закрываются в **23:45, хотя вечерняя сессия FORTS идёт до 23:50**:
  метка строки — КОНЕЦ бакета, поэтому сэмпл после 23:45 попал бы в бакет с
  меткой 00:00, а его фронт вырезал бы с оси (`rangebreaks`).  При 23:45
  последняя точка дня — 23:45, и она видна.  Раньше окно закрывалось в 23:00 и
  точки 23:15/23:30/23:45 терялись вовсе (29.07.2026).
  ⚠️ Границы окна **продублированы во фронте** (`TRADE_WINDOW` в
  `OrderBookViz.tsx`) — менять только вместе, иначе точки либо прячутся, либо
  на оси остаются мёртвые промежутки.
  ⚠️ На фронте эти простои **вырезаются с оси X** (`tradingBreaks` →
  Plotly `rangebreaks`, считаются в том же MSK-as-UTC фрейме, что и `toMsk`),
  поэтому линия непрерывна без мёртвых ночных/выходных промежутков.
  ⚠️ **Тики оси X — свои, не автотики Plotly** (`tradingTicks`, 16.07.2026):
  Plotly расставляет автотики по НЕразрезанному диапазону, из-за чего тик
  попадал ровно на шов ночного выреза и подписывал точку закрытия 23:00 как
  «07:00» следующего дня.  `tradingTicks` раскладывает ~5 тиков равномерно по
  видимой (торговой) части оси, привязывает к круглым часам (15 мин на коротких
  спанах) внутри сессий и подписывает сам (`tickvals`/`ticktext`).
  ⚠️ **Нерабочие дни MOEX — `CLOSED_DAYS`** (`OrderBookViz.tsx`, 03.08.2026):
  список дат (МСК), когда биржа не торговала вовсе; сейчас `2026-08-01`,
  `2026-08-02`.  Такие дни **вырезаются с оси** (`tradingIntervals` их
  пропускает) И **выбрасываются из самих серий** (`stripClosedDays`, заменяет
  `.map(toMsk)` на графиках спреда) — одного `rangebreaks` мало: скрытые точки
  всё равно участвуют в автоскейле оси Y, а в нерабочий день коллектор пишет
  ЗАМОРОЖЕННЫЙ стакан (напр. BTC на MOEX 01.08: 36 бакетов с одним значением
  5272.93 при обычном максимуме 301) — такой выброс расплющил бы реальную линию.
  Действует на обеих вкладках (SPB Order Book и MM), поэтому в такой день с
  Order Book пропадают и реальные точки СПБ (СПБ 02.08 торговала) — так решено
  сознательно, чтобы оси двух вкладок совпадали.  Покрыто
  `src/components/OrderBookViz.test.ts`.
- **Простое среднее в 15-мин точку** (15.07.2026, взамен время-взвешивания):
  `_add_sample`/`_mean` копят sum/count по каждой метрике (~15 сэмплов/бакет,
  None-сэмплы исключаются); когда минутная сетка переходит в новый бакет,
  пишется одна строка/тикер (`upsert_spb_spread_buckets`).  **Метка точки —
  КОНЕЦ интервала**: точка 17:45 = среднее за 17:30–17:45, появляется на
  графике в 17:45–17:46 (фронт из-за этого сдвигает начало ночного
  rangebreak-выреза на +1 мин, чтобы точка закрытия 23:00 не пряталась).
- ⚠️ **Историю НЕЛЬЗЯ бэкфилить** — стакан только живой (Finam не отдаёт
  исторический стакан).  График копится с момента первого запуска, не задним
  числом.
- Зарегистрирован в `main.py` (`_spawn(spb_spread_collector_loop())`).

### Хранение и выдача
- Таблица `spb_orderbook_spread(bucket, ticker, spread_1m_usd, spread_10m_usd)`
  — миграция `009` + inline в `init_db`.  Хранится **per-contract USD** (без
  курса), `NULL` = неликвид.  В **рубли** конвертируется на этапе запроса тем же
  `LEFT JOIN LATERAL moex_fx_rates` (USDRUBF forward-fill), что и всё остальное —
  единый источник курса.  `get_latest_usdrub()` нужен сборщику только чтобы
  отмерить V в рублях при проходе по стакану.
- `GET /api/spb/spread-history?days=7` → `fetch_spb_spread_history` → серии по
  тикерам `[{ticker, name, group, points:[{bucket, spread_1m_usd, spread_10m_usd, spread_1m_pct}]}]`.
- **`GET /api/spb/spread-live` (добавлено 09.07.2026)** — *мгновенный* AVG_SPREAD
  по всем тикерам из tick-by-tick кэша стакана (`compute_live_spreads` в
  `orderbook.py`), курс `get_latest_usdrub()`.  Для этого кэш стакана хранит
  **полную глубину** книги (срез до `_DEPTH=12` уровней делается в
  `get_cached_orderbooks`, а не при записи) — спред-математике нужна вся книга.
  Чтение продлевает активное окно фида (note_access), так что стримы живут,
  даже если поллится только график.

### Фронтенд
- Карточка Order Book: слева живой стакан СПБ, в центре два `SpreadChart`.  У
  **5 крипто-карточек добавлен 4-й столбец — живой стакан MOEX** (14.07.2026):
  порядок `[стакан СПБ | график $ | график % | стакан MOEX]`.  Рендеринг стакана
  вынесен в переиспользуемый `LiveBook` (терминальная подсветка изменившихся
  уровней); MOEX-стакан поллится с `/api/spb/moex-orderbook` в том же `load()`
  (`Promise.allSettled`, чтобы сбой MOEX не ломал SPB).  У US-акций 4-го столбца
  нет.
- `SpreadChart` (Plotly, `lines+markers`): линия SPB (1 млн ₽/сторона) + линия
  MOEX (красная) на 5 крипто-карточках; две копии графика — ось Y в $ и в
  **базисных пунктах** (15.07.2026; в БД/API процент как был — фронт умножает
  на 100, `toBps`); маркеры — чтобы серия из 1 точки была видна.
  `connectgaps:true` (14.07.2026) — линия **не рвётся** на ночных паузах/
  неликвиде (раньше `false` → `NULL` разрывал линию).  Верхняя плашка-методология
  убрана 14.07.2026.
- **График — только 15-мин точки**, live-хвоста НЕТ.  История перечитывается раз
  в **120с** (`SPREAD_REFRESH_MS`); опрос стакана (1с) на график не влияет.
  ⚠️ 29.07.2026 из-под графика убран весь live-слой, который здесь раньше
  описывался (клиентский буфер `LIVE_BUF_MAX`, трейсы-хвосты через
  `Plotly.restyle`, `splitSeries`, фоновый сэмплер `spb_spread_live_sampler_loop`
  + кольцевой буфер `_live_buf` + `GET /api/spb/spread-live-history`).  Фронт
  перестал их опрашивать ещё раньше, а сэмплер продолжал каждые 2с ходить в БД
  за курсом и считать VWAP по 25 книгам в никуда.  `GET /api/spb/spread-live`
  и `compute_live_spreads` **оставлены** — они считают по запросу и ничего не
  стоят вне вызова (удобно для отладки, симметрично MM).
- Интервал 120с выбран потому, что новая точка появляется не чаще раза в 15 мин,
  а `/spread-history?days=7` — это ~1.3 МБ JSON (у MM shares ~1.9 МБ).  На nginx
  включён `gzip` для `application/json` (~10×), см. `nginx/http-only.conf`.
- **Ось X — время МСК (UTC+3)**: Plotly не умеет таймзоны (рисует сырой UTC),
  поэтому все timestamps сдвигаются на +3ч на клиенте (`toMsk`) перед отрисовкой.

### Нюансы эксплуатации (важно при отладке «пусто»)
- **Пустой график = нет истории.** Live-точки больше нет (29.07.2026), поэтому
  на свежей БД первая точка появляется по закрытии первого 15-мин бакета.
  Плашка «нет данных» также у неликвидов, где на V не хватает глубины.
- **Число сэмплов в точке видно в логе**: строка `SPB spread: wrote 15-min point
  … (N samples/row avg, last sweep Xs)`.  Если `last sweep` подбирается к длине
  сетки сэмплирования — свип перестаёт укладываться, и интервалы между сэмплами
  начинают плавать (простое среднее по неравным интервалам смещается в сторону
  моментов, когда Finam отвечал быстро).  Лечится не «как-нибудь», а сеткой,
  в которую свип гарантированно влезает — см. `_SAMPLE_SEC` у MM.
- **Стакан прогревается лениво.** Дисплей-фид (`orderbook.py`, gRPC-стрим) живёт
  только пока `/orderbook` читают (окно 30с).  Сразу после `--force-recreate
  backend` кэш = плейсхолдеры («загрузка…»), заполняется за несколько секунд
  после того, как открыта страница.  Проверено: наполняется 25/25 при активном
  поллинге, параллельно с REST-коллектором спреда — оба на одном токене Finam,
  конфликтов нет.
- Бакеты пишутся в UTC; на границе часа возможен разрыв, если коллектор
  перезапускался внутри 15-мин интервала (частичный бакет теряется).

### MOEX-оверлей: спред фьючерсов на криптоиндексы (добавлено 14.07.2026)
На 5 крипто-карточках Order Book поверх линии SPB рисуется **вторая линия —
спред фьючерса MOEX** (та же методика, 1 млн ₽/сторона глубины, абсолют в $ и %).  Сравнение
эффективного спреда СПБ Биржи vs MOEX FORTS для одного и того же криптоактива.
- **Источник — Finam, MIC `RTSX`** (MOSCOW EXCHANGE - DERIVATIVES MARKET),
  символ `<SECID>@RTSX` (напр. `BTN6@RTSX`).  Тот же брокерский токен, что и SPB;
  `FinamClient.fetch_orderbook(secid, mic="RTSX")`.  Стакан отдаёт цену в **USD**,
  объём в контрактах — как у SPB.
- **Инструменты** (`MOEX_CRYPTO_FUTURES` в `moex/config.py`): SPB-тикер →
  (ISS assetcode, fallback-лот): BTC/ETH/SOL/XRP/TRX.  Коды контрактов на FORTS:
  BT/EH/S3/XR/TX (напр. `BTN6` = BTC-7.26).
- **Фронт-месяц резолвится динамически из ISS раз в сутки**
  (`resolve_front_secids` в `moex/fetcher.py`): самый ликвидный одиночный
  контракт (макс. VOLUME, не календарный спред) на последний торговый день —
  контракты FORTS роллятся ежемесячно, хардкод SECID сломался бы на экспирации.
- **Лот вычисляется динамически из оборота** (НЕ хардкод):
  `lot = VALUE / (VOLUME × SETTLEPRICE × usdrub)`, округление до 1 значащей цифры
  (`_snap_lot`).  Даёт `BTC 0.001 / ETH 0.01 / SOL 1 / XRP 100 / TRX 100`.  Лот
  критичен: он задаёт глубину набора 1 млн ₽/сторона; заниженный лот → 1 млн не
  набирается по стакану → нет линии.  Значения в конфиге — только fallback, если
  ISS-расчёт не удался.  ⚠️ Finam отдаёт turnover для MOEX **в рублях** (у SPB — в
  USD); на расчёт спреда это не влияет (спред читает price×size из стакана), важно
  только при выводе лота.
- **Сбор** — в том же `spb_spread_collector_loop` (тот же клиент/троттлинг),
  после 25 тикеров SPB опрашиваются 5 MOEX-контрактов; только цель 1 млн ₽.
  Таблица `moex_orderbook_spread(bucket, ticker, spread_1m_usd, spread_1m_pct)`,
  `ticker` = SPB-тикер (для оверлея на ту же карточку).  Миграция `011`.
  `upsert_moex_spread_buckets` / `fetch_moex_spread_history` (абсолют в $ без
  конвертации; курс не применяется — 15.07.2026).
- **API**: `GET /api/spb/spread-history` добавляет массив `moex` в объект каждого
  из 5 крипто-тикеров (`[{bucket, spread_1m_usd, spread_1m_pct}]`).
- **Фронт** (`SPBOrderBook.tsx`): `SpreadChart` рисует вторую линию (красная,
  `MOEX_COLOR`) при наличии `moex`; легенда показывает **SPB / MOEX**.  У
  US-карточек `moex` нет — линия и легенда не появляются.
- ⚠️ Смена лота требует пересчёта: при изменении методики лотов
  `TRUNCATE moex_orderbook_spread` (иначе смешаются старые/новые значения).

## MM (Market-Maker) FORTS Integration (добавлено 17.07.2026)

Отдельная группа вкладок **MM** в боковом меню (рядом с Cryptoexchanges / SPB).
Для **ближайшего срока (front-month)** фьючерса каждого базового актива MOEX
FORTS — живой стакан + два графика спреда-на-объём (абсолютный **в валюте
котировки** и в базисных пунктах). Дизайн **точно как на вкладке SPB Order
Book**. Классификация и коды контрактов берутся из **ISS MOEX**, живые стаканы —
из **Finam TradeAPI** (тот же брокерский токен, MIC `RTSX`, что и крипто-оверлей
на SPB-странице).

### Вкладки = коллекции ISS `securitygroups/futures_forts/collections`
`MM_GROUPS` в `backend/app/mm/config.py` — 4 группы:
- `index`     → Фьючерсы на индексы (`futures_forts_index`)
- `shares`    → Фьючерсы на акции (`futures_forts_shares`)
- `currency`  → Фьючерсы на валюты (`futures_forts_currency`)
- `commodity` → Фьючерсы на товарные контракты (`futures_forts_commodity`)

⚠️ **ОФЗ и Проц. ставки НЕ подключены**: `futures_forts_ofz` — фьючерсы на ОФЗ
делистнуты (последние торги 06.2022, живых контрактов нет вообще);
`futures_forts_interest` — только 2 контракта (RUONIA / 1-мес. ставка) с `OI=0`.
Добавление группы = одна строка в `MM_GROUPS` + один пункт в `MM_TABS`
(`frontend/src/components/Header.tsx`), но при пустой/неликвидной коллекции
вкладка будет пустой.

### Universe (ISS, раз в сутки — `backend/app/mm/universe.py`)
`build_universe()` за 5 ISS-запросов (по одному на коллекцию + один снапшот
рынка FORTS) резолвит по каждому базовому активу (ASSETCODE):
- **front-month SECID** — ближайшая экспирация среди `TYPE=='futures'` и
  `IS_TRADED==1` (календарные спреды `futures_spread` отсекаются штатным полем
  TYPE, а не по длине строки);
- **`step_ratio = STEPPRICE / MINSTEP`** — рублёвая стоимость одного пункта цены
  на контракт (ISS пересчитывает STEPPRICE ежедневно, она **уже включает лот и
  курс**);
- **валюта котировки** — `quote_symbol()`: **сначала** текст ISS `UNIT` («В
  пунктах» → пт), и только потом код `FACEUNIT` (`RUB`→₽, `USR`→$, `EUR`→€,
  `JPY/CNY`→¥, …).
  ⚠️ Порядок именно такой (исправлено 29.07.2026): у индексных фьючерсов
  `FACEUNIT` называет валюту **расчётов**, а не котировки — у RTS/RVI там `USR`,
  у MIX/RGBI `RUB`, у MOEXCNY `CNY`, тогда как котируются они все **в пунктах**.
  Пока FACEUNIT имел приоритет, спред RTS в 25.8 пункта подписывался как
  «25.8 $», а ветка «пт» была недостижима (FACEUNIT есть почти всегда).  На
  сами числа это не влияло — только на подпись оси и шапку карточки.
  У «лишних» активов (`MM_EXTRA_ASSETS`) FACEUNIT/UNIT берутся отдельным
  запросом `securities/<SECID>.json` (`_security_units`) — в рыночном снапшоте
  этих полей нет;
- **фильтр ликвидности** — оставляем инструмент, только если рублёвый OI
  (`OPENPOSITION × price × step_ratio`) ≥ `MM_MIN_OI_RUB` (10 млн ₽, tunable).
  Мёртвые стаканы не стримятся. ~102 ликвидных инструмента (index 10 / shares 66
  / currency 11 / commodity 15).

⚠️ **ISS-коллекция не полна: `MM_EXTRA_ASSETS`** (29.07.2026).  Новый фьючерс на
нефть **WTI** (`WTQ6` = WTI-8.26, листинг 13.07.2026, котировка в $/баррель,
GROUPTYPE «Товары») есть в снапшоте рынка FORTS и торгуется, но в коллекцию
`futures_forts_commodity` ISS его **не положил** — обход коллекций его не видел.
`MM_EXTRA_ASSETS = {ASSETCODE: group_id}` в `mm/config.py` перечисляет такие
активы; `_extra_front_months()` резолвит им фронт-месяц прямо из снапшота (тот же
фильтр экспирации + тот же порог ликвидности), FACEUNIT/UNIT дотягивается одним
запросом `/iss/securities/<SECID>`.  Дубль исключён: если ISS однажды добавит
актив в коллекцию, extra-строка отбрасывается.  Добавить ещё один пропущенный
актив = одна строка в `MM_EXTRA_ASSETS`.

⚠️ **В день экспирации фронт-месяц может выпасть**: инструмент отсеивается
порогом ликвидности, когда у ближней серии уже `OI=0` (наблюдалось у NG/NGM
29.07.2026), и возвращается на следующий день со следующей серией.

⚠️ **Не всё есть в коллекциях ISS**: `MM_EXTRA_ASSETS` (`backend/app/mm/config.py`)
— ASSETCODE → группа для активов, которых в коллекции нет, но которые торгуются
на FORTS и видны в рыночном снапшоте.  Сейчас там `WTI` (нефть ВТИ, листинг
13.07.2026, котируется в $/барр.): `futures_forts_commodity` его не содержит,
обход коллекций молча терял бы инструмент.  Front-month / step_ratio / порог
ликвидности для них считаются из того же снапшота (`_extra_front_months`).

⚠️ **ISS-коллекция пагинируется по ~100 строк и игнорирует большой `limit`**
(500→100 строк): `_collection_front_months` обходит страницы через `start` до
короткой/пустой страницы.  Коллекция `shares` — ~2500 строк (у каждого базового
актива десятки истёкших серий); один запрос молча обрезал её первой страницей и
терял базовые активы за пределами (NASD/SPYF/NIKK/HANG/STOX и др.).  Исправлено
17.07.2026 — было 26 shares, стало 66.

Кэш обновляется раз в UTC-сутки (`ensure_universe()`); неудачный ребилд
сохраняет последний хороший.

### Методология спреда (та же, что на SPB Order Book)
**Спред-математика переиспользуется без изменений** из `app.spb.orderbook`:
вызывается `avg_spread_on_volume(bids, asks, lot=step_ratio, usdrub=1.0,
target=1_000_000)`. Подмена `lot=step_ratio, usdrub=1.0` заставляет
`_vwap_to_notional` мерить глубину `price × size × step_ratio` (в рублях, любая
номинация) и вернуть спред в **родной единице цены**:
- Глубина набора — **1 млн ₽ на каждую сторону** (bid и ask), проход по стакану.
- **Абсолют** = `P_ср_ask − P_ср_bid` в валюте котировки (₽ / $ / ¥ / пункты),
  хранится и отдаётся **как есть, без конвертации**. Никаких `×лот`/`×курс` —
  `step_ratio` уже всё содержит.
- **Проценты / б.п.** = `(P_ср_ask − P_ср_bid) / mid × 100`, `mid` = середина
  лучших котировок. В БД хранится как %, фронт умножает на 100 → б.п.
- `None`, если у стороны нет 1 млн ₽ глубины (график мостит разрыв `connectgaps`).

### Live-фид (`backend/app/mm/orderbook.py`)
gRPC-стрим Finam (`<SECID>@RTSX`, tick-by-tick) с REST-фолбэком — как SPB, но
**ленивый по активной вкладке**: стримится только группа, чью вкладку читали в
последние 30 с (`note_access(group_id)`), при смене вкладки набор стримов
пересобирается (`_active_key`). Так открытие «Валют» не поднимает 26 стримов
акций. Кэш в памяти, API отдаёт мгновенно.
⚠️ Кэш хранит **полную** книгу (спред-математике нужна вся глубина), а на выдачу
идёт срез до `_DEPTH=12` уровней — **только** в `get_cached_orderbooks`, как у
SPB (29.07.2026).  Без среза вкладка «Акции» отдавала ~135 КБ на каждый
секундный поллинг ради 8 отрисованных уровней.

### История (персистентная — `backend/app/mm/spread_etl.py`)
`mm_spread_collector_loop` снимает стакан **всех** ликвидных MM-инструментов в
торговые часы (переиспользует `_is_trading_now` / `_bucket_start` /
`_next_minute` из `spb.spread_etl` — единое торговое окно) и пишет **простое
среднее** сэмплов одной 15-мин точкой (метка = конец интервала). ⚠️ Историю
**нельзя бэкфилить** (Finam отдаёт только живой стакан) — копится с первого
запуска.
- ⚠️ **Сетка у MM — 120с, а не 60с как у SPB** (`_SAMPLE_SEC` в
  `mm/spread_etl.py`, 29.07.2026).  Свип ~100 инструментов = 50с одного только
  троттлинга плюс ~100 round-trip'ов к Finam, в минуту он НЕ укладывается.  На
  60с-сетке он то успевал, то нет → интервал между сэмплами плавал 60/120с, а
  простое среднее по неравным интервалам смещено в пользу моментов, когда Finam
  отвечал быстро (плюс расходились локальная и прод инстанции).  Сетка, в
  которую свип влезает всегда, возвращает и равномерность, и epoch-выравнивание.
  900с не делится на 120с → бакеты чередуют **8 и 7 сэмплов** (15 за полчаса).
  Проверять по логу `last sweep Xs`; если устойчиво <55с — можно вернуть 60.
- Оба коллектора (SPB и MM) — параллельные lifespan-таски на одном токене:
  внутри свипа строго последовательно, но процесс в сумме шлёт ~2 REST-вызова за
  интервал троттлинга.  429 на этом режиме не наблюдалось.

### Хранение и эндпоинты
- Таблица `mm_orderbook_spread(bucket, ticker, group_id, spread_abs, spread_pct)`
  — миграция `012` + inline в `init_db`. `spread_abs` в валюте котировки (без
  конвертации), `spread_pct` — %.  `upsert_mm_spread_buckets` /
  `fetch_mm_spread_history(group, days)` в `timescale.py`.
- `GET /api/mm/groups` — 4 группы + счётчики ликвидных инструментов.
- `GET /api/mm/orderbook?group=` — живой стакан группы (продлевает окно фида).
- `GET /api/mm/spread-history?group=&days=` — 15-мин история (+ поле `currency`).
- `GET /api/mm/spread-live?group=` — мгновенный спред из кэша (бэкенд-эндпоинт
  есть, но **фронт его НЕ опрашивает** — график намеренно только 15-минутный).
- `POST /api/mm/refresh` — форс-ребилд universe (ролл фронт-месяца, новые листинги).
- Регистрация в `main.py`: `_spawn(mm_orderbook_poll_loop())` +
  `_spawn(mm_spread_collector_loop())` + `include_router(mm.router, prefix="/api/mm")`.

### Фронтенд
- `frontend/src/components/OrderBookViz.tsx` — **общий** компонент: `LiveBook`
  (терминальный стакан с подсветкой) + хелперы оси X (`toMsk`, `tradingBreaks`,
  `tradingTicks`, `spreadTheme`). SPB Order Book **переведён на него** (дублей
  больше нет).
- `frontend/src/pages/MM.tsx` — параметризованная группой страница: карточка
  `[стакан | график абсолют | график б.п.]`. **График = только 15-мин точки**
  (методология СПБ), стакан сверху — живой (поллинг 1 с). Абсолютная ось
  подписывается валютой инструмента (`$` префиксом, остальное — суффиксом).
- Навигация — `MM_TABS` в `Header.tsx`, маршрутизация в `App.tsx`
  (`page` вида `mm-<group>`).

### Прод
Задеплоено 17.07.2026 на `176.12.70.128`: таблица
создаётся автоматически через `init_db`, бэкфила нет — 15-мин линия наполняется
в торговые часы.

## Hourly Volume — часовой оборот (31.07.2026)

Вкладка **Cryptoexchanges → Hourly Volume**: часовой оборот перпов в рублях,
**только криптобиржи**.  Два вида на одном переключателе:
- **Профиль** (по умолчанию) — средний оборот в каждый час суток за 30 дней:
  в какие часы идёт ликвидность, дневной шум усреднён.
- **Ряд** — час за часом за последние 7 дней: видны конкретные всплески.

### Почему только крипта
Проверено 31.07.2026 по всем источникам:
- **ccxt `fetch_ohlcv('1h')`** работает на всех 6 биржах — точный оборот.
  ⚠️ OKX отдаёт **300 баров на страницу** против 1000 у остальных, поэтому
  пагинация идёт «пока последний бар двигается вперёд», а не по длине страницы.
- **MOEX ISS**: часовые свечи (`interval=60`) есть, но **`value` там всегда 0** —
  рублёвого оборота во внутридневных свечах нет (и в дневных свечах тоже; рубли
  живут только в history-эндпоинте).  Чтобы добавить MOEX, надо раскладывать
  точный дневной VALUE по долям часового объёма в контрактах.
- **SPB (Finam)**: `TIME_FRAME_H1`/`M15` работают, но бары несут только
  контракты → оборот пришлось бы приближать, как в дневном ETL.

### Хранение и ETL
- Таблица `ohlcv_hourly` — та же форма, что `ohlcv_daily`, **отдельно от неё**:
  все существующие volume-запросы читают `ohlcv_daily` без фильтра по
  гранулярности, и смешанная таблица задваивала бы обороты во всех них.
  Миграция `015` + inline в `init_db`.
- **Ретеншен 90 дней**, компрессии нет: ETL upsert'ит свежие часы на каждом
  проходе, а компрессированные чанки превращают это в цикл
  decompress-rewrite — при таком числе строк экономия того не стоит.
  `HOURLY_BACKFILL_DAYS` не должен превышать окно ретеншена, иначе ETL пишет
  строки, которые тут же удаляет retention job (`hourly_since` это гарантирует).
- `hourly_backfill_loop` (`backend/app/backfill/hourly.py`) — раз в час; первый
  проход тянет 90 дней, дальше только `latest − 6h`.
- ⚠️ **Один ccxt-инстанс на биржу, а не на пару** (`group_by_exchange`): на
  инстанс-на-пару полный проход занимал ~20 мин, потому что `load_markets()`
  вызывался ~170 раз.  Биржи идут параллельно, символы внутри биржи —
  последовательно.  Спот и перп на одной бирже — **разные бакеты** (им нужны
  разные ccxt-инстансы).
- Парсер баров общий с дневным бэкфиллом (`parse_ohlcv_batch`), поэтому
  MEXC-бары в будущее отсекаются той же проверкой.

### Часовой пояс
Хранение в UTC, **отображение в МСК** — как на графиках спреда.
- **Ряд**: бэкенд отдаёт UTC-таймстемпы, подпись строит фронт
  (`mskHourLabel` в `frontend/src/utils/hourly.ts`, сдвиг +3 ч через UTC-геттеры,
  чтобы график не менял подписи в зависимости от таймзоны ноутбука).
- **Профиль**: час — это КЛЮЧ группировки, поэтому он считается в SQL
  (`EXTRACT(hour FROM ts AT TIME ZONE 'Europe/Moscow')`); фронт после агрегации
  сдвинуть его уже не может.
- Среднее в профиле берётся по дням, где бар реально есть (`AVG`, а не
  сумма/30), иначе инструмент с коротким листингом занижался бы нулями.

### Эндпоинты
Оба отдают **колоночный** формат (`pivot_to_series`): общая ось + массив
значений на пару symbol×exchange, `null` = бара не было (это не ноль).
- `GET /api/history/hourly-volume?days=7` (1–30) → `{days, axis:[ts…], series:[{symbol, exchange, values}]}`
- `GET /api/history/hourly-profile?days=30` (1–90) → `{days, axis:[0…23], series:[…]}` (ось — час МСК)
- Оба под `@ttl_cache()`; в рубли переводятся тем же `LEFT JOIN LATERAL moex_fx_rates`.
- ⚠️ У обоих запросов есть верхняя граница по времени — без неё планировщик
  локает все чанки гипертаблицы (см. готчу про chunk locking).

### Часовой профиль на TradFi Market Share (03.08.2026)
У страницы **TradFi Market Share** третий режим переключателя — **Hourly**
(`View = 'daily' | 'weekly' | 'hourly'`): те же 6 графиков + два нижних по
акциям, но по оси X — часы суток МСК (00:00…23:00), значение — средний оборот за
этот час.  Сырьё/металлы/индексы приходят из `/api/history/hourly-profile?days=30`
(таблица `ohlcv_hourly`), вся вселенная equity-перпов — из **своего часового ETL**
(см. следующий раздел).
- Колоночный ответ разворачивается в те же `TradFiRow` (`fetchHourlyProfileRows`),
  ключ бакета — час с ведущим нулём (`'00'…'23'`), чтобы обычная строковая
  сортировка по `date` во всех графиках осталась верной.  Подпись даёт
  `bucketLabel(bucket, view)`, который в часовом режиме зовёт `hourTick`.
- В часовом режиме **один и тот же набор строк** идёт и в графики 1–4, и в 5–6:
  `nonStockTradfiRows` и так оставляет только сырьё/металлы/QQQ/SPY, то есть
  фильтрует крипту ровно как `/tradfi-volume` на бэкенде.  Отдельный
  «tradfi-only» часовой эндпоинт не нужен.
- ⚠️ **Корейские акции больше не считаются криптой** (03.08.2026): `getAssetGroup`
  отправлял всё, чью секцию он не знает, в `Cryptocurrencies`, а у секции
  `Korean Market` своего бакета не было — SKHYNIX/SAMSUNG/HYUNDAI сидели в
  крипто-слое (233 ₽B/сутки) и одновременно считались ВТОРОЙ раз внутри US Market
  через stock-ETL carrier.  Починено в двух местах: секция `Korean Market` мапится
  в `US Market`, и `assetGroupRows` выбрасывает её из ohlcv-ветки как и остальные
  акции.  Действует на все три режима.  Проверка: столбец 16:00 на Asset Group
  Volume стал 480 ₽B вместо 493.
- Акции подмешиваются ровно как в дневном режиме — из `/api/stocks/volume?period=hourly`
  в том же формате бакетов, поэтому `mergedByExchange` / `mergedByInstrument` /
  `assetGroupRows` и оба нижних графика работают без изменений.
- Проп всех графиков — `view: View`, а не прежний `weekly: boolean`.
- MOEX в часовом режиме нет (как и в остальных — страница фильтрует
  `exchange !== 'moex'`); СПБ Биржи тоже нет.

## Топ-100 крипты в группе Cryptocurrencies (03.08.2026)

На графиках 5–6 (Asset Group) слой **Cryptocurrencies** раньше состоял ровно из
трёх курируемых мажоров (BTC/ETH/SOL) из `instruments`, тогда как Commodities и
US Market покрывали свои вселенные целиком — крипта была структурно занижена.
Теперь слой берётся из **топ-100 бессрочных фьючерсов по обороту на КАЖДОЙ бирже**.

- `backend/app/crypto/config.py` — что считается криптоперпом.  Отбрасываются:
  не-swap и не-USD-котируемые (coin-margined считают оборот в другой единице),
  equity по флагу биржи (`stocks.config.is_equity`) и всё из `NON_CRYPTO_BASES`
  (сырьё/металлы/индексы/акции, тот же список, что у Futures Launches).
  ⚠️ Базы с **namespace** (`XYZ-GOLD`, `CASH-NVDA` — builder-DEX'ы Hyperliquid)
  отбрасываются целиком: обычные криптоперпы там никогда не с префиксом, а вот
  реальные активы под неизвестным префиксом иначе прошли бы как «крипта».
- Ранжирование — по 24-часовому обороту из `fetch_tickers()`, пересчитывается
  каждый проход (топ-100 постоянно тасуется).  ⚠️ У части площадок `quoteVolume`
  пустой → фолбэк `baseVolume × last`, иначе биржа схлопывается до горстки
  рынков, которые это поле заполняют.
- `backend/app/crypto/etl.py` — два прохода на одном обходчике: **дневной**
  (раз в 6 ч, бэкфилл с `BACKFILL_SINCE=2026-01-01` — нужен для Weekly YTD) и
  **часовой** (раз в час, 30 дней).  Запись потоковая батчами по 20 000 строк
  (см. готчу про OOM у `stock_hourly_volume` — здесь ~600 пар × 720 часов).
  Старт отложен на 240/300 с после буста: `load_markets()` + `fetch_tickers()`
  посреди загрузочной толчеи ловят от Hyperliquid 429.  Упавшая биржа не рушит
  проход — за ней остаётся её часть прошлой вселенной.
- Таблицы `crypto_top_daily_volume(date, exchange, symbol, quote_usd)` и
  `crypto_top_hourly_volume(hour, …)` (гипертаблица, ретеншен 45 дней), миграция
  `017` + inline в `init_db`.  `symbol` — каноническая БАЗА (BTC, DOGE…), чтобы
  монета агрегировалась между биржами.  Отдельно от `ohlcv_*` по той же причине,
  что и акции: те таблицы кормят страницы с карточкой на инструмент.
- `GET /api/crypto-top/volume?period=daily|weekly|hourly` → `{period, by_exchange}`
  (только разрез по биржам — графики его всё равно суммируют).
  `POST /api/crypto-top/refresh` — ручной прогон обоих проходов.
- ⚠️ **Курируемые BTC/ETH/SOL выбрасываются из ohlcv-ветки, как только новый
  источник непуст** (`assetGroupRows`, флаг `hasCryptoTop`): топ-100 их содержит
  по определению, иначе двойной счёт.  Пока таблица пуста — слой по-прежнему
  считается по трём мажорам, так что страница не ломается до первого прогона.

## Часовой оборот фондовых перпов (`stock_hourly_volume`, 03.08.2026)

Часовой близнец `stock_daily_volume`: та же вселенная ~520 equity-перпов, но с
часовой гранулярностью — иначе в группе **US Market** часового профиля были бы
только QQQ/SPY (в `ohlcv_hourly` из акций лежат лишь 8 курируемых имён).

- **Отдельная таблица, а не `ohlcv_hourly`**: `ohlcv_hourly` кормит страницу
  Hourly Volume, которая рисует по карточке-графику на КАЖДЫЙ символ — 200 лишних
  тикеров превратили бы её в 200 карточек и раздули payload.
- `stock_hourly_volume(hour, exchange, ticker, quote_usd)` — гипертаблица,
  уникальный индекс `(hour, exchange, ticker)`, **ретеншен 45 дней** (страница
  усредняет по 30).  Миграция `016` + inline в `init_db`.  Компрессии нет по той
  же причине, что у `ohlcv_hourly` (ETL перезаписывает свежие часы каждый проход).
- `backend/app/stocks/hourly_etl.py` — раз в час, `BACKFILL_DAYS=30` на пустой
  таблице и `latest − 6h` дальше; `quote_usd = close × volume × contractSize`
  (contractSize важен только для MEXC), бары в будущее отбрасываются (MEXC).
  Пагинация «пока последний бар двигается вперёд» — у OKX 300 баров на страницу.
  ⚠️ Вселенная берётся из общего кэша `build_stock_universe()` (TTL 6 ч), НЕ
  своим `load_markets()`: второй обход бирж через секунды после дневного ETL —
  это ровно то, от чего Hyperliquid отдаёт 429.
  ⚠️ Старт цикла отложен на `STARTUP_DELAY_SEC=180`, чтобы не встретиться на
  бусте с дневным stock ETL и часовым бэкфиллом крипты.
  ⚠️ **Пишет потоково, батчами по `_FLUSH_ROWS=20_000`** — первая версия копила
  весь проход в dict и upsert'ила одним куском в конце: 865 пар × 720 часов ≈
  620k строк, это +175 МБ и растёт, при `mem_limit: 1g` у бэкенда и ~740 МБ
  занятых на старте.  Проход шёл прямо в OOM-kill, наблюдалось на проде
  (869 → 913 МБ за 4 минуты).  С батчами пик в полёте ≈ 6 × 20k строк, замер на
  проде — 599 МБ ровно.  Пагинация одного инструмента всегда завершается до
  сброса, поэтому значение часа не разрезается между двумя upsert'ами.
  Первый 30-дневный проход на проде: **549 341 строка за 300 с**.
- `GET /api/stocks/volume?period=hourly` → тот же `{by_exchange, by_instrument}`,
  что daily/weekly, но `bucket` = `'00'…'23'` (с ведущим нулём — чтобы строковая
  сортировка на фронте осталась верной), `bucket_label` = `'13:00'`.
  Ручной прогон: `POST /api/stocks/hourly-refresh`.  ⚠️ Сразу после рестарта
  контейнера он бесполезен: кэш вселенной пуст, ETL идёт своим `load_markets()`
  в разгар загрузочной толчеи и получает от Hyperliquid 429 (наблюдалось дважды
  подряд 03.08.2026 — вместе с ним падал и дневной stock ETL).  Дожидаться строк
  `Stock ETL: … → N company-stock perps` в логе.  ⚠️ После неудачного discovery
  часовой цикл ждёт следующего часа, дневной — 6 ч; короткого ретрая нет.
- ⚠️ **Усреднение тут SUM-затем-AVG, а не AVG-затем-SUM** (`fetch_stock_hourly_profile`):
  сначала сумма по (день × час × серия), потом среднее по дням.  Именно это и
  значит «средний оборот биржи в этот час»: в день, когда тикер ещё не был
  листнут, биржа реально торговала меньше, и ноль там — правда, а не разбавление.
  Крипто-профиль (`fetch_hourly_profile_rub`) устроен наоборот — среднее по паре,
  сумма на фронте; при полном покрытии оба дают одно и то же.

## Секции инструментов и топ-10 акций (30.07.2026)

Секции карточек на Weekly Performance / Daily Volume / Open Interest задаёт
`SYMBOL_SECTIONS` + `classifySymbol` в `frontend/src/types/index.ts`.

- **Indexes** — новая секция для `QQQ`/`SPY` (вынесены из `US Market`).  На
  TradFi Market Share они по-прежнему считаются группой «US Market»
  (`getAssetGroup` явно мапит `Indexes → US Market`), иначе графики 5–6
  поменяли бы методику.
- **Порядок карточек внутри секции — по величине, а не по алфавиту**
  (30.07.2026, `frontend/src/utils/rank.ts`): Weekly Performance — по обороту
  YTD, Daily Volume — по обороту за 30 дней, Open Interest — по ПОСЛЕДНЕМУ
  значению OI (сумма по дням награждала бы инструменты с длинной историей, а не
  крупные).  Равные значения — по алфавиту, чтобы вёрстка не прыгала.
- **US Market = топ-10 equity-перпов по обороту за последнюю ПОЛНУЮ ISO-неделю**,
  а не курируемые 8 акций из `instruments`.  Ранжирование —
  `fetch_top_stock_tickers` (`timescale.py`) по `stock_daily_volume`; полная
  неделя, чтобы набор не перетасовывался в течение дня; фолбэк на последние 7
  дней с данными на пустой БД.  Корейские тикеры исключены из ранжирования —
  у них своя секция.
- Обороты этих акций берутся **из stock ETL**, а курируемые US-акции
  **выкинуты из ohlcv-ветки** `fetch_weekly_adtv_rub` / `fetch_daily_volume_rub`
  (параметр `exclude_bases=US_STOCK_CURATED_BASES`) — иначе AAPL считался бы
  дважды.  Та же методология, что на TradFi Market Share (`nonStockTradfiRows`).
- Набор тикеров ротируется, поэтому фронт не может держать их в хардкоде:
  `GET /api/history/us-stock-tickers` отдаёт список, страницы передают его
  вторым аргументом в `classifySymbol(sym, usStocks)`.
- **OI по этим акциям** пишется в ту же таблицу `open_interest` под каноникой
  `<TICKER>/USDT:USDT` — миграция не нужна, `instruments`/aliases не трогаются.
  `_stock_work_items` в `backend/app/oi/etl.py` резолвит биржевые символы через
  `build_stock_universe()` (кеш на 24 ч — там `load_markets()` по всем биржам) и
  пропускает тикеры, которые уже покрыты курируемым инструментом.
  ⚠️ Истории OI у Hyperliquid/MEXC нет, у Binance/OKX/Bybit бэкфилл ≤90 дней →
  у новичка в топе ряд начинается с момента первого прогона.
  ⚠️ `GET /api/open-interest/daily` отфильтровывает акции вне текущего топ-10
  (`_dropped_stock_bases`), иначе прошлые лидеры копились бы лишними карточками.
- `TOP_STOCKS_DISPLAYED` (`backend/app/stocks/config.py`) — единственное место,
  где задано число 10.
- **Тикеры, которые НЕ должны занимать слот US Market** (30.07.2026): `KOREAN_TICKERS`
  (`stocks/config.py`) исключаются из ранжирования, плюс всё из `EXCLUDE` —
  ранжирование смотрит и туда, потому что исключение тикера из вселенной НЕ
  удаляет уже записанные строки (иначе `DRAM` держал бы слот ещё недели).
  - `DRAM` добавлен в `EXCLUDE`: все биржи помечают его `EQUITY`/`stock`, но это
    ETF на корзину производителей памяти, а не компания.  ⚠️ Исторические строки
    в `stock_daily_volume` остаются и продолжают попадать в графики акций на
    TradFi Market Share, пока их не удалить вручную.
  - `SKHY` — ADR-контракт SK Hynix, который биржи ведут рядом с локальным
    `SKHYNIX` (первый помечен `EQUITY`, второй `KR_EQUITY`).  Показывается в
    секции **Korean Market**, а не US Market: тикер в `KOREAN_TICKERS` +
    `_CLS_KOREAN` (бэк) и в `SYMBOL_SECTIONS.Korean Market` (фронт).  Чтобы его
    строки вообще доехали до графиков (запросы просят только топ-N), он
    перечислен в `EXTRA_DISPLAYED_TICKERS`.  На карточке подпись
    `SKHY/USDT PERP · ADR` — из `SYMBOL_NOTES` в `frontend/src/types/index.ts`.

## Open Interest — источники и охват (30.07.2026)

Одна таблица `open_interest(ts, exchange, symbol, oi_contracts, oi_usdt)` для
ВСЕХ источников; `oi_usdt` всегда в USD, в рубли переводится на выдаче тем же
`LEFT JOIN LATERAL moex_fx_rates`, что и обороты.

- **Охват — всё, что отслеживает приложение**: курируемые `instruments` +
  ВСЯ вселенная equity-перпов из stock ETL (~200 тикеров × 6 бирж) + MOEX.
  ⚠️ Вселенную OI-сборщик берёт из **общего кэша** `build_stock_universe()`
  (`stocks/etl.py`, TTL 6 ч), а не строит сам: второй проход `load_markets()`
  по всем биржам через секунды после stock ETL стабильно ловил
  `hyperliquid 429` → у сборщика не оказывалось вселенной и он собирал только
  курируемые инструменты (наблюдалось на проде 30.07.2026).
  Страница Open Interest при этом остаётся компактной: `/api/open-interest/daily`
  отдаёт только топ-10 акций (`_dropped_stock_bases`), а полный охват нужен
  Custom Report.  Полный цикл опроса ≈ несколько минут при `POLL_INTERVAL=1800`.
- **Bitget**: ccxt возвращает `openInterest=None`; значение лежит в
  `info['openInterestList'][0]['size']` (контракты), USD считается через тикер
  (общий фолбэк в `_poll_ccxt`).  `fetchOpenInterestHistory` — `NotSupported`,
  поэтому история копится только с момента запуска, как у MEXC/Hyperliquid.
- **MOEX FORTS** (`backend/app/moex/oi_etl.py`, каждые 6 ч): у биржи нет API,
  который мы опрашиваем, но ISS публикует `OPENPOSITION` (контракты) и
  `OPENPOSITIONVALUE` (рубли) по каждой серии и дню.  По каждому активу из
  `ASSET_ISS_CODE` суммируем ВСЕ его серии (OI размазан по живым контрактам —
  один фронт-месяц занизил бы) тем же дискавери SECID, что и обороты, и пишем
  как `exchange='moex'` под каноникой из `ASSET_TO_CANONICAL`.
  ⚠️ ISS даёт рубли, а таблица хранит USD → делим на USDRUBF того же дня; на
  выдаче запрос умножает обратно, так что на графике ровно то, что опубликовал
  ISS.  Цифры **односторонние** (как публикует MOEX), в отличие от СПБ, где OI
  сознательно удваивается (`_SIDES = 2`).
  Ручной прогон: `POST /api/open-interest/moex-refresh`.
- **XRP/TRX исключены из OI** (`OI_EXCLUDED_ASSETS` в `moex/config.py`,
  30.07.2026): они торгуются ТОЛЬКО на MOEX, поэтому карточка OI сводилась к
  одному столбику без сопоставления с биржами.  Обороты по ним остаются.
- **Custom Report**: дерево для метрики `open_interest` теперь включает ветку
  MOEX forts, а equity-перпы уходят в класс «US stocks» — `_crypto_class(sym,
  equity)` принимает множество тикеров из `stock_daily_volume`, иначе
  `AVGO/USDT:USDT` выглядел бы как альткоин.

## Stocks Integration (фондовые перпы криптобирж, добавлено 08.07.2026)

Оборот торгов **вечными фьючерсами на акции** (equity perps) на **Binance, OKX,
Bybit, MEXC, Hyperliquid** — недельный и дневной, в рублях.  Показывается двумя
графиками (по биржам / по инструментам топ-20 + «Прочее») **внизу вкладки
Market Share (TradFi)** — они реагируют на существующий переключатель
**Daily/Weekly**.  865 инструментов, 272 уникальных тикера (замер 03.08.2026;
вселенная растёт — на 08.07.2026 было ~520/~207).

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

## Crypto Index (реплика методики MOEX, добавлено 29.07.2026)

Вкладка **Cryptoexchanges → Crypto Index** — индекс по BTC/ETH/SOL/TRX/XRP,
считаемый по методике MOEX: раз в 15с опрашиваются Binance/Bybit/OKX/Bitget
(спот к USDT), по каждой бирже берётся среднее за окно 60с, композит =
взвешенное среднее (**Binance 50 · Bybit 20 · OKX 15 · Bitget 15**), при выпадении
биржи её вес исключается и веса перенормируются.

### ⚠️ Это НЕ часть бэкенда трекера
Сборщик — **сторонний пакет** (`services/crypto-index/` в репо, бывш.
`handoff-crypto-index/` в корне — перенесён 31.07.2026), развёрнутый
**отдельным systemd-сервисом на хосте прода**, со своей SQLite-базой.  Ни FastAPI
трекера, ни TimescaleDB он не трогает; падение одного не влияет на другое.
- Код на проде: `/opt/crypto-index` (venv `./venv`), база
  `/var/lib/crypto-index/index.db` (WAL, **ретеншена нет** — растёт ~2–3 ГБ/год,
  следить за диском, см. [[prod-disk-full-outage]]).
- Юнит `crypto-index.service` (`enabled`), `User=www-data`,
  `uvicorn app:app --host 0.0.0.0 --port 8787`.  Логи: `journalctl -u crypto-index -f`.
- **Почему 0.0.0.0, а не 127.0.0.1** (как в INTEGRATION.md пакета): nginx у нас
  работает **в контейнере**, его `127.0.0.1` — это он сам.  Порт закрыт снаружи
  правилом ufw `8787/tcp ALLOW IN 172.16.0.0/12` (только docker-подсети);
  проверено curl'ом снаружи — `000`.
- nginx (`nginx/http-only.conf`): `location /crypto-index/` →
  `http://172.18.0.1:8787/` (шлюз сети `app_default` = хост; завершающий слэш
  срезает префикс) + редирект `/crypto-index` → `/crypto-index/`.
  ⚠️ Конфиг **бинд-маунтится в контейнер файлом** — синхронизировать только
  `rsync --inplace` (обычный rename оставит контейнер на старом inode).
  ⚠️ IP `172.18.0.1` зашит: при пересоздании сети `app_default` с другой подсетью
  сломается только `/crypto-index/` (502), трекер продолжит работать.

### API (полный контракт — `services/crypto-index/API.md`)
`GET /crypto-index/api/{latest,stats,index.csv,ticks.csv,db}` — read-only, без
авторизации.  Качество каждого такта — поле `flag`: `ok` (все 4 биржи + полное
окно из 4 сэмплов) / `PARTIAL_WINDOW` / `STALE` / `MISSING_EXCHANGE`.  Для строгой
сверки с методикой брать только `flag == ok`.

### Фронтенд (`frontend/src/pages/CryptoIndex.tsx`)
- `API = (VITE_API_URL ?? '') + '/crypto-index'` — тот же origin, что и трекер,
  поэтому CORS не задействован.
- Карточки по монетам (`.ex-card`, цвета из `EXCHANGE_COLORS`), бейдж `flag`,
  сводка покрытия + **задержка** (`now − last_ts`; > 45с = сборщик встал),
  поллинг 15с с кнопкой Pause, блок выгрузки CSV/`.db` с фильтром по монете и датам.
- ⚠️ **Локально вкладка покажет «Сервис недоступен»** — сборщик развёрнут только
  на проде; в локальном docker-compose его нет.

## MM Presence — оценка присутствия маркет-мейкеров (СПБ, 05.08.2026)

Вкладка **SPB → MM Presence** (`frontend/src/pages/MMDetect.tsx`): по какому
объёму и с каким спредом стоят ММ в стаканах 15 перпов СПБ Биржи.  Полное
описание методики и как читать цифры — `backend/app/mmdetect/README.md`.

Стакан анонимен, поэтому ММ выводится по двум признакам: **устойчивость**
(один объём держится на одном удалении от мида) + **двусторонность**
(зеркальный кластер сопоставимого размера).  Удаление меряется **в шагах цены**,
не в абсолютной цене — цена плывёт, котировщик двигает заявку вместе с рынком.

### Что где лежит
| Путь | Назначение |
|------|-----------|
| `backend/app/mmdetect/config.py` | все пороги (`DetectParams`), список 15 тикеров, шаг/глубина сбора |
| `backend/app/mmdetect/core.py` | **чистое ядро** — ни API, ни БД; 20 тестов на синтетике |
| `backend/app/mmdetect/collector.py` | gRPC-стрим + таймер 5 с → `ob_snapshot_level` |
| `backend/app/mmdetect/service.py` | реплей из БД + прореживание, к Finam НЕ ходит |
| `backend/app/api/routes/mmdetect.py` | `/instruments`, `/analyze`, `/summary`, `/export.csv` |
| `backend/app/db/migrations/018_ob_snapshots.sql` | `ob_snapshot_level` + `ob_capture_session` |

### Сбор
20 тикеров: 15 акций + **5 криптоиндексных перпов** (BTC/ETH/SOL/XRP/TRX,
добавлены 06.08.2026; `ANTHROPIC/OPENAI/SPCX на СПБ не резолвятся` — проверено по Finam),
MIC `RUSX`, **5 с × 20 уровней на сторону**, только в торговые часы
(`_is_trading_now` из `spb/spread_etl.py`).  Стрим держит книгу, таймер снимает
копию по epoch-сетке; REST — фолбэк (полный обход 15 тикеров ~8 с, то есть
5-секундная сетка по REST недостижима в принципе).
- ⚠️ **Замерший фид не должен выглядеть как стоящая котировка.** Персистентность
  — это буквально «объём не менялся», поэтому мёртвый стрим дал бы 1.0.  Точка
  пишется, только пока сессия жива И книга обновлялась не позже
  `STALE_AFTER_SEC`; иначе это пропуск, и доля пропусков показывается рядом с
  каждой оценкой.
- ⚠️ `retries=1` на REST даёт **ложные 404** (наблюдалось на COHR/COIN/UBER) —
  фантомный пропуск смещает метрику присутствия, поэтому здесь `retries=2`.
- ⚠️ **Историю нельзя бэкфилить** — Finam отдаёт только живой стакан.

⚠️ **Крипта не требует спец-обработки, но выглядит иначе**: шаг цены различается на
четыре порядка (BTC 0.1 … TRX 0.00001), спред у топа 0.1–2.9 б.п. против 6–20 у
акций, лоты дробные (BTC 0.0001 … XRP/TRX 10).  Шаг выводится из данных, радиус
поиска задан долей цены — оба масштабируются сами; лот применяется там, где
считаются деньги (`SPB_LOTS`).  ⚠️ У BTC вся сохранённая глубина (20 уровней)
укладывается в ~25 б.п., поэтому коридоры ±0.25/0.5% почти всегда помечены
«книга обрезана» — это правда о том, что видно, а не дефект.

### Диск (замерено, не оценка)
~143 строки/с ≈ **8.8 млн строк и 1.2 ГБ за торговый день**, сжатие **7.1×**.
Отсюда: чанк **6 часов** (при недельном чанке по умолчанию политика сжатия ждала
бы почти неделю — она срабатывает, только когда ВЕСЬ диапазон чанка старше
порога), сжатие через 12 ч, ретеншен 14 дней → установившийся размер ≈ 3 ГБ.

### Методика: персистентность спрашивается у РАЗМЕРА, не у места (05.08.2026)
⚠️ **Первая версия требовала, чтобы обе котировки стояли на одинаковом удалении
от мида, и слепла.**  Обязательство ММ связывает ОБЪЁМЫ котировок, а не
расстояния.  Замер на AMD (1433 снимка, 6 ч): заявка ~140 лотов присутствовала на
ask в 96.4% снимков, на bid в 99.4%, одновременно с двух сторон в 96.0% — а её
удаление гуляло (34% на 0–30 шагах, 38% на 30–60, 22% на 150–180), поэтому
персистентность «по корзинке» не поднималась выше 0.50 и пара не подтверждалась
никогда.  Причина: клиентские заявки на 1–20 лотов встают перед ММ, тянут мид к
себе, и удаления двух его котировок расходятся, хотя он ничего не двигал.
Расширение корзинки не лечит: на 60 шагах персистентность выросла до 0.62/0.66,
но bid-корзинка набрала 290 против 150 на ask — сломалась симметрия.
- **Сейчас:** кандидаты — устойчивые РАЗМЕРЫ заявок (`volume_centres` →
  `find_size_clusters`), пары сводятся по совпадению объёма (`match_by_size`),
  расстояние — только выход (медиана по каждой стороне отдельно).  Единственное,
  что делает расстояние, — ограничивает зону поиска (`search_radius_pct`, 0.5% от
  цены), чтобы не втянуть глубокий чужой объём.  Оценка **инвариантна к шагу
  цены** (есть тест).  `bin_steps` остался только для heatmap.
- **В одном стакане может стоять НЕСКОЛЬКО ММ** (на AMD видно минимум двоих).
  Каждая пара — отдельный котировщик со своим объёмом, удалениями и **своим
  спредом**; усреднять их бессмысленно, поэтому в карточке «объём всего» (сумма)
  + «крупнейший», а на графике до `MAX_TRACKED_PAIRS`=4 линий.  ⚠️ Разделение
  идёт по РАЗМЕРУ: два ММ с одинаковым объёмом сольются в одного, а один ММ с
  двумя разными заявками будет посчитан как двое.
- **Уровень стакана — это СУММА заявок по цене**, поэтому заявка ММ прячется в
  чужом уровне.  Замер на UBER (2127 снимков): bid показывал 350 в 51.8% снимков
  и 700 в 48.0%, никогда одновременно, вместе 99.8% — одна постоянная заявка 350
  плюс вторая такая же половину времени; поиск точного размера давал 0.52 и ММ не
  подтверждался.  Теперь уровень засчитывается кандидату V при **целой кратности**
  до 3× (`MAX_STACK_MULTIPLE`), допуск — относительно V, а не k·V.  ⚠️ Кандидаты
  проверяются НЕЗАВИСИМО (`_match_centres` возвращает все совпадения): уровень 700
  — это свидетельство и за «одна заявка 700», и за «две по 350», иначе крупный
  кандидат молча забирал бы себе весь уровень и мелкий не набирал персистентность.
  ⚠️ Обратная сторона: кандидат, который НИКОГДА не стоял на уровне один, — это
  вывод из чужих заявок, а не наблюдение (сразу после правки на AMD появились
  «котировщики» на 17 и 5 лотов с долей «один на уровне» 1–2%).  Поэтому
  `MIN_ALONE_SHARE`=0.10: кандидат обязан быть замечен в одиночку хотя бы в 10%
  засчитанных ему снимков.  На странице эта доля показана колонкой «один на
  уровне» по каждой стороне.
- ⚠️ **Зеркальная беда кратности — задвоение**: если V и 2V проходят пороги с ОБЕИХ
  сторон, один уровень отчитывается как два котировщика и его объём попадает в
  итог дважды.  Наблюдалось на ETH: ask в 20 шагах читался как 30050 в 77%
  снимков и как 15000 в остальных, итог показывал $86k там, где стоит максимум
  $57k.  `_dedupe_stacks` схлопывает пары, у которых размеры кратны И удаление
  совпадает; выживает та, что чаще стояла на уровне ОДНА.  Это занижает ЧИСЛО
  участников (два ММ по V читаются как один на 2V) — направление выбрано
  сознательно: различить их по стакану нельзя, а выдумать участника хуже, чем
  недосчитать.
- **`min_cluster_volume` = 2 контракта.**  Фильтр симметрии — это ОТНОШЕНИЕ,
  поэтому пара 1×1 проходит его тождественно; на NFLX такая пара дала
  персистентность 0.97/0.87 и попадала в отчёт как «объём ММ = 1».
- **`truncated`** в коридорах ±0.1/0.25/0.5%: доля снимков, где сохранённая
  глубина кончилась внутри коридора — там цифра нижняя оценка, а не «пусто».
- Сводка по всем инструментам считается по **прореженной сетке** (`stride_for`,
  цель 600 снимков на окно), карточка — по полной; расхождение ожидаемо и
  подписано в интерфейсе.  ⚠️ У инструмента с короткой историей прореженная
  выборка может не добрать `min_snapshots`=20, и в сводке будет «не подтверждён»,
  хотя карточка на полной сетке котировщиков находит.
- ⚠️ **Производительность сводки** (06.08.2026, после добавления крипты стало 29 с):
  профиль пересчитывал то, что уже посчитал детектор, а `_quote_price` заново
  сканировал уровни на каждую пару.  Теперь `find_size_clusters` возвращает ВСЕ
  кандидаты (профиль строится из них) и заодно цену ближайшего уровня по каждому
  кандидату в каждом снимке; инструменты читаются из БД конкурентно
  (`asyncio.gather`, семафор 5).  Итог: 29 с → 7 с на холодную, 0.6 с из TTL-кеша.

## OKR — зеркальные контракты MOEX к TradFi криптобирж (20.08.2026)

Вкладка **Cryptoexchanges → OKR** (`frontend/src/pages/OKR.tsx`) — один
показатель: дневной оборот двух корзин MOEX FORTS, делённый на дневной оборот
TradFi шести криптобирж.  Обе части в рублях, поэтому отношение безразмерно.
На странице KPI за последний ПОЛНЫЙ день (+ отклонение от среднего за 30 дней)
и линия отношения по дням.  Порядок величины: ~5–10 %.

### Числитель — две корзины (`backend/app/okr/config.py`)
Ключ — сырой ISS ASSETCODE, состав сверен по снапшоту рынка FORTS и полям
`GROUPTYPE` / `FACEUNIT` / `CONTRACTNAME` из `/iss/securities/<SECID>`.
- **`COMMODITY_BASKET` (22)** — `GROUPTYPE=Товары` с зарубежным базисом.  19 из
  них котируются в долларах (`FACEUNIT=USR`: GOLD, BR, SILV, NG, PLT, PLD,
  COFFEE, COPPER, ALUM, NICKEL, ZINC, ORANGE, WTI + мини GOLDM/SILVM/BRM/NGM/
  PLTM/PLDM), плюс по отдельному решению COCOA, SUGR (зеркало ICE, но котировка
  в ₽) и TTF (в €).  ⚠️ Мини — ОТДЕЛЬНЫЕ контракты, они складываются с большими,
  а не дублируют их (в отличие от `moex_daily_value`, где мини суммируются в
  родителя через `ASSET_ISS_MINI_CODE`).
- **`FOREIGN_SECURITIES_BASKET` (36)** — фьючерсы на иностранные ЦБ: 22 на паи
  зарубежных ETF/индексы (NASD→QQQ, SPYF→SPY, IBIT, ETHA, SOXQ, TLT, QQQF,
  SP500F, DAX, HANG, NIKK, STOX, DJ30, R2000, EM, KOREA, INDIA, CHINA, BRAZIL,
  ARGT, SAUDI, AFRICA) и 14 на акции/ADR (TENCENT, HYNIX, BAIDU, SAMSUNG,
  ALIBABA, XIA, TSM, JDCOM, SAP, NOVARTIS, ASML, TOYOTA, PDD, SONY).
- ⚠️ **Крипто-индексы MOEX (BTC/ETH/SOL/XRP/TRX) НЕ входят** — они отслеживают
  индекс МосБиржи, а не иностранную бумагу.  IBIT/ETHA входят: это фьючерсы на
  паи американских ETF.  Российские акции/индексы (89 активов, ~172 млрд ₽/день)
  и внутренние товары (WHEAT, AI92/AI95, DTL, GL/SL/GLDRUBF/SLVRUBF) исключены.
- Проверка на 19.08.2026: корзина А = 229.4 млрд ₽, Б = 14.5 млрд ₽ при всём
  FORTS 930.9 млрд ₽.

### Сбор числителя — свип по дням, а не по ASSETCODE
`fetch_market_value_by_assetcode(day)` (`moex/fetcher.py`) берёт **рыночный**
history-эндпоинт `/history/engines/futures/markets/forts/securities.json?date=`
и суммирует VALUE по ASSETCODE: ~830 SECID за день = 9 страниц по 100.  Дискавери
по ASSETCODE (как в основном MOEX ETL) стоило бы сотни запросов на 58 активов —
здесь цена фиксирована и не зависит от ширины корзины.
- Таблица `okr_moex_daily(date, asset_code, value_rub)` хранит **весь** рынок
  (~190 активов в день), а не только корзины → состав корзин меняется правкой
  конфига, без пересбора истории.  Миграция `019` + inline в `init_db`.
  Отдельно от `moex_daily_value`: там ключ — внутренний код (BR/GD/SV) и CASE-мэп
  на канонические символы в десятке запросов.
- `okr_etl_loop` (`okr/etl.py`) — старт через 120 с после буста, дальше каждые
  6 ч.  Дни идут **от свежих к старым**, поэтому график наполняется за минуту,
  пока 90-дневный бэкфилл (~час round-trip'ов к ISS) доезжает позади.
  Последние `LOOKBACK_DAYS=3` дня перезапрашиваются всегда (поздние корректировки
  ISS), остальные — только если их нет в БД.
- ⚠️ **В выходные ISS не публикует итоги FORTS вообще** (проверено на 15–16.08:
  0 строк).  Пустой ответ = нерабочий день, ETL его пропускает, а `fetch_okr_ratio`
  джойнит от MOEX-стороны — иначе суббота рисовалась бы нулевым отношением при
  круглосуточно торгующей крипте.

### Знаменатель — то, что уже собрано
`fetch_okr_ratio` складывает две ветки за тот же день:
- **всю вселенную equity-перпов** из `stock_daily_volume` (6 бирж, ~460 тикеров);
- **сырьё/металлы/индексные ETF** из `ohlcv_daily` по базам `TRADFI_OHLCV_BASES`
  (BRN, WTI, USOIL, UKOIL, NATGAS, NGAS, TTF, XAU, XAG, XPT, XPD, COPPER,
  ALUMINIUM, WHEAT, CORN, URANIUM, QQQ, SPY), `exchange <> 'moex'`.
- ⚠️ Курируемых US-акций (`US_STOCK_CURATED_BASES`) в этом списке НЕТ намеренно:
  они приходят через stock ETL, и ohlcv-строки задвоили бы их — та же логика, что
  в `fetch_weekly_adtv_rub`/`fetch_daily_volume_rub`.
- USD→RUB — тем же `LEFT JOIN LATERAL moex_fx_rates` (USDRUBF, forward-fill).
- ⚠️ Охват знаменателя **неполный по решению**: ETF-перпы (SOXL, KORU, SP500,
  EWY, DRAM…) отсеиваются списком `EXCLUDE` в stock ETL, а из сырья собираются
  только курируемые инструменты — мимо проходит ~13 % оборота ($6.9B ETF +
  $1.3B сырья в сутки на замере 20.08.2026: XAUT, SILVER на MEXC, SPX500,
  NAS100, USOIL/UKOIL).  Показатель из-за этого слегка ЗАВЫШЕН.  Чтобы закрыть
  разрыв, нужен ETL полной не-крипто вселенной (расширение `stocks/etl.py`).

### Эндпоинты и фронт
- `GET /api/okr/ratio?days=30` (1–90) → `{days, points:[{date, date_label,
  moex_rub, crypto_rub, ratio_pct}], latest, avg_pct, baskets}`, под `@ttl_cache()`.
  `avg_pct` — среднее ДНЕВНЫХ отношений (то же, что видно на линии), а не
  ΣMOEX/Σкрипто: KPI сравнивается именно с нарисованным графиком.
- `POST /api/okr/refresh` — ручной свип.
- Регистрация в `main.py` — ДВА места: `_spawn(okr_etl_loop())` и
  `include_router(okr.router, prefix="/api/okr")`.
- ⚠️ **Текущий день не показывается**: крипта торгует круглосуточно, а FORTS
  закрывается в 23:50 МСК, поэтому на незакрытом дне отношение весь день
  занижено и «доползает» к вечеру.  Запрос обрезает `date < CURRENT_DATE`.

## Фандинг СПБ — источники (20.08.2026)

Дневную ставку фандинга перпов СПБ Биржи приложение получает из **двух**
источников, оба пишут в одну таблицу `spb_funding`:

| Источник | Модуль | Что даёт | Чего не даёт |
|---|---|---|---|
| **Фид самой биржи** (основной) | `backend/app/spb/funding_exchange.py` | точный `Fund curr`, без токена | истории; `MeanPrice`/`MeanIndex`; проценты выводятся нами |
| **Telegram-канал @beststocks_neo** (история/фолбэк) | `backend/app/spb/funding_tg.py` | все 6 колонок как публикует канал, архив с июня | требует user-сессии Telegram |

### Фид биржи — `GET /stream-service/v1/funding/indicativeFunding`
Тот же публичный API (`spbexchange.ru/api`), что уже используется для OI, без
авторизации; метод в `spb_api.py`.  Отдаёт 25 записей — **ровно вселенная
`SPB_TICKERS`** (сверено).  Значение — `fundingPerContract.value`, USD на
контракт, это и есть колонка `Fund curr` из CSV канала.

- ⚠️ **Окно публикации: 23:00 МСК (акции) / 00:00 (крипта) → 11:30 следующего дня.**
  Вне окна фид отдаёт нули.  Поэтому цикл работает только в окне (`in_window`),
  а **бэкфила нет** — пропущенный день не вернуть (как со стаканом).  Архив есть
  только в канале.
- ⚠️ **Ноль ≠ «нет данных»**: пустой слот и настоящая нулевая ставка оба приходят
  как `value = 0`, различает их **бит 2 в `flag`** (так же рисует и сайт биржи).
  Без этой проверки вне окна вся таблица залилась бы фальшивыми нулями.
- ⚠️ **Лот брать из `SPB_LOTS`, не из фида**: у крипто-индексных перпов фид
  сообщает `lot = 1.0`, тогда как множитель цены 0.0001…10 — проценты уехали бы
  на порядки.
- **Проценты выводятся**: `pct_day = fund_curr / (MeanPrice × lot) × 100`,
  `pct_year = pct_day × 365`.  Формула сверена с CSV канала за 19-08-2026
  (BTC: `0.00109425 / (68733.95 × 0.0001)` = 0.01592 %, ×365 = 5.811 — совпало
  до последнего знака).  ⚠️ Но `MeanPrice` фид НЕ публикует, поэтому берётся
  типичная цена периода `(H+L+C)/3` → наши проценты расходятся с каналом в
  3–4-м знаке.  `mean_index` недоступен вовсе.
- Дата строки = **день открытия периода начисления** (`periodStart`
  `2026-08-19T19:00` → 19-08), как канал датирует свои файлы.
- ⚠️ **Строки из фида не затирают строки из канала**:
  `upsert_spb_funding_from_exchange` обновляет запись только `WHERE mean_index IS
  NULL`.  `mean_index` есть только у канала и служит меткой источника — иначе
  утренний прогон переписал бы точные значения канала нашими выведенными.
- `POST /api/spb/funding/exchange-refresh` — прогон вне расписания (inline, один
  HTTP-запрос).  Вне окна честно вернёт `rows: 0`.
- Свериться с каналом руками: `python -m app.spb.funding_exchange check` —
  печатает, что было бы записано, ничего не пишет.

### Автоингест из Telegram (история и фолбэк)

Дневные CSV с фандингом перпов СПБ Биржи публикует канал **@beststocks_neo**
(два файла в день: US Market и крипто-индексы, оба `Итоговый фандинг DD-MM-YYYY.csv`
с колонками `Neo, % year, % day, Fund curr, MeanPrice, MeanIndex`).  Раньше их
качали руками и заливали через `POST /api/spb/funding/upload`; теперь то же самое
делает фоновый цикл `spb_funding_tg_loop` (`backend/app/spb/funding_tg.py`,
раз в час, wall-clock), а страница загрузки осталась фолбэком.

- **Только MTProto под пользовательским аккаунтом** (telethon).  Bot API не
  подходит: бот не может читать чужой канал, где он не админ.  Публичное
  превью `t.me/s/beststocks_neo` тоже мимо — в разметке есть имя документа и
  размер, но ссылки на скачивание нет (проверено: на CDN уходит только аватар).
- ⚠️ **Session-файл = полный доступ к аккаунту Telegram** (чтение переписки,
  отправка от его имени) — опаснее токена Finam.  Поэтому: отдельный (не
  личный) аккаунт, `secrets/` в `.gitignore`, бинд-маунт `./secrets/telegram:/data/telegram`
  в обоих compose, в образ файл не попадает.  Разовый интерактивный логин:
  `docker compose exec backend python -m app.spb.funding_tg login`.
- Файлы качаются **в память** (`download_media(file=bytes)`) и парсятся тем же
  `parse_funding_csv` — на диск ничего не пишется.
- Окно обхода: `max(date)` в `spb_funding` **минус 3 дня** (`window_start`), на
  пустой таблице — 180 дней.  Дедупа нет и не нужно: PK `(date, ticker)`,
  повторно увиденный файл перезаписывает свой день.  После записи —
  `clear_cache()` (heatmap под TTL-кешем).
- `wants_file` пропускает только `Итоговый фандинг DD-MM-YYYY*.csv`.  ⚠️ Фильтр
  несущий: канал постит и другие вложения, а имя без даты уходит в парсер и
  молча даёт «нет даты в имени файла».  Покрыто `tests/test_funding_tg.py`.
- Цикл — **no-op при пустых `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`** или
  отсутствующей сессии (как SPB ETL без токена Finam), так что деплой без
  учётки работает по-старому.
- `POST /api/spb/funding/tg-refresh` — прогон вне расписания, **в фоне**:
  первый исторический проход качает десятки файлов и не уложился бы в
  60-секундный proxy-таймаут nginx.  Долгий разовый бэкфилл лучше гнать из CLI:
  `python -m app.spb.funding_tg ingest 180` (над ним нет HTTP-таймаута).

## Вкладка Funding (Cryptoexchanges)

- Первая вкладка — **Heatmap**: инструменты (строки, сгруппированы теми же
  `SYMBOL_SECTIONS`) × дни (столбцы), как на странице SPB Funding.  Метрики
  «% day» (сумма выплат за день) и «% year» (годовая из СРЕДНЕЙ ставки дня —
  устойчива к дню, когда биржа рассчиталась реже обычного).  Селектор биржи:
  конкретная или «все» (среднее по тем, кто рассчитался).  Цвет диверджентный:
  красный — лонги платят, зелёный — платят лонгам; шкала клипуется по 90-му
  перцентилю |значений|, чтобы выброс не выбелил остальную карту.
- Источник: `GET /api/funding/heatmap?days=` → `fetch_funding_daily`
  (агрегация `funding_rates` по дню × символу × бирже).
- **Вкладка Opportunities (кросс-биржевые спреды) убрана** 30.07.2026 вместе с
  KPI «Best spread»/«Active opportunities» — вместо них экстремумы текущего
  фандинга.  Эндпоинт `GET /api/funding/spreads` жив (им пользуется
  arbitrage-логика), фронт его больше не опрашивает.
- **Вкладка Instruments скрыта из бокового меню** (30.07.2026): страница и
  маршрут остались, просто нет пункта в `NAV_GROUPS`.

## Производительность (30.07.2026)

Пять правок по замерам на проде.  Цифры — «до».

- **gzip на статику** (`nginx/http-only.conf`).  В `gzip_types` был только
  `application/json`, поэтому JS-бандл (5.34 МБ, в основном Plotly) уезжал
  **несжатым** на каждой первой загрузке.  Добавлены `application/javascript`,
  `text/javascript`, `text/css`, `image/svg+xml` + `gzip_vary on` и
  `gzip_comp_level 5`.  `nginx/lightsail.conf` (TLS-вариант, не используется)
  был настроен правильно с самого начала.
  ⚠️ Файл **бинд-маунтится в контейнер**, синхронизировать `rsync --inplace`
  (см. раздел Crypto Index), затем `nginx -s reload`.
- **Code-splitting фронта** (`App.tsx`).  Страницы грузятся через `React.lazy`
  под общим `Suspense`.  Было: один чанк 5.34 МБ, ничего не рисовалось, пока он
  не скачается и не распарсится.  Стало: shell 164 КБ + чанк страницы (3–20 КБ)
  + Plotly 4.8 МБ/1.46 МБ gz **только на страницах с графиками**; Instruments /
  Exchanges / Launches / News / History его не тянут вовсе, а
  `lightweight-charts` (162 КБ) отделился к History/Dashboard.
  ⚠️ **`manualChunks` для plotly в `vite.config.ts` добавлять НЕЛЬЗЯ** (пробовал,
  откатил): rollup и без него выносит plotly в общий чанк, но стоит назвать его
  в `manualChunks` — Vite ставит на него `<link rel="modulepreload">` в
  `index.html`, и 1.46 МБ снова качаются на каждый заход, включая страницы без
  единого графика.
  ⚠️ `frontend/src/utils/excel.ts` (и через него `xlsx`) не импортируется ниоткуда
  — мёртвый код, в бандл не попадает.
- **`/api/open-interest/daily` — было ~5.7 с** (замер с сервера; снаружи 6.0 с).
  Две причины, обе исправлены в `fetch_oi_daily`:
  1. Расширение охвата OI на всю вселенную акций довело таблицу до ~940 пар
     exchange×symbol, forward-fill считался по всем, и только потом роут
     выбрасывал ~90% в Python.  Список скрытых баз (`_dropped_stock_bases`)
     теперь уходит параметром в запрос и режет пары **до** заполнения.
  2. Сам forward-fill был LATERAL-подзапросом «последний снимок ≤ этот день» на
     КАЖДУЮ пару × день (~6 000 обращений, каждое перебирало чанки 40-дневного
     окна).  Заменён на один проход `DISTINCT ON` + оконный LOCF
     (`count(…) OVER` как счётчик групп, `first_value` внутри группы).
     Чистый SQL: 4.19 с → 1.31 с на тех же 3 6xx строках.
  Итог на проде: холодный запрос ~3.2 с (остаток — сборка 3 700 dict'ов и
  сериализация JSON на стороне Python), из TTL-кеша **0.11 с**.
  ⚠️ `GREATEST(ts::date, начало_сетки)` в `snaps` обязателен: без него пара, чьи
  снимки лежат ТОЛЬКО старше 31-дневной сетки (инструмент перестал отдавать OI),
  молча исчезала бы с графика, тогда как 40-дневное окно сканирования
  существует именно ради переноса такого значения вперёд.  На проде это стоило
  63 строки.
- **TTL-кеш ответов** (`backend/app/api/cache.py`, декоратор `@ttl_cache()`,
  120 с).  Агрегаты пересчитывались на каждый запрос, хотя данные под ними
  двигаются раз в 6 ч (ETL) или 15 мин (спред).  Навешен на аналитические GET
  в `history` / `stocks` / `funding` / `open_interest` / `spb` / `mm`.
  ⚠️ Живые эндпоинты (`/orderbook`, `/moex-orderbook`, `/spread-live`, prices)
  **намеренно не кешируются**.  `clear_cache()` вызывается после
  `POST /api/spb/funding/upload`, чтобы загруженный файл был виден сразу.
  ⚠️ Кеш процессный — это нормально ровно потому, что uvicorn работает в один
  воркер (см. готчу про `--workers`).
  ⚠️ `functools.wraps` обязателен: FastAPI читает сигнатуру хендлера, чтобы
  собрать query-параметры.
- **Колоночный формат spread-history** (SPB + MM).  Серия отдаётся параллельными
  массивами (`buckets` / `spread_usd` / `spread_pct`, у MM — `spread_abs`), а не
  списком объектов-точек: имена полей повторялись на каждый 15-мин бакет и
  составляли бо́льшую часть ответа (SPB 1.3 МБ, MM shares 1.9 МБ).  Plotly всё
  равно принимает массивы.  Заодно из SQL убраны `spread_10m_*` — линия 10 млн
  снята со страницы 14.07.2026, колонки в таблице остались.
  ⚠️ У SPB `moex` стал **объектом** (`{buckets, spread_usd, spread_pct}`) или
  `null` вместо массива точек; у него своя ось X — сбор MOEX может начинаться и
  прерываться независимо от SPB.

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