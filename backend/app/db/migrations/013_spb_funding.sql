-- Daily funding figures for СПБ Биржа perpetual futures, uploaded from the
-- channel's per-day CSVs (columns: Neo, % year, % day, Fund curr, MeanPrice,
-- MeanIndex).  One row per instrument × trading day.  Values are stored
-- verbatim — funding is a rate, not a turnover, so no fx conversion.

CREATE TABLE IF NOT EXISTS spb_funding (
    date       DATE             NOT NULL,
    ticker     TEXT             NOT NULL,
    pct_year   DOUBLE PRECISION,
    pct_day    DOUBLE PRECISION,
    fund_curr  DOUBLE PRECISION,
    mean_price DOUBLE PRECISION,
    mean_index DOUBLE PRECISION,
    PRIMARY KEY (date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_spb_funding_date
    ON spb_funding (date DESC);
