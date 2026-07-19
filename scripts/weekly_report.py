"""Weekly orchestrator: fetch ADP → compute deltas → generate charts → markdown report.

Works with a single snapshot for the initial report; risers/fallers sections
appear automatically once a second week's snapshot exists.

Usage: python scripts/weekly_report.py
"""

import sys

import pandas as pd

from _common import REPORTS_DIR, ROOT, ensure_dirs, league_teams, load_env, today_str
import fetch_adp
import compute_deltas as deltas_mod
import generate_charts

README_START = "<!-- ADP:START -->"
README_END = "<!-- ADP:END -->"


def movers_table(movers: pd.DataFrame) -> str:
    lines = ["| Player | Pos | Last week | This week | Δ |", "|---|---|---|---|---|"]
    for _, r in movers.iterrows():
        lines.append(
            f"| {r['name']} | {r['position']} | {int(r['rank_prev'])} | {int(r['rank'])} | {int(r['delta']):+d} |"
        )
    return "\n".join(lines)


def scarcity_section(snapshot: pd.DataFrame) -> str:
    """Positional counts in the early rounds — where the cliffs are in this league size."""
    teams = league_teams()
    df = snapshot[snapshot["source"] == "ffc"]
    early = df.nsmallest(teams * 5, "adp")  # first 5 rounds
    counts = early["position"].value_counts()
    lines = [
        f"In an **{teams}-team league** replacement level runs much deeper than standard 10/12-team "
        "ADP assumes — consensus ADP overprices positional scarcity. Composition of the first 5 rounds "
        f"(top {teams * 5} picks):",
        "",
    ]
    for pos, n in counts.items():
        lines.append(f"- **{pos}**: {n} drafted in the first 5 rounds")
    lines.append("")
    lines.append(
        f"Onesie positions (QB/TE) are startable off the waiver wire in an {teams}-teamer — "
        "fade early-round QB/TE unless the tier cliff is real."
    )
    return "\n".join(lines)


def build_report(snapshot: pd.DataFrame, deltas: pd.DataFrame | None, charts: dict) -> str:
    date = today_str()
    ffc = snapshot[snapshot["source"] == "ffc"]
    sources = ", ".join(sorted(snapshot["source"].unique()))

    parts = [
        f"# ADP Report — {date}",
        "",
        f"*{len(ffc)} players tracked · sources: {sources} · {league_teams()}-team PPR*",
        "",
    ]

    if deltas is not None:
        risers, fallers = deltas_mod.top_movers(deltas)
        span = f"{deltas.attrs['prev_date']} → {deltas.attrs['curr_date']}"
        parts += [f"## Top risers ({span})", "", movers_table(risers), ""]
        parts += [f"## Top fallers ({span})", "", movers_table(fallers), ""]
        new = deltas[deltas["is_new"]].nsmallest(10, "adp")
        if not new.empty:
            names = ", ".join(f"{r['name']} ({r['position']}, ADP {r['adp']:.0f})" for _, r in new.iterrows())
            parts += ["## New to the player pool", "", names, ""]
    else:
        parts += [
            "## Movement",
            "",
            "First snapshot recorded — week-over-week risers/fallers will appear in next week's report.",
            "",
        ]

    parts += ["## Positional scarcity", "", scarcity_section(snapshot), ""]

    parts += ["## Charts", ""]
    for name, path in charts.items():
        rel = str(path).replace(str(ROOT) + "/", "")
        parts.append(f"![{name}](../{rel})")
    parts.append("")
    return "\n".join(parts)


def update_readme(charts: dict, date: str) -> None:
    """Refresh the 'Latest ADP Movement' section of README.md between the ADP markers."""
    readme = ROOT / "README.md"
    if not readme.exists():
        return
    text = readme.read_text()
    if README_START not in text or README_END not in text:
        return
    chart = charts.get("risers") or charts.get("positional")
    rel = str(chart).replace(str(ROOT) + "/", "")
    block = (
        f"{README_START}\n"
        f"Latest report: [reports/{date}.md](reports/{date}.md)\n\n"
        f"![latest ADP chart]({rel})\n"
        f"{README_END}"
    )
    head, _, rest = text.partition(README_START)
    _, _, tail = rest.partition(README_END)
    readme.write_text(head + block + tail)
    print("Updated README latest-movement section")


def main() -> None:
    load_env()
    ensure_dirs()

    snapshot = fetch_adp.main()
    print()
    deltas = deltas_mod.compute_deltas()
    print("Generating charts...")
    charts = generate_charts.generate_all(snapshot)
    print()

    date = today_str()
    report_path = REPORTS_DIR / f"{date}.md"
    report_path.write_text(build_report(snapshot, deltas, charts))
    print(f"Report written: reports/{date}.md")
    update_readme(charts, date)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
