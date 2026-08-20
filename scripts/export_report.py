"""
Выгрузка данных Crypto Tracker с прода в Excel + PNG-графики.

Тянет уже посчитанные агрегаты с публичного API (http://176.12.70.128/api/...),
SSH и доступ к БД не нужны.  Результат — папка exports/YYYY-MM-DD/ c
report_YYYY-MM-DD.xlsx (плоские листы по каждому датасету) и графиками.

Запуск:
    scripts/.venv/bin/python scripts/export_report.py [--url http://...] [--days 30]
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

# Цвета бирж — фирменные из frontend/src/types/index.ts, но жёлтый Binance и
# циан Hyperliquid притемнены под светлый фон (палитра прогнана через
# CVD-валидатор: все проверки пройдены).
EXCHANGE_COLORS = {
    "binance":     "#c49200",
    "okx":         "#0052ff",
    "bybit":       "#e85720",
    "mexc":        "#0a9e66",
    "hyperliquid": "#0f7fbf",
    "bitget":      "#8659d6",
    "moex":        "#d52b1e",
}
GROUP_COLORS = {"US Market": "#0052ff", "Crypto": "#e85720"}

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
GRID = "#e8e7e3"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "text.color": TEXT,
    "axes.labelcolor": TEXT_MUTED,
    "xtick.color": TEXT_MUTED,
    "ytick.color": TEXT_MUTED,
    "axes.edgecolor": GRID,
    "font.size": 10,
})


def fetch(base: str, path: str, **params):
    r = requests.get(f"{base}{path}", params=params or None, timeout=180)
    r.raise_for_status()
    return r.json()


def _style_axes(ax, unit: str):
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_ylabel(unit)
    ax.margins(x=0.01)


def _pick_unit(max_total: float) -> tuple[float, str]:
    """Делитель и подпись оси по максимальной сумме стека (как pickUnit на фронте)."""
    for div, label in ((1e12, "₽ трлн"), (1e9, "₽ млрд"), (1e6, "₽ млн")):
        if max_total >= div:
            return div, label
    return 1e3, "₽ тыс"


def stacked_bar(df: pd.DataFrame, colors: dict, title: str, out: Path):
    """df: index = период (str), columns = серия, values = ₽."""
    df = df[[c for c in colors if c in df.columns]]  # фиксированный порядок серий
    div, unit = _pick_unit(df.sum(axis=1).max())
    data = df / div

    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=150)
    bottom = np.zeros(len(data))
    for col in data.columns:
        vals = data[col].to_numpy()
        ax.bar(range(len(data)), vals, bottom=bottom, width=0.72,
               color=colors[col], label=col,
               edgecolor=SURFACE, linewidth=1.2)  # зазор между сегментами
        bottom += vals

    step = max(1, len(data) // 10)
    ax.set_xticks(range(0, len(data), step))
    ax.set_xticklabels(data.index[::step], rotation=0)
    _style_axes(ax, unit)
    ax.set_title(title, loc="left", fontsize=12, color=TEXT, pad=12)
    ax.legend(frameon=False, ncols=len(data.columns), loc="upper left",
              bbox_to_anchor=(0, 1.02), fontsize=9)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  chart → {out.name}")


def funding_heatmap(df: pd.DataFrame, title: str, out: Path):
    """df: строки symbol × столбцы date, значения pct_day (среднее по биржам)."""
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

    # зелёный — платят лонгам (отрицательный), красный — лонги платят
    cmap = LinearSegmentedColormap.from_list(
        "funding", ["#0a7d44", "#f2f1ef", "#c0392b"])
    vmax = np.nanpercentile(np.abs(df.to_numpy(dtype=float)), 90) or 0.01
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

    fig, ax = plt.subplots(
        figsize=(max(8, len(df.columns) * 0.35), max(4, len(df) * 0.28)), dpi=150)
    ax.imshow(df.to_numpy(dtype=float), aspect="auto", cmap=cmap, norm=norm)
    ax.set_yticks(range(len(df)))
    ax.set_yticklabels(df.index, fontsize=8)
    step = max(1, len(df.columns) // 12)
    ax.set_xticks(range(0, len(df.columns), step))
    ax.set_xticklabels([d[5:] for d in df.columns[::step]],
                       fontsize=8, rotation=45, ha="right")
    ax.set_title(title, loc="left", fontsize=12, color=TEXT, pad=12)
    cbar = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap),
                        ax=ax, shrink=0.7)
    cbar.set_label("% за день (красный — лонги платят)", fontsize=8)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  chart → {out.name}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://176.12.70.128",
                   help="база трекера (по умолчанию прод)")
    p.add_argument("--days", type=int, default=30, help="дней фандинга в heatmap")
    p.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "exports"))
    args = p.parse_args()

    today = dt.date.today().isoformat()
    out_dir = Path(args.out) / today
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Выгрузка с {args.url} → {out_dir}")

    datasets = {
        "Daily Volume":      "/api/history/daily-volume",
        "Weekly ADTV":       "/api/history/weekly-adtv",
        "Open Interest":     "/api/open-interest/daily",
        "SPB Volume":        "/api/spb/daily-volume",
        "SPB Weekly ADTV":   "/api/spb/weekly-adtv",
        "SPB Open Interest": "/api/spb/open-interest",
    }
    frames: dict[str, pd.DataFrame] = {}
    for name, path in datasets.items():
        print(f"  fetch {path} ...")
        frames[name] = pd.DataFrame(fetch(args.url, path))

    print(f"  fetch /api/funding/heatmap?days={args.days} ...")
    funding = pd.DataFrame(fetch(args.url, "/api/funding/heatmap", days=args.days)["rows"])
    frames["Funding"] = funding

    # ── Excel ────────────────────────────────────────────────────────────────
    xlsx = out_dir / f"report_{today}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        for name, df in frames.items():
            df.to_excel(xw, sheet_name=name, index=False)
    print(f"  excel → {xlsx.name}")

    # ── Графики ──────────────────────────────────────────────────────────────
    dv = frames["Daily Volume"]
    stacked_bar(dv.pivot_table(index="date", columns="exchange",
                               values="volume_rub", aggfunc="sum").fillna(0),
                EXCHANGE_COLORS,
                "Дневной оборот по биржам, 30 дней",
                out_dir / "daily_volume.png")

    wa = frames["Weekly ADTV"]
    stacked_bar(wa.pivot_table(index="week_label", columns="exchange",
                               values="adtv", aggfunc="sum").fillna(0),
                EXCHANGE_COLORS,
                "Недельный ADTV по биржам",
                out_dir / "weekly_adtv.png")

    oi = frames["Open Interest"]
    stacked_bar(oi.pivot_table(index="date", columns="exchange",
                               values="oi_rub", aggfunc="sum").fillna(0),
                EXCHANGE_COLORS,
                "Открытый интерес по биржам, 30 дней",
                out_dir / "open_interest.png")

    spb = frames["SPB Volume"]
    stacked_bar(spb.pivot_table(index="date", columns="group",
                                values="turnover_rub", aggfunc="sum").fillna(0),
                GROUP_COLORS,
                "СПБ Биржа: дневной оборот перпов, 30 дней",
                out_dir / "spb_volume.png")

    spb_oi = frames["SPB Open Interest"]
    stacked_bar(spb_oi.pivot_table(index="date", columns="group",
                                   values="oi_rub", aggfunc="sum").fillna(0),
                GROUP_COLORS,
                "СПБ Биржа: открытый интерес (long+short), 30 дней",
                out_dir / "spb_open_interest.png")

    if not funding.empty:
        hm = funding.pivot_table(index="symbol", columns="date",
                                 values="pct_day", aggfunc="mean")
        funding_heatmap(hm, f"Фандинг, % за день (среднее по биржам), {args.days} дн.",
                        out_dir / "funding_heatmap.png")

    print("Готово.")


if __name__ == "__main__":
    main()
