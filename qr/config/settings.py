"""Process configuration.

Only three values come from the environment / ``.env``:

* ``MONGO_URL`` - the single MongoDB connection string (database name is taken
  from the URI path, otherwise :data:`DEFAULT_DB_NAME`).
* ``API_ID`` / ``API_HASH`` - Telegram application credentials.

Everything else (channels, templates, split percentages, ...) is runtime state
stored per account in MongoDB and changed with chat commands.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
SESSIONS_DIR = BASE_DIR / "sessions"
DEFAULT_SESSION = "quickreply"

REQUIRED_KEYS = ("MONGO_URL", "API_ID", "API_HASH")

# Used only when MONGO_URL has no "/<database>" path component.
DEFAULT_DB_NAME = "quickreply"

# Default destination for verified daily screenshot ZIPs (changeable with
# `.setarchive`). The posting channel for .post/.postbatch/.postnow has no
# default: it must be chosen explicitly with `.setpostchannel`.
DEFAULT_ARCHIVE_CHANNEL = "@ohthatsthepoint"

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001 - no tz database (e.g. Windows without tzdata)
    # Asia/Kolkata has had a fixed +05:30 offset with no DST since 1945.
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")


@dataclass(frozen=True)
class Settings:
    mongo_url: str
    api_id: int
    api_hash: str
    session_name: str = DEFAULT_SESSION

    @property
    def session_path(self) -> str:
        return str(SESSIONS_DIR / self.session_name)


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Parse KEY=VALUE lines without touching os.environ."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # First definition wins, like python-dotenv.
        values.setdefault(key, value)
    return values


def current_values(path: Path = ENV_PATH) -> dict[str, str]:
    """Environment variables override the .env file."""
    values = read_env_file(path)
    for key in REQUIRED_KEYS:
        env_value = os.environ.get(key)
        if env_value:
            values[key] = env_value
    return values


def load(session_name: str = DEFAULT_SESSION, path: Path = ENV_PATH) -> Settings:
    """Load and validate configuration, raising SystemExit with guidance."""
    from qr.config.validation import validate_api_hash, validate_api_id, validate_mongo_url

    values = current_values(path)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise SystemExit(
            "Missing configuration: " + ", ".join(missing)
            + "\nRun:  python setup.py"
        )
    problems = [
        problem
        for problem in (
            validate_mongo_url(values["MONGO_URL"]),
            validate_api_id(values["API_ID"]),
            validate_api_hash(values["API_HASH"]),
        )
        if problem
    ]
    if problems:
        raise SystemExit("Invalid configuration:\n  " + "\n  ".join(problems)
                         + "\nRun:  python setup.py")
    return Settings(
        mongo_url=values["MONGO_URL"],
        api_id=int(values["API_ID"]),
        api_hash=values["API_HASH"],
        session_name=session_name,
    )
