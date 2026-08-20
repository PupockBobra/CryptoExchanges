# Инструкция по установке и интеграции в трекер

> Читает **Claude/разработчик на стороне сервера трекера** (`http://176.12.70.128`).
> Цель: поднять сборщик криптоиндекса и добавить в трекер новую вкладку «Криптоиндекс».
> Трекер на Python. Мой сервис — **отдельный процесс**, от стека трекера не зависит.

## 0. Что это и как устроено
Сборщик раз в 15с одновременно опрашивает Binance, Bybit, OKX, Bitget по 5 монетам
(BTC, ETH, SOL, TRX, XRP, спот к USDT), пишет **каждую сырую цену** в SQLite и считает
индекс на скользящем окне 60с (веса: Binance 50, Bybit 20, OKX 15, Bitget 15). Наружу —
JSON/CSV API и снимок базы (контракт — в `API.md`). Данные хранятся **всегда** (ретеншена нет).

Файлы пакета:
```
index_core.py        ядро: конфиг, фетчеры, SQLite, сборщик, функции чтения
app.py               Сценарий A: сборщик + HTTP-API + служебный дашборд (FastAPI)
collector.py         Сценарий B: только сборщик (API отдаёт сам трекер)
requirements.txt     httpx (+ fastapi, uvicorn для сценария A)
systemd/*.service    юниты для автозапуска
frontend/tab-example.html   референс вкладки (fetch API → карточки + выгрузка)
API.md               контракт эндпоинтов
README.md            краткий обзор
```

## 1. Выбери сценарий
| | **A. Отдельный сервис с API** (проще) | **B. Читает сам трекер** |
|---|---|---|
| Сборщик | внутри `app.py` | `collector.py` отдельным демоном |
| Кто отдаёт данные | FastAPI этого пакета (порт 8787) | роуты самого трекера |
| Нужен reverse-proxy | да (подпуть → :8787) | нет |
| Фронт вкладки берёт данные с | `/crypto-index/api/...` | твоих роутов трекера |

**Рекомендация:** начни со **сценария A** — он не трогает код трекера, только добавляет
проксируемый подпуть и вкладку-фронт. Сценарий B бери, если не хочешь второй порт/сервис
и предпочитаешь отдавать данные роутами трекера.

---

## 2. Общий шаг для обоих сценариев — установка
```bash
sudo mkdir -p /opt/crypto-index /var/lib/crypto-index
sudo chown "$USER" /opt/crypto-index /var/lib/crypto-index
# скопируй файлы пакета в /opt/crypto-index (scp/git — как удобно)

cd /opt/crypto-index
python3 --version                      # нужен 3.10+
python3 -m venv venv
./venv/bin/pip install -r requirements.txt   # для B достаточно: pip install httpx
```
База будет жить в `/var/lib/crypto-index/index.db` (см. `INDEX_DB` в юнитах). Путь важен:
в сценарии B трекер должен читать **этот же** файл.

---

## 3A. Сценарий A — сервис с API

### 3A.1 Автозапуск (systemd)
```bash
# при необходимости поправь User/пути/порт в файле:
sudo cp /opt/crypto-index/systemd/crypto-index.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-index
systemctl status crypto-index
journalctl -u crypto-index -f          # должны идти строки "записано сэмплов: 20 (бирж ответило: 4/4)"
```
Проверка локально на сервере:
```bash
curl -s http://127.0.0.1:8787/api/stats
curl -s http://127.0.0.1:8787/api/latest
```

### 3A.2 Проксирование под домен трекера (подпуть `/crypto-index/`)
Сервис слушает только `127.0.0.1:8787`. Наружу его отдаёт веб-сервер трекера.
**Сначала определи, что стоит перед трекером:**
```bash
systemctl status nginx apache2 caddy 2>/dev/null | grep -E 'nginx|apache|caddy|Active'
ss -ltnp | grep ':80\|:443'
```
Затем добавь один блок (проксируем `/crypto-index/` → сервис, срезая префикс):

