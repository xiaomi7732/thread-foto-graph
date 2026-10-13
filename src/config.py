import os
from dotenv import load_dotenv

load_dotenv()

THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
THREADS_USERNAME = os.environ.get("THREADS_USERNAME", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "4"))

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
