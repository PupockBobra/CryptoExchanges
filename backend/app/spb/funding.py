"""Parser for the СПБ Биржа funding CSVs (uploaded via the Funding page).

Each file is one trading day for one instrument group (US stocks or crypto),
named ``Итоговый фандинг DD-MM-YYYY.csv`` (an optional `` (1)`` suffix marks the
second group of the same day).  Columns:

    Neo, % year, % day, Fund curr, MeanPrice, MeanIndex

The date is taken from the filename; the ticker (``Neo``) column already matches
the app's SPB tickers exactly, so no mapping is needed.
"""

import csv
import io
import re
from datetime import date

# DD-MM-YYYY anywhere in the filename.
_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")

_HEADER = {"Neo", "% year", "% day", "Fund curr", "MeanPrice", "MeanIndex"}


def date_from_filename(name: str) -> date | None:
    m = _DATE_RE.search(name)
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_funding_csv(filename: str, text: str) -> tuple[list[tuple], str | None]:
    """
    Parse one uploaded CSV into rows ready for ``upsert_spb_funding``:
    ``(date, ticker, pct_year, pct_day, fund_curr, mean_price, mean_index)``.

    Returns ``(rows, error)``.  ``error`` is a human-readable reason the file was
    skipped (bad/missing date, wrong header, no data); ``rows`` is empty then.
    """
    day = date_from_filename(filename)
    if day is None:
        return [], "нет даты в имени файла (ожидается DD-MM-YYYY)"

    # Tolerate a UTF-8 BOM.
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not _HEADER.issubset({f.strip() for f in reader.fieldnames}):
        return [], "неожиданные колонки (ожидается Neo, % year, % day, Fund curr, MeanPrice, MeanIndex)"

    rows: list[tuple] = []
    for r in reader:
        ticker = (r.get("Neo") or "").strip()
        if not ticker:
            continue
        rows.append((
            day,
            ticker,
            _num(r.get("% year")),
            _num(r.get("% day")),
            _num(r.get("Fund curr")),
            _num(r.get("MeanPrice")),
            _num(r.get("MeanIndex")),
        ))

    if not rows:
        return [], "нет строк с данными"
    return rows, None
