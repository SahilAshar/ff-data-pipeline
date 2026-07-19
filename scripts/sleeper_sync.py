"""Sync league data from the Sleeper API (no auth required) into data/league/.

Pulls: NFL state, league settings, rosters, users, and the full player DB
(~5MB, cached and refreshed at most once per day).

Usage: python scripts/sleeper_sync.py
"""

import datetime
import json
import os
import sys

import requests

from _common import LEAGUE_DIR, ensure_dirs, load_env

BASE = "https://api.sleeper.app/v1"


def get(path: str):
    resp = requests.get(f"{BASE}{path}", timeout=60)
    resp.raise_for_status()
    return resp.json()


def save(name: str, data) -> None:
    path = LEAGUE_DIR / name
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved {name}")


def sync_players() -> None:
    """Full player DB — Sleeper asks that this be fetched at most daily, so cache by mtime."""
    path = LEAGUE_DIR / "players.json"
    if path.exists():
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(path.stat().st_mtime)
        if age < datetime.timedelta(days=1):
            print(f"  players.json is fresh ({age.seconds // 3600}h old), skipping")
            return
    players = get("/players/nfl")
    save("players.json", players)
    print(f"  ({len(players)} players in DB)")


def sync_league(league_id: str) -> None:
    league = get(f"/league/{league_id}")
    save("league.json", league)
    print(f"  league: {league['name']} ({league['settings'].get('num_teams', '?')} teams, {league['season']})")

    rosters = get(f"/league/{league_id}/rosters")
    save("rosters.json", rosters)
    print(f"  ({len(rosters)} rosters)")

    users = get(f"/league/{league_id}/users")
    save("users.json", users)

    draft_id = league.get("draft_id")
    if draft_id:
        picks = get(f"/draft/{draft_id}/picks")
        save("draft_picks.json", picks)
        print(f"  ({len(picks)} draft picks recorded)")


def main() -> None:
    load_env()
    ensure_dirs()

    print("Syncing Sleeper data...")
    state = get("/state/nfl")
    save("state.json", state)
    print(f"  NFL state: {state['season']} {state['season_type']}, week {state['week']}")

    sync_players()

    league_id = os.environ.get("SLEEPER_LEAGUE_ID", "")
    if league_id:
        sync_league(league_id)
    else:
        print("  ! SLEEPER_LEAGUE_ID not set in .env — skipping league/roster sync")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
