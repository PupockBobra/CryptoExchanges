"""
Выгрузка 10-минутных свечей по SK Hynix на трёх площадках в один Excel.

  * SKHYNIXUSDT — Binance USDⓈ-M perp (локальный корейский тикер, KR_EQUITY)
  * SKHYUSDT    — Binance USDⓈ-M perp (ADR-контракт, EQUITY)
  * HXU6        — MOEX FORTS фьючерс HYNIX-9.26 (ASSETCODE=HYNIX)

Окно: с момента запуска HYNIX на MOEX (первая сделка 16.07.2026) до «сейчас».
Инструменты, запущенные позже/раньше, начинаются со своей первой свечи.

У Binance нет нативного 10-минутного интервала → берутся 5-минутные клайны и
складываются попарно по сетке :00/:10/:20 (та же сетка, что у MOEX).

Запуск:
    scripts/.venv/bin/python scripts/export_hynix_10m.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import requests

MSK = dt.timezone(dt.timedelta(hours=3))

# Первая сделка HYNIX-9.26 на FORTS (ISS candleborders): 2026-07-16 17:23 МСК.
START_MSK = dt.datetime(2026, 7, 16, 0, 0, tzinfo=MSK)

BINANCE_SYMBOLS = ["SKHYNIXUSDT", "SKHYUSDT"]
MOEX_SECID = "HXU6"

OUT_DIR = Path(__file__).resolve().parent.parent / "exports" / dt.date.today().isoformat()


def binance_5m(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Все 5-минутные клайны фьючерса за окно (пагинация по 1500)."""
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "5m", "startTime": cursor,
                    "endTime": end_ms, "limit": 1500},
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        rows.extend(page)
        nxt = page[-1][0] + 5 * 60_000
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1500:
            break
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "open_ms", "open", "high", "low", "close", "volume", "close_ms",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    df = df.drop_duplicates(subset="open_ms").sort_values("open_ms")
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c])
    df["trades"] = pd.to_numeric(df["trades"])

    # 5m → 10m по сетке :00/:10
    df["bucket_ms"] = (df["open_ms"] // 600_000) * 600_000
    g = df.groupby("bucket_ms")
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "quote_volume_usdt": g["quote_volume"].sum(),
        "trades": g["trades"].sum(),
    }).reset_index()

    ts = pd.to_datetime(out.pop("bucket_ms"), unit="ms", utc=True)
    out.insert(0, "time_utc", ts.dt.tz_localize(None))
    out.insert(0, "time_msk", ts.dt.tz_convert(MSK).dt.tz_localize(None))
    return out


def moex_10m(secid: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """10-минутные свечи FORTS (ISS отдаёт время в МСК, страницами по 500)."""
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"
    rows: list[list] = []
    cols: list[str] = []
    start_idx = 0
    while True:
        r = requests.get(url, params={
            "iss.meta": "off", "interval": 10, "start": start_idx,
            "from": start.strftime("%Y-%m-%d %H:%M:%S"),
            "till": end.strftime("%Y-%m-%d %H:%M:%S"),
        }, timeout=60)
        r.raise_for_status()
        block = r.json()["candles"]
        cols = block["columns"]
        page = block["data"]
        if not page:
            break
        rows.extend(page)
        start_idx += len(page)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=cols).drop_duplicates(subset="begin")
    ts = pd.to_datetime(df["begin"])
    out = pd.DataFrame({
        "time_msk": ts,
        "time_utc": ts - pd.Timedelta(hours=3),
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        # ISS во ВНУТРИДНЕВНЫХ свечах отдаёт value=0 (рубли живут только в
        # history-эндпоинте) — колонка не выгружается, чтобы не выглядела как
        # «оборота не было».
        "volume_contracts": df["volume"],
    }).sort_values("time_msk").reset_index(drop=True)
    return out


def main() -> None:
    now = dt.datetime.now(MSK)
    start_ms = int(START_MSK.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # Одна таблица: общая 10-мин ось × close по каждому инструменту.
    closes: list[pd.Series] = []
    for sym in BINANCE_SYMBOLS:
        df = binance_5m(sym, start_ms, end_ms)
        print(f"{sym}: {len(df)} свечей "
              f"({df['time_msk'].min()} … {df['time_msk'].max()})" if len(df) else f"{sym}: пусто")
        closes.append(df.set_index("time_msk")["close"].rename(f"{sym} (Binance, USDT)"))

    moex = moex_10m(MOEX_SECID, START_MSK.replace(tzinfo=None), now.replace(tzinfo=None))
    print(f"{MOEX_SECID}: {len(moex)} свечей "
          f"({moex['time_msk'].min()} … {moex['time_msk'].max()})" if len(moex) else "MOEX: пусто")
    closes.append(moex.set_index("time_msk")["close"]
                  .rename(f"HYNIX {MOEX_SECID} (MOEX, пункты)"))

    # outer join — у MOEX бар есть только там, где были сделки; пропуски
    # остаются ПУСТЫМИ (не ffill), чтобы не выдумывать котировку.
    table = pd.concat(closes, axis=1).sort_index()
    table.index.name = "time_msk"
    table = table.reset_index()
    table.insert(1, "time_utc", table["time_msk"] - pd.Timedelta(hours=3))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"skhynix_10m_close_{dt.date.today().isoformat()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl", datetime_format="YYYY-MM-DD HH:MM") as xl:
        table.to_excel(xl, sheet_name="close_10m", index=False)
        ws = xl.book.worksheets[0]
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 12), 32)
        ws.freeze_panes = "C2"

    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
