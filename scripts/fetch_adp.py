"""Fetch current ADP data and save a timestamped snapshot CSV to data/adp/.

Sources:
  - Fantasy Football Calculator (free JSON API, no key) — always fetched
  - FantasyPros consensus ADP (requires FANTASYPROS_API_KEY) — fetched if key set

Usage: python scripts/fetch_adp.py
"""

import os
import sys

import pandas as pd
import requests

from _common import ADP_DIR, current_season, ensure_dirs, league_teams, load_env, today_str

FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/ppr"
FP_URL = "https://api.fantasypros.com/v2/json/nfl/{season}/consensus-rankings"

COLUMNS = ["rank", "name", "position", "team", "adp", "high", "low", "stdev", "bye", "source"]


def fetch_ffc(season: int, teams: int) -> pd.DataFrame:
    resp = requests.get(FFC_URL, params={"teams": teams, "year": season}, timeout=30)
    resp.raise_for_status()
    players = resp.json().get("players", [])
    if not players:
        raise RuntimeError(f"FFC returned no players for season={season} teams={teams}")
    df = pd.DataFrame(players)
    df = df.reindex(columns=["name", "position", "team", "adp", "high", "low", "stdev", "bye"])
    df = df.sort_values("adp").reset_index(drop=True)
    df["rank"] = df.index + 1
    df["source"] = "ffc"
    return df[COLUMNS]


def fetch_fantasypros(season: int, api_key: str) -> pd.DataFrame | None:
    """FantasyPros consensus ADP. Field names vary by endpoint version, so map defensively."""
    try:
        resp = requests.get(
            FP_URL.format(season=season),
            params={"type": "adp", "scoring": "PPR", "position": "ALL"},
            headers={"x-api-key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        players = resp.json().get("players", [])
    except Exception as exc:  # noqa: BLE001 — source is optional, never fail the snapshot
        print(f"  ! FantasyPros fetch failed, continuing with FFC only: {exc}")
        return None
    if not players:
        print("  ! FantasyPros returned no players, continuing with FFC only")
        return None
    rows = []
    for p in players:
        adp = p.get("adp") or p.get("rank_ave") or p.get("rank_ecr")
        rows.append(
            {
                "name": p.get("player_name") or p.get("name"),
                "position": p.get("player_position_id") or p.get("position"),
                "team": p.get("player_team_id") or p.get("team"),
                "adp": float(adp) if adp else None,
                "stdev": p.get("rank_std"),
            }
        )
    df = pd.DataFrame(rows).dropna(subset=["name", "adp"])
    df = df.sort_values("adp").reset_index(drop=True)
    df["rank"] = df.index + 1
    df["source"] = "fantasypros"
    return df.reindex(columns=COLUMNS)


def fetch_snapshot() -> pd.DataFrame:
    """Fetch all available sources and return the combined snapshot."""
    load_env()
    season = current_season()
    teams = league_teams()

    print(f"Fetching ADP for {season} season ({teams}-team PPR)...")
    frames = [fetch_ffc(season, teams)]
    print(f"  FFC: {len(frames[0])} players")

    fp_key = os.environ.get("FANTASYPROS_API_KEY", "")
    if fp_key:
        fp = fetch_fantasypros(season, fp_key)
        if fp is not None:
            frames.append(fp)
            print(f"  FantasyPros: {len(fp)} players")
    else:
        print("  FantasyPros: skipped (no FANTASYPROS_API_KEY in .env)")

    return pd.concat(frames, ignore_index=True)


def main() -> "pd.DataFrame":
    ensure_dirs()
    snapshot = fetch_snapshot()
    out_path = ADP_DIR / f"{today_str()}.csv"
    snapshot.to_csv(out_path, index=False)
    print(f"Saved snapshot: {out_path.relative_to(ADP_DIR.parent.parent)} ({len(snapshot)} rows)")
    return snapshot


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
