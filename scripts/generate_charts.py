"""Generate ADP movement charts (matplotlib, static PNGs sized for Twitter).

Charts:
  - top 10 risers / top 10 fallers (needs 2+ snapshots)
  - positional ADP distribution with round boundaries (works with 1 snapshot)

Usage: python scripts/generate_charts.py
"""

import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from _common import ADP_DIR, CHARTS_DIR, adp_format, ensure_dirs, league_teams, load_env, today_str
from compute_deltas import compute_deltas, list_snapshots, top_movers

# Chart chrome (validated default palette — light mode; see dataviz palette reference)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
# Risers/fallers are a polarity → the diverging pair's poles
RISER = "#2a78d6"   # cool pole (blue)
FALLER = "#e34948"  # warm pole (red)
# Positions use the categorical order (identity carried by axis rows, not color alone)
POS_COLORS = {"QB": "#2a78d6", "RB": "#008300", "WR": "#e87ba4", "TE": "#eda100"}

FIGSIZE = (12, 6.75)  # 1200x675 @ 100dpi — Twitter card ratio


def _style_ax(ax, x_grid: bool = True) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=10, length=0)
    if x_grid:
        ax.grid(axis="x", color=GRID, linewidth=1)
        ax.set_axisbelow(True)


def _figure(title: str, subtitle: str):
    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=100)
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, x=0.06, y=0.96, ha="left", fontsize=17, fontweight="bold", color=INK)
    ax.set_title(subtitle, loc="left", fontsize=11, color=SECONDARY, pad=16)
    return fig, ax


def _save(fig, name: str) -> str:
    path = CHARTS_DIR / name
    fig.text(0.06, 0.02, f"Data: Fantasy Football Calculator ADP ({adp_format()})", fontsize=8, color=MUTED)
    fig.savefig(path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print(f"  chart: {path.name}")
    return str(path)


def movers_chart(movers: pd.DataFrame, direction: str, prev_date: str, curr_date: str) -> str | None:
    if movers.empty:
        return None
    rising = direction == "risers"
    color = RISER if rising else FALLER
    df = movers.iloc[::-1]  # biggest mover on top
    spots = df["delta"].abs().astype(int)
    labels = [f"{r['name']}  ({r['position']})" for _, r in df.iterrows()]

    title = f"ADP {'Risers' if rising else 'Fallers'} of the Week"
    sub = f"Spots {'risen' if rising else 'fallen'} in overall ADP, {prev_date} → {curr_date}"
    fig, ax = _figure(title, sub)
    bars = ax.barh(labels, spots, height=0.55, color=color)
    for bar, (_, r) in zip(bars, df.iterrows()):
        ax.text(
            bar.get_width() + spots.max() * 0.015,
            bar.get_y() + bar.get_height() / 2,
            f"{int(r['rank_prev'])} → {int(r['rank'])}",
            va="center", fontsize=10, color=SECONDARY,
        )
    _style_ax(ax)
    ax.set_xlabel("ADP spots moved", fontsize=10, color=MUTED)
    ax.tick_params(axis="y", labelcolor=INK, labelsize=11)
    ax.margins(x=0.12)
    return _save(fig, f"{direction}-{curr_date}.png")


def positional_chart(snapshot: pd.DataFrame, date_str: str, top_n: int = 120) -> str:
    """Strip plot of ADP by position — where each position's talent cliff sits."""
    teams = league_teams()
    df = snapshot[snapshot["source"] == "ffc"].nsmallest(top_n, "adp")
    positions = [p for p in ("QB", "RB", "WR", "TE") if p in set(df["position"])]

    fig, ax = _figure(
        f"Positional ADP Distribution — Top {top_n}",
        f"Each dot is a player at their current ADP · vertical lines are {teams}-team round boundaries · {date_str}",
    )
    rounds = int(top_n / teams)
    for rd in range(1, rounds + 1):
        ax.axvline(rd * teams + 0.5, color=GRID, linewidth=1, zorder=0)
    for i, pos in enumerate(positions):
        adps = df[df["position"] == pos]["adp"]
        ax.scatter(
            adps, [i] * len(adps),
            s=90, color=POS_COLORS.get(pos, MUTED),
            edgecolors=SURFACE, linewidths=2, zorder=3,  # surface ring for overlapping dots
        )
    _style_ax(ax, x_grid=False)
    ax.set_yticks(range(len(positions)), positions)
    ax.tick_params(axis="y", labelcolor=INK, labelsize=12)
    ax.set_xticks([rd * teams for rd in range(1, rounds + 1)])
    ax.set_xlabel(f"ADP (end of round marked every {teams} picks)", fontsize=10, color=MUTED)
    ax.set_ylim(-0.7, len(positions) - 0.3)
    ax.invert_yaxis()
    return _save(fig, f"positional-adp-{date_str}.png")


def generate_all(snapshot: pd.DataFrame | None = None) -> dict:
    """Generate every chart the current data supports. Returns {chart_name: path}."""
    load_env()
    ensure_dirs()
    charts: dict = {}

    if snapshot is None:
        snaps = list_snapshots()
        if not snaps:
            raise RuntimeError("No ADP snapshots found — run scripts/fetch_adp.py first.")
        snapshot = pd.read_csv(snaps[-1])
        date_str = snaps[-1].stem
    else:
        date_str = today_str()

    charts["positional"] = positional_chart(snapshot, date_str)

    deltas = compute_deltas()
    if deltas is None:
        print("  (risers/fallers charts skipped — need 2+ snapshots)")
        return charts

    prev_date, curr_date = deltas.attrs["prev_date"], deltas.attrs["curr_date"]
    risers, fallers = top_movers(deltas)
    if (p := movers_chart(risers, "risers", prev_date, curr_date)):
        charts["risers"] = p
    if (p := movers_chart(fallers, "fallers", prev_date, curr_date)):
        charts["fallers"] = p
    return charts


if __name__ == "__main__":
    try:
        generate_all()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
