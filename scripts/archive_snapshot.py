"""Immutable pregame archive of every decision-time input.

Captures, per run, the raw feeds the lineup/waiver/trade decisions are made
from, so that any later model can be scored against what was actually known
before kickoff — not against a regenerated "historical" view.

Sources (all free, no auth):
  sleeper_projections     api.sleeper.com/projections/nfl/{season}/{week}  (RotoWire pts + stats)
  sleeper_player_status   api.sleeper.app/v1/players/nfl  -> compact per-player status/injury/depth CSV
  sleeper_rosters         api.sleeper.app/v1/league/{id}/rosters         (waiver pool = complement)
  sleeper_league          api.sleeper.app/v1/league/{id}                 (scoring contract)
  fantasypros_ecr_*       fantasypros.com/nfl/rankings/{page}.php  `ecrData` blob (127-expert consensus,
                          rank_std/min/max, kickoff ts, game status) for flex / qb / k / dst
  nflverse_injuries       nflverse-data injuries_{season}.csv  (official report + practice status)
  nflverse_games          nfldata games.csv filtered to season (kickoffs, spread, total, moneylines)

Layout (never overwritten; a run is a directory):
  data/archive/{season}/week{WW}/{run_id}/manifest.json   always written
  data/archive/{season}/week{WW}/{run_id}/<source>.<ext>  only when content differs from the
                                                          most recent capture of that source in
                                                          the same week (manifest records
                                                          `unchanged_from` otherwise)
  data/archive/index.jsonl                                one line per run

Every source carries a row-count floor. A source below its floor is recorded as
FAILED in the manifest and its file is not written — missing data is not zero.
Exit code: 0 all sources ok, 2 if any source failed (the workflow still commits
what succeeded), 3 if nothing at all could be captured.

Usage
  python scripts/archive_snapshot.py               # current NFL week; quiet outside regular/post season
  python scripts/archive_snapshot.py --week 7      # override week
  python scripts/archive_snapshot.py --force       # run even in preseason/offseason
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

import requests

from _common import ROOT, load_env

SCHEMA_VERSION = 1
ARCHIVE_DIR = ROOT / "data" / "archive"
INDEX_PATH = ARCHIVE_DIR / "index.jsonl"

SLEEPER_API = "https://api.sleeper.app/v1"
SLEEPER_PROJ = "https://api.sleeper.com/projections/nfl"
FP_RANKINGS = "https://www.fantasypros.com/nfl/rankings/{page}.php"
NFLVERSE_INJ = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
NFLVERSE_GAMES = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

API_HEADERS = {"User-Agent": "ff-data-pipeline archive/1.0"}
# FantasyPros serves the ecrData blob to browsers; a bare python UA gets a stub page.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

FP_PAGES = {"flex": "half-point-ppr-flex", "qb": "qb", "k": "k", "dst": "dst"}
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Row-count floors: below these the feed is broken, not merely quiet.
FLOORS = {
    "sleeper_projections": 800,
    "sleeper_projections_with_pts": 300,
    "sleeper_player_status": 600,   # ~32 teams x ~17 QB/RB/WR/TE/K + 32 DEF; 889 seen 2026 wk2
    "sleeper_rosters": 8,
    "sleeper_league": 1,
    "fantasypros_ecr_flex": 300,
    "fantasypros_ecr_qb": 40,
    "fantasypros_ecr_k": 25,
    "fantasypros_ecr_dst": 28,
    "nflverse_injuries": 30,
    "nflverse_games": 250,
}

STATUS_FIELDS = [
    "player_id", "full_name", "position", "team", "status", "active", "injury_status",
    "injury_body_part", "injury_notes", "injury_start_date", "practice_participation",
    "practice_description", "depth_chart_position", "depth_chart_order", "age",
    "years_exp", "news_updated",
]


# ---------------------------------------------------------------- helpers

def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Capture:
    """One fetched source: bytes to write plus provenance for the manifest."""

    def __init__(self, name: str, ext: str, url: str, params=None):
        self.name = name
        self.ext = ext
        self.url = url
        self.params = params or {}
        self.requested_at = utc_now().isoformat()
        self.http_status: int | None = None
        self.source_updated: str | None = None
        self.rows: int | None = None
        self.extra: dict = {}
        self.data: bytes | None = None
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error is None

    def fail(self, msg: str) -> "Capture":
        self.error = msg
        self.data = None
        return self

    def manifest_entry(self, written: str | None, unchanged_from: str | None) -> dict:
        return {
            "source": self.name,
            "url": self.url,
            "params": self.params,
            "requested_at": self.requested_at,
            "http_status": self.http_status,
            "source_updated": self.source_updated,
            "status": "ok" if self.ok else "failed",
            "error": self.error,
            "rows": self.rows,
            "bytes": len(self.data) if self.data else None,
            "sha256": sha256(self.data) if self.data else None,
            "file": written,
            "unchanged_from": unchanged_from,
            **self.extra,
        }


def http_get(url: str, headers: dict, params=None, timeout: int = 60) -> requests.Response:
    r = requests.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r


def gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    # mtime=0 keeps identical content byte-identical across runs (dedup by hash works)
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------- sources

def cap_sleeper_projections(season: str, week: int) -> Capture:
    params = [("season_type", "regular"), ("order_by", "pts_half_ppr")] + [
        ("position[]", p) for p in POSITIONS
    ]
    c = Capture("sleeper_projections", "json.gz", f"{SLEEPER_PROJ}/{season}/{week}", dict(params))
    try:
        r = http_get(c.url, API_HEADERS, params=params)
        c.http_status = r.status_code
        rows = r.json()
        with_pts = sum(1 for x in rows if (x.get("stats") or {}).get("pts_half_ppr") is not None)
        companies = sorted({x.get("company") for x in rows if x.get("company")})
        c.rows = len(rows)
        c.extra = {"rows_with_pts_half_ppr": with_pts, "company": companies}
        if len(rows) < FLOORS["sleeper_projections"]:
            return c.fail(f"only {len(rows)} rows (floor {FLOORS['sleeper_projections']})")
        if with_pts < FLOORS["sleeper_projections_with_pts"]:
            return c.fail(f"only {with_pts} rows carry pts_half_ppr (floor {FLOORS['sleeper_projections_with_pts']})")
        c.data = gz(canonical_json(rows))
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


def cap_sleeper_player_status() -> Capture:
    c = Capture("sleeper_player_status", "csv.gz", f"{SLEEPER_API}/players/nfl")
    try:
        r = http_get(c.url, API_HEADERS, timeout=120)
        c.http_status = r.status_code
        players = r.json()
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=STATUS_FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        n = 0
        for pid in sorted(players):
            p = players[pid]
            if p.get("position") not in POSITIONS:
                continue
            if not p.get("team") and p.get("position") != "DEF":
                continue  # free agents with no NFL team are not decision-relevant
            row = {k: p.get(k) for k in STATUS_FIELDS}
            row["player_id"] = pid
            if p.get("position") == "DEF":
                row["full_name"] = f"{pid} DEF"
            w.writerow(row)
            n += 1
        c.rows = n
        c.extra = {"players_in_db": len(players)}
        if n < FLOORS["sleeper_player_status"]:
            return c.fail(f"only {n} rostered players (floor {FLOORS['sleeper_player_status']})")
        c.data = gz(buf.getvalue().encode())
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


def cap_sleeper_json(name: str, path: str, floor_key: str) -> Capture:
    c = Capture(name, "json", f"{SLEEPER_API}{path}")
    try:
        r = http_get(c.url, API_HEADERS)
        c.http_status = r.status_code
        obj = r.json()
        c.rows = len(obj) if isinstance(obj, list) else 1
        if c.rows < FLOORS[floor_key]:
            return c.fail(f"{c.rows} rows (floor {FLOORS[floor_key]})")
        c.data = json.dumps(obj, sort_keys=True, indent=1).encode()
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


ECR_RE = re.compile(r"var ecrData\s*=\s*(\{.*?\});\s*\n", re.S)


def cap_fantasypros(key: str, page: str, expect_week: int) -> Capture:
    name = f"fantasypros_ecr_{key}"
    c = Capture(name, "json.gz", FP_RANKINGS.format(page=page))
    try:
        r = http_get(c.url, BROWSER_HEADERS)
        c.http_status = r.status_code
        m = ECR_RE.search(r.text)
        if not m:
            return c.fail(f"ecrData blob not found in page ({len(r.text)} bytes)")
        blob = json.loads(m.group(1))
        players = blob.get("players") or []
        c.rows = len(players)
        c.source_updated = str(blob.get("last_updated") or "")
        c.extra = {
            "experts": blob.get("total_experts"),
            "page_week": blob.get("week"),
            "page_year": blob.get("year"),
        }
        if c.rows < FLOORS[name]:
            return c.fail(f"{c.rows} players (floor {FLOORS[name]})")
        if blob.get("week") not in (None, "", expect_week, str(expect_week)):
            return c.fail(f"page is week {blob.get('week')}, expected {expect_week}")
        c.data = gz(canonical_json(blob))
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


def cap_nflverse_injuries(season: str) -> Capture:
    c = Capture("nflverse_injuries", "csv.gz", NFLVERSE_INJ.format(season=season))
    try:
        r = http_get(c.url, API_HEADERS)
        c.http_status = r.status_code
        c.source_updated = r.headers.get("Last-Modified")
        text = r.content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        c.rows = len(rows)
        c.extra = {"weeks_present": sorted({x.get("week") for x in rows}, key=lambda w: int(w or 0))}
        if c.rows < FLOORS["nflverse_injuries"]:
            return c.fail(f"{c.rows} rows (floor {FLOORS['nflverse_injuries']})")
        c.data = gz(text.encode())
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


def cap_nflverse_games(season: str) -> Capture:
    c = Capture("nflverse_games", "csv.gz", NFLVERSE_GAMES, {"season": season})
    try:
        r = http_get(c.url, API_HEADERS)
        c.http_status = r.status_code
        c.source_updated = r.headers.get("Last-Modified")
        reader = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
        rows = [x for x in reader if x.get("season") == str(season)]
        c.rows = len(rows)
        if c.rows < FLOORS["nflverse_games"]:
            return c.fail(f"{c.rows} rows for {season} (floor {FLOORS['nflverse_games']})")
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=reader.fieldnames, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        c.data = gz(buf.getvalue().encode())
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))
    return c


# ---------------------------------------------------------------- archive I/O

def previous_hashes(week_dir: Path) -> dict[str, tuple[str, str]]:
    """source -> (sha256, run_id) of the most recent *written* capture this week."""
    out: dict[str, tuple[str, str]] = {}
    if not week_dir.exists():
        return out
    for run_dir in sorted(p for p in week_dir.iterdir() if p.is_dir()):
        mpath = run_dir / "manifest.json"
        if not mpath.exists():
            continue
        try:
            m = json.loads(mpath.read_text())
        except json.JSONDecodeError:
            continue
        for s in m.get("sources", []):
            if s.get("status") == "ok" and s.get("sha256"):
                out[s["source"]] = (s["sha256"], run_dir.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="run outside regular/post season")
    args = ap.parse_args()

    load_env()
    league_id = os.environ.get("SLEEPER_LEAGUE_ID", "")
    if not league_id:
        print("SLEEPER_LEAGUE_ID not set", file=sys.stderr)
        return 3

    state = http_get(f"{SLEEPER_API}/state/nfl", API_HEADERS).json()
    season = str(state.get("season"))
    season_type = state.get("season_type")
    week = args.week or int(state.get("display_week") or state.get("week") or 0)
    if season_type not in ("regular", "post") and not args.force:
        print(f"season_type={season_type}: not in season, nothing to archive (use --force)")
        return 0
    if not week:
        print("could not determine week", file=sys.stderr)
        return 3

    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    week_dir = ARCHIVE_DIR / season / f"week{week:02d}"
    run_dir = week_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    prev = previous_hashes(week_dir)

    print(f"archive {season} week {week} run {run_id} ({season_type})")
    captures = [
        cap_sleeper_projections(season, week),
        cap_sleeper_player_status(),
        cap_sleeper_json("sleeper_rosters", f"/league/{league_id}/rosters", "sleeper_rosters"),
        cap_sleeper_json("sleeper_league", f"/league/{league_id}", "sleeper_league"),
        *(cap_fantasypros(k, p, week) for k, p in FP_PAGES.items()),
        cap_nflverse_injuries(season),
        cap_nflverse_games(season),
    ]

    entries = []
    n_ok = n_written = n_failed = 0
    for c in captures:
        written = unchanged_from = None
        if c.ok:
            n_ok += 1
            h = sha256(c.data)
            if c.name in prev and prev[c.name][0] == h:
                unchanged_from = prev[c.name][1]
            else:
                fname = f"{c.name}.{c.ext}"
                (run_dir / fname).write_bytes(c.data)
                written = fname
                n_written += 1
            print(f"  ok      {c.name:26} rows={c.rows:<5} {'wrote ' + written if written else 'unchanged since ' + unchanged_from}")
        else:
            n_failed += 1
            print(f"  FAILED  {c.name:26} {c.error}")
        entries.append(c.manifest_entry(written, unchanged_from))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "season": season,
        "week": week,
        "season_type": season_type,
        "sleeper_state": state,
        "started_at": started.isoformat(),
        "finished_at": utc_now().isoformat(),
        "sources_ok": n_ok,
        "sources_written": n_written,
        "sources_failed": n_failed,
        "sources": entries,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with INDEX_PATH.open("a") as f:
        f.write(json.dumps({
            "run_id": run_id, "season": season, "week": week, "season_type": season_type,
            "started_at": manifest["started_at"], "ok": n_ok, "written": n_written, "failed": n_failed,
            "failed_sources": [e["source"] for e in entries if e["status"] == "failed"],
        }) + "\n")

    print(f"done: {n_ok} ok ({n_written} new files), {n_failed} failed -> {run_dir.relative_to(ROOT)}")
    if n_ok == 0:
        return 3
    return 2 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