**nginx** (в тот же `server {}`, где трекер):
```nginx
location /crypto-index/ {
    proxy_pass http://127.0.0.1:8787/;   # завершающий слэш срезает префикс
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
после правки: `sudo nginx -t && sudo systemctl reload nginx`

**Caddy** (в блоке сайта трекера):
```caddy
handle_path /crypto-index/* {
    reverse_proxy 127.0.0.1:8787
}
```
после правки: `sudo systemctl reload caddy`

**Apache** (mod_proxy включён: `a2enmod proxy proxy_http`):
```apache
ProxyPass        /crypto-index/ http://127.0.0.1:8787/
ProxyPassReverse /crypto-index/ http://127.0.0.1:8787/
```
после правки: `sudo systemctl reload apache2`

Проверка снаружи: открой `http(s)://<домен-трекера>/crypto-index/` — увидишь служебный
дашборд; `…/crypto-index/api/latest` — JSON.

> Если трекер работает по HTTPS, а сервис по http — это **не** проблема mixed-content,
> потому что браузер ходит на тот же `https://…/crypto-index/…`, а расшифровка http идёт
> уже внутри сервера (proxy_pass на localhost). Ничего дополнительно настраивать не нужно.

### 3A.3 Вкладка во фронте трекера
В `frontend/tab-example.html` установи `API_BASE = "/crypto-index"` и перенеси разметку/JS
в стиль трекера (свои классы/тему). Логика: `GET /crypto-index/api/latest` каждые 15с →
карточки по монетам; кнопки выгрузки строят ссылки на `/crypto-index/api/{index,ticks}.csv`
и `/crypto-index/api/db`. Полный контракт — `API.md`. **Переходи к разделу 4.**

---

## 3B. Сценарий B — данные отдаёт сам трекер

### 3B.1 Автозапуск только сборщика
```bash
sudo cp /opt/crypto-index/systemd/crypto-index-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-index-collector
journalctl -u crypto-index-collector -f
```

### 3B.2 Роуты в трекере (Python)
Трекер импортирует функции чтения из `index_core` и отдаёт их своими роутами. **Условие:**
процесс трекера видит `index_core.py` (общий путь или установка в его venv) и переменная
`INDEX_DB` указывает на **тот же** файл базы, что у сборщика.

Пример (Flask; для FastAPI/Django — аналогично):
```python
import os
os.environ.setdefault("INDEX_DB", "/var/lib/crypto-index/index.db")
import index_core as core   # index_core.py доступен трекеру

@app.get("/api/crypto-index/latest")
def ci_latest():
    return core.fetch_latest()          # dict → json

@app.get("/api/crypto-index/stats")
def ci_stats():
    return core.fetch_stats()

@app.get("/api/crypto-index/index.csv")
def ci_index_csv():
    frm = request.args.get("from"); to = request.args.get("to")
    coin = request.args.get("coin")
    t0, t1 = core.resolve_range(frm, to, 86400)
    def gen():
        yield ",".join(core.INDEX_HEADER) + "\n"
        for row in core.iter_index_rows(t0, t1, coin):
            yield ",".join("" if x is None else str(x) for x in row) + "\n"
    return Response(gen(), mimetype="text/csv")
# ticks.csv — то же самое через core.iter_ticks_rows / core.TICKS_HEADER (default 3600)
```
Альтернатива без импорта — читать SQLite напрямую (схема ниже).

### 3B.3 Вкладка
В `frontend/tab-example.html` укажи `API_BASE = "/api/crypto-index"` (или где твои роуты)
и перенеси в стиль трекера. **Переходи к разделу 4.**

---

## 4. Проверка (чек-лист)
- [ ] `journalctl` сборщика: идут такты «ok: записано сэмплов 20 (4/4 бирж)».
- [ ] `.../api/stats`: `last_ts` отстаёт от now не более чем на ~20с, растут `ticks_rows`.
- [ ] `.../api/latest`: у всех 5 монет `index` не null, `n_exchanges: 4`, `flag` = `ok`
      (в первые ~45с после старта возможен `PARTIAL_WINDOW` — это нормально, окно набирается).
- [ ] **Контроль качества:** в `index.csv` колонка `flag` — почти всегда `ok`. Массовые
      `STALE`/`MISSING_EXCHANGE` = проблемы с сетью сервера или доступом к бирже, разбирайся
      по `journalctl` (строки `[!] <биржа>: не удалось за N попыток`). Для строгой сверки с
      методикой бери только `flag == ok`.
- [ ] Сверка формулы: для строки с `flag=ok` вручную посчитай
      `binance·0.5 + bybit·0.2 + okx·0.15 + bitget·0.15` = `index_value`.
- [ ] Вкладка в трекере обновляется раз в 15с, кнопки выгрузки скачивают CSV, флаг качества виден.
- [ ] `.../api/db` скачивает валидный SQLite (открой: `sqlite3 f.db 'SELECT COUNT(*) FROM ticks;'`).
- [ ] После `reboot` сервисы поднимаются сами (`systemctl is-enabled ...`).

## 5. Обслуживание
- Логи: `journalctl -u crypto-index -f` (или `-collector`).
- Рестарт: `sudo systemctl restart crypto-index`.
- Размер базы растёт на ~2–3 ГБ/год (ретеншена нет — так и просили). Если позже понадобится
  чистить сырьё: удалять старые строки `ticks` (`DELETE FROM ticks WHERE ts < ?`), `index_values`
  не трогать. Скажите — добавлю готовую задачу ротации.

## 6. Схема БД (для прямого доступа/проверки)
```sql
ticks(ts INTEGER, exchange TEXT, coin TEXT, price REAL, PRIMARY KEY(ts,exchange,coin))
index_values(ts INTEGER, coin TEXT, binance REAL, bybit REAL, okx REAL, bitget REAL,
             index_value REAL, n_exchanges INTEGER, PRIMARY KEY(ts,coin))
-- ts — unix-секунды UTC (граница 15с-такта). SQLite в режиме WAL.
```

## 7. Методические допущения — СВЕРИТЬ С МЕТОДИКОЙ MOEX
Прежде чем считать реплику «точной», подтвердите (это заложено в код, меняется в шапке
`index_core.py`): **цена = last trade** (не mid по стакану); **пропуск биржи → перенормировка
весов** оставшихся; **индекс каждые 15с** на окне последней минуты; **пары к USDT** (пересчёт
в RUB по фиксингу — отдельный слой, если нужен). Если методика отличается — правки минимальны,
запросите у постановщика.
