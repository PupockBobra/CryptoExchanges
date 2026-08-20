#!/usr/bin/env python3
"""
Ядро репликатора криптоиндекса MOEX (без веб-фреймворка).

Здесь всё, что не зависит от способа отдачи наружу:
  - конфигурация (монеты, веса бирж, шаг/окно);
  - фетчеры цен с 4 бирж;
  - хранилище SQLite (init/запись/чтение);
  - фоновый цикл сборщика (collector_loop);
  - функции чтения для API/интеграции (latest, stats, диапазоны, снимок базы).

Этот модуль импортируют:
  - app.py       — самостоятельный сервис (сборщик + HTTP-API + дашборд);
  - collector.py — только сборщик (когда данные читает бэкенд трекера напрямую);
  - бэкенд трекера — при желании может импортировать функции чтения (fetch_latest,
    iter_index_rows, ...) и отдавать их своими роутами.

Единственное требование к среде: Python 3.10+ и пакет httpx.
ВАЖНО: и сборщик, и любой читатель должны указывать на ОДИН И ТОТ ЖЕ файл базы
через переменную окружения INDEX_DB (иначе будут читать пустую базу).

Методические допущения (СВЕРЬ С МЕТОДИКОЙ MOEX):
  - цена = last trade (последняя сделка) со спота, пара к USDT;
  - индекс монеты = взвешенное среднее биржевых минутных средних; если биржа не
    ответила на такте, её вес исключается, веса оставшихся перенормируются к 1;
  - индекс пересчитывается на каждом такте (каждые 15с) на окне последней минуты.
"""

import asyncio
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone

import httpx

# ========================= Конфигурация =========================
COINS = ["BTC", "ETH", "SOL", "TRX", "XRP"]

WEIGHTS = {
    "binance": 0.50,
    "bybit":   0.20,
    "okx":     0.15,
    "bitget":  0.15,
}

POLL_SEC     = 15          # шаг опроса, сек
WINDOW_SEC   = 60          # окно усреднения (последняя минута), сек
QUOTE        = "USDT"      # котировальная валюта пары
HTTP_TIMEOUT = 8.0         # общий таймаут запроса к бирже, сек
CONNECT_TIMEOUT = 5.0      # таймаут установки TCP-соединения, сек
RETRY_ATTEMPTS  = 3        # попыток на биржу в пределах одного такта
RETRY_BACKOFF   = 0.5      # пауза между попытками, сек
USER_AGENT   = "moex-crypto-index-replicator/1.0"

DB_PATH = os.environ.get(
    "INDEX_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "index.db"),
)
# ----------------------------------------------------------------

EXCHANGES = list(WEIGHTS.keys())

# Символы под формат каждой биржи -> обратный маппинг для фильтрации.
SYMBOLS = {
    "binance": {f"{c}{QUOTE}":  c for c in COINS},   # BTCUSDT
    "bybit":   {f"{c}{QUOTE}":  c for c in COINS},   # BTCUSDT
    "okx":     {f"{c}-{QUOTE}": c for c in COINS},   # BTC-USDT
    "bitget":  {f"{c}{QUOTE}":  c for c in COINS},   # BTCUSDT
}

# Заголовки CSV (используются и в API, и при прямой интеграции).
TICKS_HEADER = ["ts", "iso_utc", "exchange", "coin", "price"]
INDEX_HEADER = ["ts", "iso_utc", "coin", "binance", "bybit", "okx", "bitget",
                "index_value", "n_exchanges", "min_samples", "missing_now", "flag"]

# Значения поля flag в index_values:
#   ok               — все 4 биржи, полное окно из 4 сэмплов, свежая цена на этом такте;
#   PARTIAL_WINDOW   — окно неполное (<4 сэмплов): разогрев после старта или пропуск внутри минуты;
#   STALE            — на ЭТОМ такте от биржи не пришло свежей цены (взято из окна) — см. missing_now;
#   MISSING_EXCHANGE — биржа выпала из окна целиком → веса перенормированы, индекс отклоняется от методики.
FLAG_OK = "ok"


