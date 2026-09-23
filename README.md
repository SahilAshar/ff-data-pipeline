# ff-data-pipeline

Automated fantasy football ADP tracking and draft prep pipeline. Takes weekly ADP
snapshots from public sources, computes week-over-week movement (risers/fallers),
generates shareable movement charts, and syncs league data from the Sleeper API —
tuned for a 10-team half-PPR league ("Injury Prone" on Sleeper) with two FLEX
spots, where positional scarcity works differently than 12-team consensus ADP
assumes.

## Latest ADP Movement

<!-- ADP:START -->
Latest report: [reports/2026-09-23.md](reports/2026-09-23.md)

![latest ADP chart](charts/positional-adp-2026-09-23.png)
<!-- ADP:END -->

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
cp config.env.example .env   # then fill in SLEEPER_LEAGUE_ID (and optionally FANTASYPROS_API_KEY)
```

## Usage

```bash
python scripts/fetch_adp.py       # save today's ADP snapshot to data/adp/
python scripts/compute_deltas.py  # week-over-week movement (needs 2+ snapshots)
python scripts/generate_charts.py # ADP movement + positional distribution charts
python scripts/sleeper_sync.py    # pull league settings, rosters, player DB from Sleeper
python scripts/weekly_report.py   # full pipeline: fetch → deltas → charts → markdown report
```

Run `weekly_report.py` once a week (cron on Wednesdays recommended — ADP settles
midweek after camp news). Deltas and risers/fallers charts appear automatically
once two snapshots exist.

## Lineup guard (in-season)

`scripts/lineup_guard.py` checks the 0xAshar starting lineup before every kickoff
window — empty slots, byes, Out/Doubtful/IR/PUP starters, Questionable tags — and
suggests bench swaps from Sleeper's weekly half-PPR projections. Report lands in
`reports/lineup-guard/latest.md`. The `Lineup guard` workflow runs it Thu/Sun/Mon
(and Sat late season) and opens or updates a GitHub Issue **assigned to the repo
owner** only when the set of flagged starters changes; it closes the issue once
the lineup is clean. Quiet outside the regular season.

```bash
python scripts/lineup_guard.py --force --week 7   # dry run for any week
```

## Projection archive (in-season)

`scripts/archive_snapshot.py` takes an **immutable pregame capture of every
decision-time input**: Sleeper/RotoWire projections, per-player status/injury/depth,
league rosters and scoring, the FantasyPros 127-expert consensus (flex/qb/k/dst with
`rank_std`/`min`/`max`), nflverse official injury reports, and nflverse game lines
(kickoff, spread, total). One directory per run under `data/archive/{season}/week{WW}/`,
never overwritten; a source whose content is unchanged since the last capture that
week is recorded in `manifest.json` as `unchanged_from` instead of being re-written.
Every source has a row-count floor and is marked FAILED (file not written) below it —
missing data is never silently zero. `data/archive/index.jsonl` has one line per run.

The `Projection archive` workflow runs Thu/Sat/Sun(x3)/Mon/Tue ahead of each kickoff
window with a ~3h margin for Actions cron lag. It exists so a future model can be
scored against what was actually knowable before kickoff.

```bash
python scripts/archive_snapshot.py --force --week 7   # dry run for any week
```

## Data layout

```
data/adp/       # weekly ADP snapshots (YYYY-MM-DD.csv, git-tracked for history)
data/league/    # Sleeper API pulls (rosters, settings, draft picks; player DB cached, untracked)
data/archive/   # immutable pregame captures, one dir per run (see Projection archive)
charts/         # generated PNGs (Twitter-sized 1200x675)
reports/        # weekly markdown reports
```

## Data sources

| Source | Access | Used for |
|---|---|---|
| [Fantasy Football Calculator](https://fantasyfootballcalculator.com/adp) | Free JSON API, no key | Primary ADP (real + mock drafts, league-size + scoring-format aware) |
| [FantasyPros](https://www.fantasypros.com/apis/) | Free API key (50 req/day) | Consensus ADP triangulation (optional) |
| [Sleeper API](https://docs.sleeper.com/) | Free, no auth | League settings, rosters, draft picks, player DB |

Delta convention: `delta = this_week_rank - last_week_rank` — negative is rising,
positive is falling.
