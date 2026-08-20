-- Seed additional commodity perps (agriculture, industrial metals, energy) that
-- trade on the crypto exchanges but were missing from the tradfi universe.
-- Symbols verified via load_markets() on 2026-07-08:
--   WTI       → binance/okx/bybit CL/USDT:USDT, mexc USOIL/USDT:USDT,
--               hyperliquid XYZ-CL/USDC:USDC (was prod-only, now seeded)
--   COPPER    → binance/mexc/bitget COPPER/USDT:USDT, okx XCU/USDT:USDT,
--               hyperliquid XYZ-COPPER
--   ALUMINIUM → mexc ALUMINUM/USDT:USDT (US spelling), hyperliquid XYZ-ALUMINIUM
--   WHEAT / CORN / URANIUM / TTF → hyperliquid XYZ- markets exist but currently
--               trade ~zero volume (no daily candles yet); seeded so data flows
--               in automatically once they start trading.  (Wheat also lists on
--               the illiquid VNTL builder dex, ~0.4M USD/day — not worth wiring.)
-- ON CONFLICT DO UPDATE keeps it idempotent (mirrors 002_seed_instruments.sql).
--
-- ⚠️ `aliases` is OVERWRITTEN wholesale on conflict, so this map must stay
-- complete — a re-run reverts prod to exactly what is written here.
--
-- Re-verified live via load_markets() + price cross-check on 2026-07-29:
--   * okx lists copper as XCU/USDT:USDT (6.3013 vs 6.297 on binance/mexc) — it
--     was missing here, so okx copper was silently never collected.
--   * An explicit `null` means "not listed on this exchange" and makes
--     _build_work_list skip the pair.  Without it every poll raises BadSymbol
--     and logs a warning, which is why the warning channel was full of noise.
--     Absence of an entry means "resolves under the canonical symbol".
--   * TTF is NOT the `GAS/USDT:USDT` perp on binance/okx/bybit/bitget — that is
--     the Neo GAS crypto token ($0.93), not Dutch TTF gas.  Left unlisted.

INSERT INTO instruments (canonical, type, base_asset, quote_asset, description, enabled, aliases)
VALUES
  ('WTI/USDT:USDT',       'perp', 'WTI',       'USDT', 'WTI Crude Oil perpetual', true,
      '{"binance": "CL/USDT:USDT", "okx": "CL/USDT:USDT", "bybit": "CL/USDT:USDT", "mexc": "USOIL/USDT:USDT", "hyperliquid": "XYZ-CL/USDC:USDC", "bitget": "CL/USDT:USDT"}'::jsonb),
  ('COPPER/USDT:USDT',    'perp', 'COPPER',    'USDT', 'Copper perpetual',       true,
      '{"okx": "XCU/USDT:USDT", "hyperliquid": "XYZ-COPPER/USDC:USDC", "bybit": null}'::jsonb),
  ('ALUMINIUM/USDT:USDT', 'perp', 'ALUMINIUM', 'USDT', 'Aluminium perpetual',    true,
      '{"mexc": "ALUMINUM/USDT:USDT", "hyperliquid": "XYZ-ALUMINIUM/USDC:USDC", "binance": null, "okx": null, "bybit": null, "bitget": null}'::jsonb),
  ('WHEAT/USDT:USDT',     'perp', 'WHEAT',     'USDT', 'Wheat perpetual',        true,
      '{"hyperliquid": "XYZ-WHEAT/USDC:USDC", "binance": null, "okx": null, "bybit": null, "mexc": null, "bitget": null}'::jsonb),
  ('CORN/USDT:USDT',      'perp', 'CORN',      'USDT', 'Corn perpetual',         true,
      '{"hyperliquid": "XYZ-CORN/USDC:USDC", "binance": null, "okx": null, "bybit": null, "mexc": null, "bitget": null}'::jsonb),
  ('URANIUM/USDT:USDT',   'perp', 'URANIUM',   'USDT', 'Uranium perpetual',      true,
      '{"hyperliquid": "XYZ-URANIUM/USDC:USDC", "binance": null, "okx": null, "bybit": null, "mexc": null, "bitget": null}'::jsonb),
  ('TTF/USDT:USDT',       'perp', 'TTF',       'USDT', 'Dutch TTF gas perpetual', true,
      '{"hyperliquid": "XYZ-TTF/USDC:USDC", "binance": null, "okx": null, "bybit": null, "mexc": null, "bitget": null}'::jsonb)
ON CONFLICT (canonical) DO UPDATE SET
  type         = EXCLUDED.type,
  base_asset   = EXCLUDED.base_asset,
  quote_asset  = EXCLUDED.quote_asset,
  description  = EXCLUDED.description,
  enabled      = EXCLUDED.enabled,
  aliases      = EXCLUDED.aliases;
