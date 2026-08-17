"""Pre-kickoff lineup guard.

Checks one Sleeper roster's *current* starting lineup for anything that will
score zero or is at risk of it — empty slots, players on bye, players ruled
Out/Doubtful/IR/PUP/Suspended, Questionable tags, free agents — and suggests the
best bench replacement for each flagged slot using Sleeper's weekly half-PPR
projections. It also lists plain projection swaps (a bench player projected
meaningfully higher than a starter he could replace).

Motivation: 2025 week 6 lost 24 bench points to two inactive starters. The
weekly start/sit backtest showed lineup *skill* was already at the
projection-following ceiling; the leak was availability hygiene. This script is
that hygiene, run before every kickoff window.

Outputs
  reports/lineup-guard/latest.md      human report (always overwritten)
  reports/lineup-guard/state.json     machine state for change detection
  stdout                              the same report

Exit code is always 0 unless the API is unreachable. Alerting is the caller's
job (see .github/workflows/lineup-guard.yml, which opens/updates a GitHub Issue
when the alert set changes).

Usage
  python scripts/lineup_guard.py                # current NFL week, quiet outside the season
  python scripts/lineup_guard.py --week 7       # specific week
  python scripts/lineup_guard.py --force        # run even in preseason/offseason
  python scripts/lineup_guard.py --user-id ...  # someone else's roster

Env: SLEEPER_LEAGUE_ID (required), SLEEPER_USER_ID (default: 0xAshar).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

from _common import ROOT, load_env

API = "https://api.sleeper.app/v1"
PROJ_API = "https://api.sleeper.com/projections/nfl"
HEADERS = {"User-Agent": "ff-data-pipeline lineup-guard/1.0"}
OUT_DIR = ROOT / "reports" / "lineup-guard"

DEFAULT_USER_ID = "656303088369512448"  # 0xAshar

# Slot -> positions eligible for it. Sleeper roster_positions uses these names.
SLOT_ELIGIBLE = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
}

# Sleeper injury_status values that mean "will not play".
RED_STATUS = {"Out", "Doubtful", "IR", "PUP", "Sus", "COV", "NA", "DNR"}
YELLOW_STATUS = {"Questionable"}

SWAP_MARGIN = 2.0  # projected pts a bench player must exceed a starter by to be listed

ALL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA", "MIN", "NE",
    "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS",
}


def get(url: str, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def et_now() -> dt.datetime:
    """Wall-clock in America/New_York regardless of runner TZ."""
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # pragma: no cover
        return dt.datetime.now()


def fetch_projections(season: str, week: int) -> dict[str, dict]:
    """player_id -> {pts, opp, game_id, date}. Also used to infer byes."""
    url = f"{PROJ_API}/{season}/{week}"
    params = [
        ("season_type", "regular"),
        ("order_by", "pts_half_ppr"),
    ] + [("position[]", p) for p in ("QB", "RB", "WR", "TE", "K", "DEF")]
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    out = {}
    for row in r.json():
        stats = row.get("stats") or {}
        out[row["player_id"]] = {
            "pts": stats.get("pts_half_ppr"),
            "opp": row.get("opponent"),
            "game_id": row.get("game_id"),
            "date": row.get("date"),
            "team": row.get("team"),
        }
    return out


def teams_playing(proj: dict[str, dict]) -> set[str]:
    return {p["team"] for p in proj.values() if p.get("game_id") and p.get("team")}


def player_line(p: dict, pid: str) -> str:
    if pid == "0" or not p:
        return "(empty)"
    name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
    if p.get("position") == "DEF":
        name = f"{p.get('team')} DEF"
    return f"{name} ({p.get('position')}, {p.get('team') or 'FA'})"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--force", action="store_true", help="run even outside regular/post season")
    ap.add_argument("--user-id", default=os.environ.get("SLEEPER_USER_ID", DEFAULT_USER_ID))
    ap.add_argument("--league-id", default=os.environ.get("SLEEPER_LEAGUE_ID"))
    args = ap.parse_args()
    if not args.league_id:
        print("SLEEPER_LEAGUE_ID is not set", file=sys.stderr)
        return 2

    state = get(f"{API}/state/nfl")
    season = state["season"]
    season_type = state.get("season_type")
    week = args.week or state.get("week") or 1
    in_season = season_type in ("regular", "post")
    if not in_season and not args.force:
        print(f"season_type={season_type}: not in season, nothing to do (use --force to run anyway)")
        return 0

    league = get(f"{API}/league/{args.league_id}")
    slots = [s for s in league["roster_positions"] if s != "BN"]
    rosters = get(f"{API}/league/{args.league_id}/rosters")
    users = {u["user_id"]: u for u in get(f"{API}/league/{args.league_id}/users")}
    mine = next((r for r in rosters if r.get("owner_id") == args.user_id), None)
    if not mine:
        print(f"no roster owned by user {args.user_id} in league {args.league_id}", file=sys.stderr)
        return 2
    display = users.get(args.user_id, {}).get("display_name", args.user_id)

    players = get(f"{API}/players/nfl")  # ~5 MB; fine for a scheduled run
    proj = fetch_projections(season, week)
    playing = teams_playing(proj)
    bye_teams = sorted(ALL_TEAMS - playing) if playing else []

    starters: list[str] = list(mine.get("starters") or [])
    # Pad/truncate to slot count defensively.
    starters = (starters + ["0"] * len(slots))[: len(slots)]
    all_players = set(mine.get("players") or [])
    bench = sorted(all_players - set(s for s in starters if s != "0"))
    reserve = set(mine.get("reserve") or []) | set(mine.get("taxi") or [])

    def status_of(pid: str) -> tuple[str, str]:
        """(level, reason) where level in RED/YELLOW/OK."""
        if pid == "0":
            return "RED", "empty slot"
        p = players.get(pid)
        if not p:
            return "RED", "unknown player id"
        team = p.get("team")
        if not team:
            return "RED", "free agent / no team"
        if bye_teams and team in bye_teams:
            return "RED", f"BYE (week {week})"
        inj = p.get("injury_status")
        if inj in RED_STATUS:
            part = p.get("injury_body_part")
            return "RED", f"{inj}" + (f" — {part}" if part else "")
        if p.get("status") in ("Inactive", "Injured Reserve", "PUP", "Suspended"):
            return "RED", f"roster status {p.get('status')}"
        if inj in YELLOW_STATUS:
            part = p.get("injury_body_part")
            return "YELLOW", f"{inj}" + (f" — {part}" if part else "")
        pr = proj.get(pid)
        if playing and (not pr or not pr.get("game_id")):
            return "YELLOW", "no game/projection found for this week"
        return "OK", ""

    def pts(pid: str) -> float:
        pr = proj.get(pid) or {}
        return float(pr.get("pts") or 0.0)

    lines: list[str] = []
    now = et_now()
    lines.append(f"# Lineup guard — {display} — {season} week {week}")
    lines.append("")
    lines.append(
        f"Run {now:%a %Y-%m-%d %I:%M %p ET} · season_type={season_type} · "
        f"byes this week: {', '.join(bye_teams) if bye_teams else 'none/unknown'}"
    )
    lines.append("")

    alerts: list[dict] = []
    rows: list[str] = []
    rows.append("| Slot | Starter | Proj | Opp | Status |")
    rows.append("|---|---|--:|---|---|")
    for slot, pid in zip(slots, starters):
        p = players.get(pid, {}) if pid != "0" else {}
        level, reason = status_of(pid)
        pr = proj.get(pid) or {}
        icon = {"RED": "🔴", "YELLOW": "🟡", "OK": "🟢"}[level]
        rows.append(
            f"| {slot} | {player_line(p, pid)} | {pts(pid):.1f} | {pr.get('opp') or '—'} | {icon} {reason} |"
        )
        if level != "OK":
            # Best bench alternatives eligible for this slot that are themselves OK.
            elig = SLOT_ELIGIBLE.get(slot, set())
            cands = []
            for b in bench:
                bp = players.get(b, {})
                if bp.get("position") not in elig:
                    continue
                bl, br = status_of(b)
                if bl == "RED":
                    continue
                cands.append((pts(b), b, bl, br))
            cands.sort(reverse=True)
            alerts.append(
                {
                    "slot": slot,
                    "player_id": pid,
                    "player": player_line(p, pid),
                    "level": level,
                    "reason": reason,
                    "alternatives": [
                        {"player": player_line(players.get(b, {}), b), "pts": round(pt, 1), "flag": br}
                        for pt, b, bl, br in cands[:3]
                    ],
                }
            )

    lines.append("## Starters")
    lines.append("")
    lines.extend(rows)
    lines.append("")

    if alerts:
        lines.append("## ⚠️ Action needed")
        lines.append("")
        for a in alerts:
            lines.append(f"- **{a['slot']}: {a['player']}** — {a['reason']}")
            if a["alternatives"]:
                for alt in a["alternatives"]:
                    flag = f" ({alt['flag']})" if alt["flag"] else ""
                    lines.append(f"    - swap in {alt['player']} — proj {alt['pts']}{flag}")
            else:
                lines.append("    - no eligible bench replacement — pick up a free agent")
        lines.append("")
    else:
        lines.append("## ✅ All starters available")
        lines.append("")

    # Projection swaps: bench player > starter by margin, same slot eligibility.
    swaps = []
    for slot, pid in zip(slots, starters):
        if slot in ("K", "DEF"):
            continue
        elig = SLOT_ELIGIBLE.get(slot, set())
        s_pts = pts(pid)
        for b in bench:
            bp = players.get(b, {})
            if bp.get("position") not in elig:
                continue
            bl, _ = status_of(b)
            if bl == "RED":
                continue
            if pts(b) - s_pts >= SWAP_MARGIN:
                swaps.append((pts(b) - s_pts, slot, pid, b))
    swaps.sort(reverse=True)
    lines.append("## Bench")
    lines.append("")
    lines.append("| Player | Proj | Status |")
    lines.append("|---|--:|---|")
    for b in sorted(bench, key=lambda x: -pts(x)):
        bl, br = status_of(b)
        icon = {"RED": "🔴", "YELLOW": "🟡", "OK": "🟢"}[bl]
        tag = " (IR slot)" if b in reserve else ""
        lines.append(f"| {player_line(players.get(b, {}), b)}{tag} | {pts(b):.1f} | {icon} {br} |")
    lines.append("")
    if swaps:
        lines.append("## Projection swaps (info only, ≥ %.1f pt edge)" % SWAP_MARGIN)
        lines.append("")
        seen = set()
        for edge, slot, s, b in swaps:
            key = (slot, s)
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- {slot}: {player_line(players.get(b, {}), b)} ({pts(b):.1f}) over "
                f"{player_line(players.get(s, {}), s)} ({pts(s):.1f}) — +{edge:.1f}"
            )
        lines.append("")

    report = "\n".join(lines)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.md").write_text(report + "\n")

    # Machine state + change detection so the workflow only alerts on change.
    sig = sorted(f"{a['slot']}:{a['player_id']}:{a['level']}" for a in alerts)
    prev = {}
    state_path = OUT_DIR / "state.json"
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text())
        except Exception:
            prev = {}
    changed = not (prev.get("season") == season and prev.get("week") == week and prev.get("signature") == sig)
    has_red = any(a["level"] == "RED" for a in alerts)
    out_state = {
        "season": season,
        "week": week,
        "season_type": season_type,
        "run_at_et": now.isoformat(timespec="minutes"),
        "user": display,
        "alert": bool(alerts),
        "red": has_red,
        "changed": changed,
        "signature": sig,
        "alerts": alerts,
        "bye_teams": bye_teams,
    }
    state_path.write_text(json.dumps(out_state, indent=2) + "\n")

    print(report)
    # For the workflow.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"alert={'true' if alerts else 'false'}\n")
            fh.write(f"red={'true' if has_red else 'false'}\n")
            fh.write(f"changed={'true' if changed else 'false'}\n")
            fh.write(f"week={week}\n")
            fh.write(f"season={season}\n")
            fh.write(f"title=Lineup guard — {season} week {week}: {len(alerts)} flagged\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
