-- Seed instruments — applied automatically by Docker entrypoint on first run.
-- Uses ON CONFLICT DO UPDATE so it is safe to re-run (idempotent).
-- Add new instruments here and they will be present on every fresh deployment.

INSERT INTO instruments (canonical, type, base_asset, quote_asset, description, enabled, aliases)
VALUES
  -- ── Crypto majors ──────────────────────────────────────────────────────────
  -- type='spot' drives the real-time price feed (spot symbols below).  Their
  -- daily *volume* and *open interest*, however, are sourced from PERPETUAL
  -- futures via CRYPTO_PERP_OVERRIDES in backend/app/exchanges.py.
  ('BTC/USDT',  'spot', 'BTC',    'USDT', 'Bitcoin',                   true, '{"hyperliquid": "BTC/USDC"}'::jsonb),
  ('ETH/USDT',  'spot', 'ETH',    'USDT', 'Ethereum',                  true, '{"hyperliquid": "ETH/USDC"}'::jsonb),
  ('SOL/USDT',  'spot', 'SOL',    'USDT', 'Solana',                    true, '{"hyperliquid": "SOL/USDC"}'::jsonb),

  -- ── Precious metals (perps) ────────────────────────────────────────────────
  ('XAU/USDT:USDT', 'perp', 'XAU', 'USDT', 'Gold perpetual',          true, '{"hyperliquid": "XYZ-GOLD/USDC:USDC"}'::jsonb),
  ('XAG/USDT:USDT', 'perp', 'XAG', 'USDT', 'Silver perpetual',        true, '{"mexc": "SILVER/USDT:USDT", "hyperliquid": "XYZ-SILVER/USDC:USDC"}'::jsonb),
  -- bybit lists neither platinum nor palladium; explicit null skips the pair
  -- instead of raising BadSymbol on every poll (verified 2026-07-29).
  ('XPT/USDT:USDT', 'perp', 'XPT', 'USDT', 'Platinum perpetual',      true, '{"hyperliquid": "XYZ-PLATINUM/USDC:USDC", "bybit": null}'::jsonb),
  ('XPD/USDT:USDT', 'perp', 'XPD', 'USDT', 'Palladium perpetual',     true, '{"hyperliquid": "XYZ-PALLADIUM/USDC:USDC", "bybit": null}'::jsonb),

  -- ── Commodities (perps) ────────────────────────────────────────────────────
  ('BRN/USDT:USDT',    'perp', 'BRN',    'USDT', 'Brent Crude Oil perpetual', true,
      '{"binance": "BZ/USDT:USDT", "okx": "BZ/USDT:USDT", "bybit": "BZ/USDT:USDT", "mexc": "UKOIL/USDT:USDT", "hyperliquid": "XYZ-BRENTOIL/USDC:USDC", "bitget": "BZ/USDT:USDT"}'::jsonb),
  ('NATGAS/USDT:USDT', 'perp', 'NATGAS', 'USDT', 'Natural Gas perpetual',     true,
      '{"mexc": "NGAS/USDT:USDT", "okx": "NG/USDT:USDT", "bybit": null, "hyperliquid": "XYZ-NATGAS/USDC:USDC"}'::jsonb),

  -- ── US market (perps) ──────────────────────────────────────────────────────
  ('AAPL/USDT:USDT',  'perp', 'AAPL',  'USDT', 'Apple perpetual',             true, '{"mexc": "AAPLSTOCK/USDT:USDT", "bybit": "AAPL/USDT:USDT", "hyperliquid": "XYZ-AAPL/USDC:USDC"}'::jsonb),
  ('AMZN/USDT:USDT',  'perp', 'AMZN',  'USDT', 'Amazon perpetual',            true, '{"mexc": "AMZNSTOCK/USDT:USDT", "bybit": "AMZN/USDT:USDT", "hyperliquid": "XYZ-AMZN/USDC:USDC"}'::jsonb),
  ('GOOGL/USDT:USDT', 'perp', 'GOOGL', 'USDT', 'Alphabet perpetual',          true, '{"mexc": "GOOGLSTOCK/USDT:USDT", "bybit": "GOOGL/USDT:USDT", "hyperliquid": "XYZ-GOOGL/USDC:USDC"}'::jsonb),
  ('META/USDT:USDT',  'perp', 'META',  'USDT', 'Meta perpetual',              true, '{"mexc": "METASTOCK/USDT:USDT", "bybit": "META/USDT:USDT", "hyperliquid": "XYZ-META/USDC:USDC"}'::jsonb),
  ('MSFT/USDT:USDT',  'perp', 'MSFT',  'USDT', 'Microsoft perpetual',         true, '{"mexc": "MSFTSTOCK/USDT:USDT", "bybit": "MSFT/USDT:USDT", "hyperliquid": "XYZ-MSFT/USDC:USDC"}'::jsonb),
  ('NVDA/USDT:USDT',  'perp', 'NVDA',  'USDT', 'NVIDIA perpetual',            true, '{"mexc": null, "hyperliquid": "CASH-NVDA/USDT0:USDT0"}'::jsonb),
  ('QQQ/USDT:USDT',   'perp', 'QQQ',   'USDT', 'Invesco QQQ ETF perpetual',   true, '{"mexc": "QQQSTOCK/USDT:USDT", "hyperliquid": null}'::jsonb),
  ('SPY/USDT:USDT',   'perp', 'SPY',   'USDT', 'SPDR S&P 500 ETF perpetual',  true, '{"mexc": null, "hyperliquid": null}'::jsonb),
  ('TSLA/USDT:USDT',  'perp', 'TSLA',  'USDT', 'Tesla perpetual',             true, '{"mexc": null, "hyperliquid": null}'::jsonb),
  ('SPCX/USDT:USDT',  'perp', 'SPCX',  'USDT', 'SpaceX (pre-IPO) perpetual',  true, '{"mexc": "SPCXSTOCK/USDT:USDT", "hyperliquid": "XYZ-SPCX/USDC:USDC"}'::jsonb),

  -- ── Korean market (perps) ──────────────────────────────────────────────────
  ('SKHYNIX/USDT:USDT', 'perp', 'SKHYNIX', 'USDT', 'SK Hynix perpetual',   true, '{"hyperliquid": "XYZ-SKHX/USDC:USDC", "mexc": "SKHYNIXSTOCK/USDT:USDT"}'::jsonb),
  ('SAMSUNG/USDT:USDT', 'perp', 'SAMSUNG', 'USDT', 'Samsung perpetual',     true, '{"hyperliquid": "XYZ-SMSN/USDC:USDC", "mexc": "SAMSUNGSTOCK/USDT:USDT"}'::jsonb),
  ('HYUNDAI/USDT:USDT', 'perp', 'HYUNDAI', 'USDT', 'Hyundai perpetual',     true, '{"hyperliquid": "XYZ-HYUNDAI/USDC:USDC", "mexc": "HYUNDAISTOCK/USDT:USDT"}'::jsonb)

ON CONFLICT (canonical) DO UPDATE SET
  type         = EXCLUDED.type,
  base_asset   = EXCLUDED.base_asset,
  quote_asset  = EXCLUDED.quote_asset,
  description  = EXCLUDED.description,
  enabled      = EXCLUDED.enabled,
  aliases      = EXCLUDED.aliases;
