"""Shared paths and config loading for all pipeline scripts."""

import datetime
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADP_DIR = ROOT / "data" / "adp"
LEAGUE_DIR = ROOT / "data" / "league"
CHARTS_DIR = ROOT / "charts"
REPORTS_DIR = ROOT / "reports"


def load_env() -> None:
    """Load KEY=VALUE pairs from .env in the repo root into os.environ.

    Existing environment variables win, so CI/cron can override the file.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key, value)


def league_teams() -> int:
    return int(os.environ.get("LEAGUE_TEAMS", "8"))


def today_str() -> str:
    return datetime.date.today().isoformat()


def current_season(today: datetime.date | None = None) -> int:
    """NFL season year. Jan/Feb belong to the previous season."""
    today = today or datetime.date.today()
    return today.year - 1 if today.month < 3 else today.year


def ensure_dirs() -> None:
    for d in (ADP_DIR, LEAGUE_DIR, CHARTS_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
