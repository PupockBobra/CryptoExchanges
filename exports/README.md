# Exports — готовые выгрузки данных

Сюда складываются выгрузки с прода (http://176.12.70.128): по папке на дату,
внутри — Excel со всеми данными и PNG-графики.

```
exports/2026-07-31/
  report_2026-07-31.xlsx   ← листы: Daily Volume, Weekly ADTV, Open Interest,
                             SPB Volume, SPB Weekly ADTV, SPB Open Interest, Funding
  daily_volume.png         ← дневной оборот по биржам, 30 дней
  weekly_adtv.png          ← недельный ADTV по биржам
  open_interest.png        ← открытый интерес по биржам, 30 дней
  spb_volume.png           ← СПБ: дневной оборот (US Market / Crypto)
  spb_open_interest.png    ← СПБ: открытый интерес (long+short)
  funding_heatmap.png      ← фандинг, % за день (среднее по биржам)
```

## Как сделать свежую выгрузку

```bash
scripts/.venv/bin/python scripts/export_report.py
```

Опции: `--url` (другой сервер, напр. `http://localhost:8000` для локального),
`--days N` (окно фандинга, по умолчанию 30).

Первичная установка окружения (уже сделана):

```bash
cd scripts && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Данные берутся с публичного API трекера — SSH/токены не нужны. Папка не
попадает в git (`exports/` в .gitignore), кроме этого README.
