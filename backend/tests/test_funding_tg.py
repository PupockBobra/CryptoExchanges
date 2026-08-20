"""Telegram funding ingest: which attachments we take, and how far back we scan.

Both are pure functions — the ingest itself needs a live MTProto session and is
not covered here.  The file filter is the load-bearing one: the channel posts
other documents too, and a wrong match reaches parse_funding_csv, which reports
"нет даты в имени файла" and silently ingests nothing.
"""

from datetime import date

from app.spb.funding import parse_funding_csv
from app.spb.funding_tg import wants_file, window_start


def test_accepts_the_channel_filenames():
    assert wants_file("Итоговый фандинг 19-08-2026.csv")
    # Both of a day's files (US stocks + crypto) share a name; a download folder
    # renames the second one.
    assert wants_file("Итоговый фандинг 19-08-2026 (1).csv")


def test_rejects_other_attachments():
    assert not wants_file(None)                       # message with no document
    assert not wants_file("Итоговый фандинг 19-08-2026.xlsx")
    assert not wants_file("Отчёт 19-08-2026.csv")
    assert not wants_file("прайс.csv")


def test_accepted_name_parses():
    """The filter must only pass names parse_funding_csv can date."""
    name = "Итоговый фандинг 19-08-2026.csv"
    rows, error = parse_funding_csv(
        name,
        "Neo,% year,% day,Fund curr,MeanPrice,MeanIndex\n"
        "BTCUSDperpA,5.811,0.01592,0.0010942,68733.95,68723.01\n",
    )
    assert wants_file(name) and error is None
    assert rows[0][0] == date(2026, 8, 19)
    assert rows[0][1] == "BTCUSDperpA"


def test_window_backs_off_from_the_newest_stored_day():
    # A day can be posted late, so re-scan a few days behind what we have.
    assert window_start(date(2026, 8, 19), date(2026, 8, 20)) == date(2026, 8, 16)


def test_window_backfills_when_empty():
    assert window_start(None, date(2026, 8, 20)) == date(2026, 2, 21)
