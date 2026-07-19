"""Compute week-over-week ADP movement from the two most recent snapshots.

Delta convention: delta = current_rank - previous_rank.
  Negative = rising (drafted earlier than last week)
  Positive = falling (drafted later than last week)

Usage: python scripts/compute_deltas.py
"""

import sys

import pandas as pd

from _common import ADP_DIR, REPORTS_DIR, ensure_dirs, today_str


def list_snapshots() -> list:
    """Snapshot CSVs sorted oldest → newest (filenames are ISO dates, so lexical sort works)."""
    return sorted(p for p in ADP_DIR.glob("*.csv") if not p.name.startswith("deltas"))


def compute_deltas(source: str = "ffc") -> pd.DataFrame | None:
    """Return a delta DataFrame from the two latest snapshots, or None if fewer than 2 exist."""
    snaps = list_snapshots()
    if len(snaps) < 2:
        return None

    prev_path, curr_path = snaps[-2], snaps[-1]
    prev = pd.read_csv(prev_path)
    curr = pd.read_csv(curr_path)
    prev = prev[prev["source"] == source]
    curr = curr[curr["source"] == source]

    merged = curr.merge(
        prev[["name", "position", "rank", "adp"]],
        on=["name", "position"],  # not team — players change teams midsummer
        how="left",
        suffixes=("", "_prev"),
    )
    merged["delta"] = merged["rank"] - merged["rank_prev"]
    merged["adp_delta"] = merged["adp"] - merged["adp_prev"]
    merged["is_new"] = merged["rank_prev"].isna()
    merged.attrs["prev_date"] = prev_path.stem
    merged.attrs["curr_date"] = curr_path.stem
    return merged.sort_values("delta", na_position="last")


def top_movers(deltas: pd.DataFrame, n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(risers, fallers) — the n biggest movers in each direction, excluding new entries."""
    moved = deltas[~deltas["is_new"]].dropna(subset=["delta"])
    risers = moved[moved["delta"] < 0].nsmallest(n, "delta")
    fallers = moved[moved["delta"] > 0].nlargest(n, "delta")
    return risers, fallers


def main() -> None:
    ensure_dirs()
    deltas = compute_deltas()
    if deltas is None:
        n = len(list_snapshots())
        print(f"Need at least 2 snapshots to compute deltas (found {n}).")
        print("Run scripts/fetch_adp.py again next week.")
        return

    out_path = REPORTS_DIR / f"adp-deltas-{today_str()}.csv"
    cols = ["rank", "name", "position", "team", "adp", "rank_prev", "delta", "adp_delta", "is_new"]
    deltas[cols].to_csv(out_path, index=False)
    print(f"Comparing {deltas.attrs['prev_date']} → {deltas.attrs['curr_date']}")
    print(f"Saved: {out_path.name}\n")

    risers, fallers = top_movers(deltas)
    print("Top risers:")
    for _, r in risers.iterrows():
        print(f"  {r['name']:<24} {r['position']:<3} {int(r['rank_prev']):>3} → {int(r['rank']):>3}  ({int(r['delta']):+d})")
    print("\nTop fallers:")
    for _, r in fallers.iterrows():
        print(f"  {r['name']:<24} {r['position']:<3} {int(r['rank_prev']):>3} → {int(r['rank']):>3}  ({int(r['delta']):+d})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
