-- Seed instruments — applied automatically by Docker entrypoint on first run.
-- Uses ON CONFLICT DO UPDATE so it is safe to re-run (idempotent).
-- Add new instruments here and they will be present on every fresh deployment.

INSERT INTO instruments (canonical, type, base_asset, quote_asset, description, enabled, aliases)
VALUES
  -- ── Spot crypto ────────────────────────────────────────────────────────────
  ('BTC/USDT',  'spot', 'BTC',    'USDT', 'Bitcoin spot',              true, '{"hyperliquid": "BTC/USDC"}'::jsonb),
  ('ETH/USDT',  'spot', 'ETH',    'USDT', 'Ethereum spot',             true, '{"hyperliquid": "ETH/USDC"}'::jsonb),
  ('SOL/USDT',  'spot', 'SOL',    'USDT', 'Solana spot',               true, '{"hyperliquid": "SOL/USDC"}'::jsonb),

  -- ── Precious metals (perps) ────────────────────────────────────────────────
  ('XAU/USDT:USDT', 'perp', 'XAU', 'USDT', 'Gold perpetual',          true, '{"hyperliquid": "XYZ-GOLD/USDC:USDC"}'::jsonb),
  ('XAG/USDT:USDT', 'perp', 'XAG', 'USDT', 'Silver perpetual',        true, '{"hyperliquid": "XYZ-SILVER/USDC:USDC"}'::jsonb),
  ('XPT/USDT:USDT', 'perp', 'XPT', 'USDT', 'Platinum perpetual',      true, '{"hyperliquid": null}'::jsonb),
  ('XPD/USDT:USDT', 'perp', 'XPD', 'USDT', 'Palladium perpetual',     true, '{"hyperliquid": null}'::jsonb),

  -- ── Commodities (perps) ────────────────────────────────────────────────────
  ('BRN/USDT:USDT',    'perp', 'BRN',    'USDT', 'Brent Crude Oil perpetual', true,
      '{"binance": "BZ/USDT:USDT", "okx": "BZ/USDT:USDT", "mexc": null, "hyperliquid": "XYZ-BRENTOIL/USDC:USDC"}'::jsonb),
  ('NATGAS/USDT:USDT', 'perp', 'NATGAS', 'USDT', 'Natural Gas perpetual',     true,
      '{"mexc": null, "hyperliquid": null}'::jsonb),

  -- ── US market (perps) ──────────────────────────────────────────────────────
  ('NVDA/USDT:USDT', 'perp', 'NVDA', 'USDT', 'NVIDIA perpetual',              true, '{"hyperliquid": "CASH-NVDA/USDT0:USDT0"}'::jsonb),
  ('QQQ/USDT:USDT',  'perp', 'QQQ',  'USDT', 'Invesco QQQ ETF perpetual',     true, '{"mexc": "QQQSTOCK/USDT:USDT", "hyperliquid": null}'::jsonb),
  ('SPY/USDT:USDT',  'perp', 'SPY',  'USDT', 'SPDR S&P 500 ETF perpetual',   true, '{"hyperliquid": null}'::jsonb),
  ('TSLA/USDT:USDT', 'perp', 'TSLA', 'USDT', 'Tesla perpetual',               true, '{"hyperliquid": null}'::jsonb)

ON CONFLICT (canonical) DO UPDATE SET
  type         = EXCLUDED.type,
  base_asset   = EXCLUDED.base_asset,
  quote_asset  = EXCLUDED.quote_asset,
  description  = EXCLUDED.description,
  enabled      = EXCLUDED.enabled,
  aliases      = EXCLUDED.aliases;