# ============================ Время =============================
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_boundary(interval: int) -> float:
    now = time.time()
    return (now // interval + 1) * interval


def parse_time(s):
    """None | unix-секунды (int/str) | ISO8601 -> unix-секунды UTC | None."""
    if s is None or s == "":
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# =========================== Фетчеры ============================
# Каждый возвращает {монета: цена(float)}. При ошибке пробрасывает исключение.

async def fetch_binance(client: httpx.AsyncClient) -> dict:
    r = await client.get("https://api.binance.com/api/v3/ticker/price")
    r.raise_for_status()
    want = SYMBOLS["binance"]
    return {want[x["symbol"]]: float(x["price"])
            for x in r.json() if x["symbol"] in want}


async def fetch_bybit(client: httpx.AsyncClient) -> dict:
    r = await client.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "spot"})
    r.raise_for_status()
    want = SYMBOLS["bybit"]
    return {want[x["symbol"]]: float(x["lastPrice"])
            for x in r.json()["result"]["list"] if x["symbol"] in want}


async def fetch_okx(client: httpx.AsyncClient) -> dict:
    r = await client.get("https://www.okx.com/api/v5/market/tickers",
                         params={"instType": "SPOT"})
    r.raise_for_status()
    want = SYMBOLS["okx"]
    return {want[x["instId"]]: float(x["last"])
            for x in r.json()["data"] if x["instId"] in want}


async def fetch_bitget(client: httpx.AsyncClient) -> dict:
    r = await client.get("https://api.bitget.com/api/v2/spot/market/tickers")
    r.raise_for_status()
    want = SYMBOLS["bitget"]
    return {want[x["symbol"]]: float(x["lastPr"])
            for x in r.json()["data"] if x["symbol"] in want}


FETCHERS = {
    "binance": fetch_binance,
    "bybit":   fetch_bybit,
    "okx":     fetch_okx,
    "bitget":  fetch_bitget,
}


def make_client() -> httpx.AsyncClient:
    """Клиент с раздельными таймаутами и авто-ретраем на уровне соединения."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT),
        transport=httpx.AsyncHTTPTransport(retries=2),  # ретрай на сбоях установки соединения
        headers={"User-Agent": USER_AGENT},
    )


async def fetch_one(client: httpx.AsyncClient, ex: str):
    """Опрос одной биржи с ретраями. -> (данные|None, число_попыток, ошибка|None)."""
    last = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await FETCHERS[ex](client), attempt, None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}".strip()
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF)
    return None, RETRY_ATTEMPTS, last or "unknown error"


async def poll_all(client: httpx.AsyncClient):
    """Одновременный опрос всех бирж с ретраями.
    -> (prices={биржа:{монета:цена}}, diag={биржа:{attempts, error}})."""
    results = await asyncio.gather(*[fetch_one(client, ex) for ex in EXCHANGES])
    prices, diag = {}, {}
    for ex, (data, attempts, err) in zip(EXCHANGES, results):
        prices[ex] = data or {}
        diag[ex] = {"attempts": attempts, "error": err}
        if err:
            print(f"[{now_iso()}] [!] {ex}: не удалось за {attempts} попыток -> {err}",
                  flush=True)
        elif attempts > 1:
            print(f"[{now_iso()}] [~] {ex}: успех с {attempts}-й попытки", flush=True)
    return prices, diag


# ========================== Хранилище ===========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(db_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ticks (
                ts       INTEGER NOT NULL,   -- граница такта, unix-секунды UTC
                exchange TEXT    NOT NULL,
                coin     TEXT    NOT NULL,
                price    REAL    NOT NULL,
                PRIMARY KEY (ts, exchange, coin)
            );
            CREATE INDEX IF NOT EXISTS idx_ticks_coin_ts ON ticks(coin, ts);

            CREATE TABLE IF NOT EXISTS index_values (
                ts          INTEGER NOT NULL,   -- граница такта, unix-секунды UTC
                coin        TEXT    NOT NULL,
                binance     REAL,               -- минутное среднее по бирже
                bybit       REAL,
                okx         REAL,
                bitget      REAL,
                index_value REAL,               -- итоговый композит
                n_exchanges INTEGER,            -- сколько бирж участвовало
                min_samples INTEGER,            -- мин. число сэмплов в окне по биржам (4 = полное)
                missing_now TEXT,               -- биржи без свежей цены на этом такте (через запятую)
                flag        TEXT,               -- ok | PARTIAL_WINDOW | STALE | MISSING_EXCHANGE
                PRIMARY KEY (ts, coin)
            );
            CREATE INDEX IF NOT EXISTS idx_index_coin_ts ON index_values(coin, ts);
            """
        )
        conn.commit()


