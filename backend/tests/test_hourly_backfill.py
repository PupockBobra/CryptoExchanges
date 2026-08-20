"""Hourly OHLCV backfill: fetch window and work-list expansion."""

from datetime import datetime, timedelta, timezone

from app.backfill.hourly import (
    HOURLY_BACKFILL_DAYS,
    HOURLY_LOOKBACK_HOURS,
    build_work_list,
    group_by_exchange,
    hourly_since,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class TestHourlySince:
    def test_empty_table_starts_at_the_retention_window(self):
        assert hourly_since(None, NOW) == NOW - timedelta(days=HOURLY_BACKFILL_DAYS)

    def test_incremental_run_looks_back_a_few_hours(self):
        latest = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        assert hourly_since(latest, NOW) == latest - timedelta(hours=HOURLY_LOOKBACK_HOURS)

    def test_never_reaches_past_the_retention_window(self):
        """A stale row must not drag the fetch behind rows the retention job deletes."""
        stale = NOW - timedelta(days=400)
        assert hourly_since(stale, NOW) == NOW - timedelta(days=HOURLY_BACKFILL_DAYS)

    def test_naive_timestamp_from_asyncpg_is_treated_as_utc(self):
        naive = datetime(2026, 7, 31, 10, 0)
        assert hourly_since(naive, NOW) == datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)


class TestBuildWorkList:
    def test_plain_instrument_expands_to_every_exchange(self):
        work = build_work_list([{"canonical": "XAU/USDT:USDT", "aliases": {}}])
        assert {w[0] for w in work}                      # at least one exchange
        assert all(w[2] == "XAU/USDT:USDT" for w in work)
        assert all(w[1] == "XAU/USDT:USDT" for w in work)  # no alias → canonical
        assert all(w[4] is False for w in work)            # not a perp override

    def test_alias_is_used_as_the_exchange_symbol(self):
        work = build_work_list([{"canonical": "WTI/USDT:USDT", "aliases": {"mexc": "USOIL/USDT:USDT"}}])
        mexc = [w for w in work if w[0] == "mexc"]
        assert mexc and mexc[0][1] == "USOIL/USDT:USDT"
        assert mexc[0][3] is True   # has_alias

    def test_explicit_null_alias_skips_that_exchange(self):
        work = build_work_list([{"canonical": "XAU/USDT:USDT", "aliases": {"okx": None}}])
        assert "okx" not in {w[0] for w in work}

    def test_aliases_may_arrive_as_a_json_string(self):
        """asyncpg hands JSONB back as raw text."""
        work = build_work_list([{"canonical": "WTI/USDT:USDT", "aliases": '{"mexc": "USOIL/USDT:USDT"}'}])
        mexc = [w for w in work if w[0] == "mexc"]
        assert mexc and mexc[0][1] == "USOIL/USDT:USDT"

    def test_crypto_majors_are_fetched_from_their_perp_contract(self):
        work = build_work_list([{"canonical": "BTC/USDT", "aliases": {}}])
        assert work, "BTC must expand to its perp contracts"
        for ex_id, ex_sym, canonical, has_alias, force_perp in work:
            assert canonical == "BTC/USDT"      # stored under the spot canonical
            assert ":" in ex_sym                # fetched from the perp market
            assert force_perp is True


class TestGroupByExchange:
    def test_one_bucket_per_exchange(self):
        work = build_work_list([
            {"canonical": "XAU/USDT:USDT", "aliases": {}},
            {"canonical": "XAG/USDT:USDT", "aliases": {}},
        ])
        buckets = group_by_exchange(work)
        # every pair landed in some bucket, and each exchange got exactly one
        assert sum(len(v) for v in buckets.values()) == len(work)
        assert len(buckets) == len({w[0] for w in work})

    def test_spot_and_perp_never_share_a_bucket(self):
        """They need different ccxt instances, so mixing them would break the fetch."""
        work = build_work_list([
            {"canonical": "BTC/USDT",      "aliases": {}},   # perp override
            {"canonical": "XAU/USDT:USDT", "aliases": {}},   # native perp
        ])
        buckets = group_by_exchange(work)
        for (_ex_id, is_perp), jobs in buckets.items():
            assert all(job[2] is is_perp for job in jobs)

    def test_empty_work_list(self):
        assert group_by_exchange([]) == {}
