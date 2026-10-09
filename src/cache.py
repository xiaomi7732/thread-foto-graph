"""mediaCache keeps a persistent media_id -> description map so each photo is
only ever sent to the vision model once, even across daily runs. This is the
primary defense against the free-tier Gemini Flash daily request cap, since
reply photos on older threads rarely need to be re-described.
"""
import json
import os
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "media_cache.json"


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CACHE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CACHE_PATH)