def store_ticks(conn: sqlite3.Connection, tick: int, prices: dict):
    rows = [(tick, ex, coin, price)
            for ex, cp in prices.items() for coin, price in cp.items()]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO ticks (ts, exchange, coin, price) VALUES (?,?,?,?)",
            rows,
        )


def store_index(conn: sqlite3.Connection, tick: int):
    """Минутные средние по биржам из ticks -> взвешенный композит + флаги качества."""
    expected = WINDOW_SEC // POLL_SEC  # сколько сэмплов в полном окне (=4)
    # средние и число сэмплов в окне по каждой (бирже, монете)
    cur = conn.execute(
        "SELECT exchange, coin, AVG(price), COUNT(*) FROM ticks "
        "WHERE ts > ? AND ts <= ? GROUP BY exchange, coin",
        (tick - WINDOW_SEC, tick),
    )
    avg, cnt = {}, {}
    for ex, coin, mean, c in cur.fetchall():
        avg.setdefault(coin, {})[ex] = mean
        cnt.setdefault(coin, {})[ex] = c
    # какие (биржа, монета) реально пришли ИМЕННО на этом такте
    fresh = {}
    for ex, coin in conn.execute(
            "SELECT DISTINCT exchange, coin FROM ticks WHERE ts = ?", (tick,)).fetchall():
        fresh.setdefault(coin, set()).add(ex)

    for coin in COINS:
        per_ex = avg.get(coin, {})
        counts = cnt.get(coin, {})
        num = wsum = 0.0
        n = 0
        for ex in EXCHANGES:
            v = per_ex.get(ex)
            if v is not None:
                num += WEIGHTS[ex] * v
                wsum += WEIGHTS[ex]
                n += 1
        index_value = (num / wsum) if wsum > 0 else None

        min_samples = min((counts.get(ex, 0) for ex in EXCHANGES), default=0)
        fresh_set = fresh.get(coin, set())
        missing_now = [ex for ex in EXCHANGES if ex not in fresh_set]
        if n < len(EXCHANGES):
            flag = "MISSING_EXCHANGE"    # биржа отсутствует в окне целиком -> веса перенормированы
        elif missing_now:
            flag = "STALE"              # на этом такте нет свежей цены, взято из окна
        elif min_samples < expected:
            flag = "PARTIAL_WINDOW"     # окно ещё неполное (разогрев/пропуск внутри минуты)
        else:
            flag = FLAG_OK

        conn.execute(
            "INSERT OR REPLACE INTO index_values "
            "(ts, coin, binance, bybit, okx, bitget, index_value, n_exchanges, "
            " min_samples, missing_now, flag) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tick, coin, per_ex.get("binance"), per_ex.get("bybit"),
             per_ex.get("okx"), per_ex.get("bitget"), index_value, n,
             min_samples, ",".join(missing_now), flag),
        )


# ===================== Функции чтения (для API/интеграции) =====================
def fetch_latest() -> dict:
    """Последнее значение индекса по всем монетам + биржевые средние."""
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT MAX(ts) FROM index_values").fetchone()
        last_ts = row[0]
        if last_ts is None:
            return {"ts": None, "iso": None, "weights": WEIGHTS, "coins": []}
        cur = conn.execute(
            "SELECT coin, binance, bybit, okx, bitget, index_value, n_exchanges, "
            "min_samples, missing_now, flag "
            "FROM index_values WHERE ts = ?", (last_ts,))
        by_coin = {r[0]: r for r in cur.fetchall()}
    coins = []
    for c in COINS:
        r = by_coin.get(c)
        if r:
            coins.append({"coin": c, "index": r[5], "n_exchanges": r[6],
                          "binance": r[1], "bybit": r[2], "okx": r[3], "bitget": r[4],
                          "min_samples": r[7],
                          "missing_now": (r[8].split(",") if r[8] else []),
                          "flag": r[9]})
    return {"ts": last_ts, "iso": iso_of(last_ts), "weights": WEIGHTS, "coins": coins}


