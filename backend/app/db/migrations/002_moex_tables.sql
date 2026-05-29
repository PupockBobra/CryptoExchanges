-- MOEX FORTS daily volumes and USD/RUB rate cache
-- Applied automatically by timescaledb Docker entrypoint on first run.

CREATE TABLE IF NOT EXISTS moex_daily_value (
    date       DATE NOT NULL,
    asset_code TEXT NOT NULL,
    value_rub  NUMERIC(20, 2) NOT NULL,
    PRIMARY KEY (date, asset_code)
);

CREATE INDEX IF NOT EXISTS idx_moex_daily_value_date
    ON moex_daily_value (date DESC);

CREATE TABLE IF NOT EXISTS moex_fx_rates (
    date   DATE             PRIMARY KEY,
    usdrub NUMERIC(12, 4)   NOT NULL
);
