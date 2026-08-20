#!/usr/bin/env python3
"""
Автономный сборщик (без HTTP-сервера).

Используй этот вариант для СЦЕНАРИЯ B из INTEGRATION.md: собирать данные в SQLite
отдельным процессом, а отдавать их наружу — роутами самого трекера, который читает
ту же базу (через функции index_core: fetch_latest, iter_index_rows, ...).

Запуск:  INDEX_DB=/путь/к/index.db python3 collector.py
Прод:    через systemd (юнит crypto-index-collector.service).

Для СЦЕНАРИЯ A (сервис сам отдаёт API) этот файл не нужен — там сборщик крутится
внутри app.py.
"""

import asyncio

import index_core as core

if __name__ == "__main__":
    core.init_db()
    try:
        asyncio.run(core.collector_loop())
    except KeyboardInterrupt:
        print("\nОстановлено.")
