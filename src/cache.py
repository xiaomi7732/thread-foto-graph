"""Persistent media-ID descriptions, with provenance for new entries."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "media_cache.json"


def description_text(entry: object) -> str | None:
    value = entry.get("description") if isinstance(entry, dict) else entry
    return value if isinstance(value, str) else None


def make_entry(description: str, provider: str, model: str, fallback_reason: str | None = None) -> dict:
    return {
        "description": description,
        "provider": provider,
        "model": model,
        "described_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fallback_reason": fallback_reason,
    }


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CACHE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CACHE_PATH)
