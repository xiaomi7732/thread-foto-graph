import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
THREADS_USERNAME = os.environ.get("THREADS_USERNAME", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "gemini").strip().lower()
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-vl:4b-instruct")
OLLAMA_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_CONNECT_TIMEOUT_SECONDS", "3"))
OLLAMA_READ_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_READ_TIMEOUT_SECONDS", "180"))
MAX_LOCAL_VISION_REQUESTS_PER_RUN = int(os.environ.get("MAX_LOCAL_VISION_REQUESTS_PER_RUN", "200"))
VISION_RUN_TIMEOUT_SECONDS = float(os.environ.get("VISION_RUN_TIMEOUT_SECONDS", "2700"))
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

# Re-fetch the conversation after describing new images so photos posted
# during a long run can be picked up before the next scheduled run.
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "3"))
if MAX_ITERATIONS < 1:
    raise ValueError("MAX_ITERATIONS must be at least 1")


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def validate_vision() -> None:
    if VISION_PROVIDER not in {"gemini", "ollama", "auto"}:
        raise ValueError("VISION_PROVIDER must be gemini, ollama, or auto")
    if VISION_PROVIDER == "gemini":
        require("GEMINI_API_KEY", GEMINI_API_KEY)
    if VISION_PROVIDER in {"ollama", "auto"}:
        url = urlsplit(OLLAMA_BASE_URL)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.query or url.fragment
        ):
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) URL without credentials or query parameters")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("OLLAMA_BASE_URL port is invalid")
        require("OLLAMA_MODEL", OLLAMA_MODEL)
    for name in ("MAX_VISION_REQUESTS_PER_RUN", "MAX_LOCAL_VISION_REQUESTS_PER_RUN"):
        if globals()[name] < 0:
            raise ValueError(f"{name} must be nonnegative")
    for name in (
        "OLLAMA_CONNECT_TIMEOUT_SECONDS", "OLLAMA_READ_TIMEOUT_SECONDS",
        "VISION_RUN_TIMEOUT_SECONDS",
    ):
        value = globals()[name]
        if not 0 < value < float("inf"):
            raise ValueError(f"{name} must be finite and positive")
