# Crypto Arbitrage Tracker

Real-time cross-exchange arbitrage detection: Binance, OKX, Kraken.

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
- **frontend** (`frontend/src/`) — React + Vite, TradingView Lightweight Charts, WebSocket-driven price/alert feeds

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
| `backend/app/collector/base.py` | Base WebSocket collector |
| `backend/app/arbitrage/detector.py` | Spread detection logic |
| `backend/app/db/migrations/001_init.sql` | TimescaleDB schema |
| `frontend/src/components/Chart.tsx` | TradingView chart wrapper |
| `frontend/src/hooks/useWebSocket.ts` | Reconnecting WebSocket hook |

## Environment variables

See `.env.example` for full list. Minimum required:
- `DATABASE_URL` — TimescaleDB connection string
- `REDIS_URL` — Redis URL
- Exchange API keys are optional for public ticker feeds (ccxt uses public endpoints for prices)

## Arbitrage threshold

Default spread threshold: **0.3%** — edit `ARBI_THRESHOLD_PCT` in `.env`.

## Adding a new exchange

1. Add collector in `backend/app/collector/<exchange>.py` extending `BaseCollector`
2. Register it in `backend/worker/main.py`
3. Add exchange name to `EXCHANGES` list in `backend/app/config.py`
