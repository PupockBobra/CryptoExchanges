FROM python:3.12-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS dev
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS prod
COPY . .
# Single worker only: lifespan spawns background ETL loops (SPB/MOEX/stocks/OI,
# orderbook feed) that must not be duplicated per process — 2 workers ran every
# ETL twice and doubled Finam REST traffic into its rate limit (429 → skipped
# tickers), and split the in-memory orderbook/spread caches across processes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
