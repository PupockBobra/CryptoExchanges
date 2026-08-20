-- Seed additional commodity perps (agriculture, industrial metals, energy) that
-- trade on the crypto exchanges but were missing from the tradfi universe.
-- Symbols verified via load_markets() on 2026-07-08:
--   WTI       → binance/okx/bybit CL/USDT:USDT, mexc USOIL/USDT:USDT,
--               hyperliquid XYZ-CL/USDC:USDC (was prod-only, now seeded)
--   COPPER    → binance/mexc COPPER/USDT:USDT, hyperliquid XYZ-COPPER
--   ALUMINIUM → mexc ALUMINUM/USDT:USDT (US spelling), hyperliquid XYZ-ALUMINIUM
--   WHEAT / CORN / URANIUM / TTF → hyperliquid XYZ- markets exist but currently
--               trade ~zero volume (no daily candles yet); seeded so data flows
--               in automatically once they start trading.  (Wheat also lists on
--               the illiquid VNTL builder dex, ~0.4M USD/day — not worth wiring.)
-- ON CONFLICT DO UPDATE keeps it idempotent (mirrors 002_seed_instruments.sql).

INSERT INTO instruments (canonical, type, base_asset, quote_asset, description, enabled, aliases)
VALUES
  ('WTI/USDT:USDT',       'perp', 'WTI',       'USDT', 'WTI Crude Oil perpetual', true,
      '{"binance": "CL/USDT:USDT", "okx": "CL/USDT:USDT", "bybit": "CL/USDT:USDT", "mexc": "USOIL/USDT:USDT", "hyperliquid": "XYZ-CL/USDC:USDC"}'::jsonb),
  ('COPPER/USDT:USDT',    'perp', 'COPPER',    'USDT', 'Copper perpetual',       true,
      '{"hyperliquid": "XYZ-COPPER/USDC:USDC"}'::jsonb),
  ('ALUMINIUM/USDT:USDT', 'perp', 'ALUMINIUM', 'USDT', 'Aluminium perpetual',    true,
      '{"mexc": "ALUMINUM/USDT:USDT", "hyperliquid": "XYZ-ALUMINIUM/USDC:USDC"}'::jsonb),
  ('WHEAT/USDT:USDT',     'perp', 'WHEAT',     'USDT', 'Wheat perpetual',        true,
      '{"hyperliquid": "XYZ-WHEAT/USDC:USDC"}'::jsonb),
  ('CORN/USDT:USDT',      'perp', 'CORN',      'USDT', 'Corn perpetual',         true,
      '{"hyperliquid": "XYZ-CORN/USDC:USDC"}'::jsonb),
  ('URANIUM/USDT:USDT',   'perp', 'URANIUM',   'USDT', 'Uranium perpetual',      true,
      '{"hyperliquid": "XYZ-URANIUM/USDC:USDC"}'::jsonb),
  ('TTF/USDT:USDT',       'perp', 'TTF',       'USDT', 'Dutch TTF gas perpetual', true,
      '{"hyperliquid": "XYZ-TTF/USDC:USDC"}'::jsonb)
ON CONFLICT (canonical) DO UPDATE SET
  type         = EXCLUDED.type,
  base_asset   = EXCLUDED.base_asset,
  quote_asset  = EXCLUDED.quote_asset,
  description  = EXCLUDED.description,
  enabled      = EXCLUDED.enabled,
  aliases      = EXCLUDED.aliases;
