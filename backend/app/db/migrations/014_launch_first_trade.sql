-- Futures Launches: listing dates derived from the first traded daily candle.
--
-- Bitget and Hyperliquid publish no listing timestamp, so the Launches page
-- walks their klines to find the first bar that actually traded.  That costs
-- dozens of paginated requests per symbol and the answer never changes, so it
-- is persisted here and reloaded on backend start.
CREATE TABLE IF NOT EXISTS launch_first_trade (
    exchange     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    first_traded DATE NOT NULL,
    PRIMARY KEY (exchange, symbol)
);
