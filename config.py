"""Settings in one place: model names, database URLs, .env loading."""
import os


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader (KEY=VALUE lines). Existing environment variables win."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except FileNotFoundError:
        pass


load_dotenv()

# --- LLM (OpenRouter, free models only) --------------------------------------
OPENROUTER_KEY_ENV = "OPENROUTER_LLM_KEY"   # the variable in .env that holds the key
# Tried in order. Free models get rate-limited or retired, so check
# https://openrouter.ai/models?q=free before the class.
FREE_MODELS = [
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "inclusionai/ling-3.2-flash:free",
]

# --- Database (PostgreSQL 18, see schema.sql) --------------------------------
# One URL per role. The roles are created by schema.sql.
DATABASE_URLS = {
    "owner": os.environ.get("DATABASE_URL_OWNER", "postgresql://mas_owner:mas_owner_pw@localhost:5432/mas"),
    "app":   os.environ.get("DATABASE_URL_APP",   "postgresql://mas_app:mas_app_pw@localhost:5432/mas"),
    "agent": os.environ.get("DATABASE_URL_AGENT", "postgresql://mas_agent:mas_agent_pw@localhost:5432/mas"),
    "worker": os.environ.get("DATABASE_URL_WORKER", "postgresql://mas_worker:mas_worker_pw@localhost:5432/mas"),
}

# --- Celery / Redis ------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")

# --- Business rules (enforced by tools.py, mirrored in the prompt) ------------
TIMEZONE = "Asia/Kolkata"
QUIET_HOURS = (21, 9)          # no messages from 21:00 until 09:00; drafts made then are held
DAILY_CAP = 2                  # max messages per resident per day
MAX_PROMISE_DAYS = 60          # a promised date further out than this is refused
MAX_MESSAGE_CHARS = 400
HISTORY_MAX = 20               # most chat messages get_chat_history returns

# --- Scheduling ----------------------------------------------------------------
# The pipeline runs only from 09:00 to 20:59 India time (QUIET_HOURS is the complement).
SCAN_EVERY_MINUTES = 15        # beat fires at :00, :15, :30, :45 for hours 09 to 20
RECHECK_HOURS = 4              # after a run that leaves the case waiting, look again after this long
LADDER_GAP_DAYS = {1: 3, 2: 4}  # after step 1 is sent, step 2 is due 3 days later; after step 2, escalate 4 days later (day 7)
CASE_LOCK_SECONDS = 300        # one agent run per case at a time
MAX_CASES_PER_SCAN = 50