def fetch_stats() -> dict:
    with closing(db_connect()) as conn:
        t = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ticks").fetchone()
        i = conn.execute("SELECT COUNT(*) FROM index_values").fetchone()
    n_ticks, min_ts, max_ts = t
    return {
        "ticks_rows": n_ticks, "index_rows": i[0],
        "first_ts": min_ts, "first_iso": iso_of(min_ts) if min_ts else None,
        "last_ts": max_ts, "last_iso": iso_of(max_ts) if max_ts else None,
        "coins": COINS, "exchanges": EXCHANGES, "weights": WEIGHTS,
        "poll_sec": POLL_SEC, "window_sec": WINDOW_SEC,
    }


def resolve_range(frm, to, default_span: int):
    """Границы диапазона в unix-секундах. Без from -> последние default_span секунд."""
    t_to = parse_time(to) or int(time.time())
    t_from = parse_time(frm)
    if t_from is None:
        t_from = t_to - default_span
    return t_from, t_to


def iter_index_rows(t_from: int, t_to: int, coin: str | None = None):
    """Генератор строк index_values как кортежей по INDEX_HEADER."""
    sql = ("SELECT ts, coin, binance, bybit, okx, bitget, index_value, n_exchanges, "
           "min_samples, missing_now, flag "
           "FROM index_values WHERE ts >= ? AND ts <= ? ")
    params = [t_from, t_to]
    if coin:
        sql += "AND coin = ? "
        params.append(coin.upper())
    sql += "ORDER BY ts, coin"
    with closing(db_connect()) as conn:
        for r in conn.execute(sql, params):
            yield (r[0], iso_of(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                   r[8], r[9], r[10])


def iter_ticks_rows(t_from: int, t_to: int, coin: str | None = None):
    """Генератор строк ticks (сырьё) как кортежей по TICKS_HEADER."""
    sql = "SELECT ts, exchange, coin, price FROM ticks WHERE ts >= ? AND ts <= ? "
    params = [t_from, t_to]
    if coin:
        sql += "AND coin = ? "
        params.append(coin.upper())
    sql += "ORDER BY ts, coin, exchange"
    with closing(db_connect()) as conn:
        for r in conn.execute(sql, params):
            yield (r[0], iso_of(r[0]), r[1], r[2], r[3])


def snapshot_db() -> str:
    """Консистентный снимок всей базы во временный файл. Возвращает путь (удали после отдачи)."""
    fd, snap = tempfile.mkstemp(prefix="index_snap_", suffix=".db",
                                dir=os.path.dirname(DB_PATH))
    os.close(fd)
    os.remove(snap)  # VACUUM INTO требует, чтобы целевого файла не существовало
    with closing(db_connect()) as conn:
        conn.execute("VACUUM INTO ?", (snap,))
    return snap


# ====================== Фоновый сборщик =========================
async def collector_loop():
    print(f"[{now_iso()}] Сборщик запущен. Монеты: {', '.join(COINS)} | пара к {QUOTE} "
          f"| база: {DB_PATH}", flush=True)
    print(f"[{now_iso()}] Веса: " +
          ", ".join(f"{k} {v:.0%}" for k, v in WEIGHTS.items()), flush=True)
    async with make_client() as client:
        while True:
            target = next_boundary(POLL_SEC)
            await asyncio.sleep(max(0.0, target - time.time()))
            tick = int(round(target))
            try:
                prices, diag = await poll_all(client)
                with closing(db_connect()) as conn:
                    store_ticks(conn, tick, prices)
                    store_index(conn, tick)
                    conn.commit()
                got = sum(len(v) for v in prices.values())
                failed = [ex for ex in EXCHANGES if diag[ex]["error"]]
                if failed:
                    print(f"[{iso_of(tick)}] ВНИМАНИЕ: нет свежих данных от "
                          f"{', '.join(failed)} — индекс на этом такте помечен флагом "
                          f"(STALE/MISSING_EXCHANGE)", flush=True)
                else:
                    print(f"[{iso_of(tick)}] ok: записано сэмплов {got} (4/4 бирж)",
                          flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[{now_iso()}] [!] ошибка такта: {type(e).__name__}: {e}",
                      flush=True)
