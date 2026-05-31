# Crypto Arbitrage Tracker

Real-time cross-exchange arbitrage detection across **Binance, OKX, Bybit,
MEXC, Hyperliquid** + MOEX FORTS turnover data integration.

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
| `frontend/src/utils/format.ts` | daysAgo / timeAgo / fmtVolume helpers |
| `frontend/src/hooks/useWebSocket.ts` | Reconnecting WebSocket with exponential backoff |
| `frontend/src/pages/Analytics.tsx` | Weekly ADTV stacked-bar charts |
| `frontend/src/pages/DailyVolume.tsx` | Daily volume stacked-bar charts (30d) |
| `frontend/src/pages/Launches.tsx` | Non-crypto perp futures listings page |

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
5. Add color + label to `frontend/src/types/index.ts` (`EXCHANGE_COLORS`, `EXCHANGES`)
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

### Фронтенд (Analytics.tsx)
- Ось Y: авто-масштаб — если max ADTV < 30B → миллионы (₽M), иначе миллиарды (₽B)
- MOEX сегмент скрыт для секций `US Market` и `Spot Crypto`
- X-ось: диапазон недели `May 18 – May 24` вместо одной даты
- Hover: `₽83.2B` / `₽9800.1M`

### Ключевые файлы
| Путь | Назначение |
|------|-----------|
| `backend/app/moex/config.py` | ASSET_ISS_CODE, ASSET_TO_CANONICAL |
| `backend/app/moex/fetcher.py` | ISS HTTP-клиент, dynamic SECID discovery |
| `backend/app/moex/etl.py` | Планировщик ETL, upsert в БД |
| `backend/app/db/timescale.py` | fetch_weekly_adtv_rub() |
| `backend/app/api/routes/history.py` | GET /api/history/weekly-adtv |
| `frontend/src/pages/Analytics.tsx` | Stacked-bar диаграммы |

### Статус (на 31.05.2026)
- ✅ ETL с динамическим discovery (нет хардкода серий)
- ✅ Таблицы moex_fx_rates, moex_daily_value заполнены (116 торг. дней с 01.12.2025)
- ✅ /weekly-adtv возвращает RUB-данные с MOEX как exchange='moex'
- ✅ Analytics.tsx: рубли, авто-масштаб B/M, диапазоны на оси X