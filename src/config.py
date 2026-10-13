import os
from dotenv import load_dotenv

load_dotenv()

THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
THREADS_USERNAME = os.environ.get("THREADS_USERNAME", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "4"))

# Optional absolute floor on what's ever scanned, as an ISO 8601 date/
# datetime (e.g. "2026-01-01" or "2026-01-01T00:00:00Z"). Root posts older
# than this are skipped outright, regardless of LOOKBACK_DAYS -- useful to
# permanently exclude old posts (e.g. a one-off giveaway thread) without
# having to keep LOOKBACK_DAYS small forever. Unset (empty) means no floor.
HARD_CUTOFF_DATE = os.environ.get("HARD_CUTOFF_DATE", "")

# Optional cap on how many of the most recent root posts to process; unset
# (None) means "all posts within LOOKBACK_DAYS". Handy for quick test runs.
_max_root_threads_raw = os.environ.get("MAX_ROOT_THREADS", "")
MAX_ROOT_THREADS = int(_max_root_threads_raw) if _max_root_threads_raw else None

# Gemini free tier is request-count limited (not $ cost), so we cap requests
# per run and let unprocessed images carry over to the next scheduled run.
MAX_VISION_REQUESTS_PER_RUN = int(os.environ.get("MAX_VISION_REQUESTS_PER_RUN", "1400"))


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value
