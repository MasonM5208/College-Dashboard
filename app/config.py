"""Configuration, read from the environment.

Two rules here, both from SPEC.md:

* Fail loudly (SPEC §4). A missing setting raises at startup with the variable's
  name in the message, rather than defaulting to something that half-works.
* Never log a secret (SPEC §11). ``require`` reports that a variable is missing or
  empty; it never reports what a variable contains.
"""

from __future__ import annotations

import os
from pathlib import Path

# Inside the container this resolves to /app, which is where the Dockerfile puts
# both the `app` package and the `migrations` directory. On a laptop it resolves
# to the repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent

MIGRATIONS_DIR = REPO_ROOT / "migrations"

# The database file. docker-compose.yml sets this to /data/dashboard.db, which is
# the host directory /srv/dashboard/data bind-mounted into the container.
DB_PATH = Path(os.environ.get("DB_PATH", "/data/dashboard.db"))

# Display timezone only. Everything stored in SQLite is UTC.
TZ = os.environ.get("TZ", "UTC")

# Which Claude model answers questions. Settable so that dropping from Opus to
# Sonnet — roughly half the price per token — is a line in the secrets file and a
# restart, not a code change. SPEC §4 forbids a provider abstraction; this is not
# one, it is the same API with a different model string.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-opus-5").strip() or "claude-opus-5"

# How hard the model works before answering. The API default is "high"; "medium"
# is the lever to pull if replies cost more than they are worth.
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "high").strip() or "high"


class MissingSetting(RuntimeError):
    """A required environment variable is absent or empty."""


def canvas_configured() -> bool:
    """Whether the Canvas feed address has been set.

    Used to decide whether to start polling at all, so that a dashboard without a
    feed address logs one clear line rather than failing every 30 minutes.
    """
    return bool(os.environ.get("CANVAS_ICS_URL", "").strip())


def require(name: str) -> str:
    """Return the value of a required environment variable.

    Raises MissingSetting, naming the variable and where to set it, if it is
    absent or empty. The value itself is never included in the message.
    """
    value = os.environ.get(name, "")
    if not value.strip():
        raise MissingSetting(
            f"Required setting {name} is not set. Real values belong in the "
            f"secrets file at /etc/college-dashboard/env — see docs/SECRETS.md. "
            f"Add a line reading {name}=... there, then restart with "
            f"'sudo docker compose up -d'."
        )
    return value
