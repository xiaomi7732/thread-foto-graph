"""Persists the Threads long-lived access token + its expiry locally.

Separate from cache.py (which holds vision descriptions) because this file
is more sensitive -- it's an access token, not just derived data.
"""
import json
import os
from pathlib import Path

TOKEN_STORE_PATH = Path(__file__).resolve().parent.parent / "cache" / "token_store.json"


def load_token() -> dict | None:
    if not TOKEN_STORE_PATH.exists():
        return None
    with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return None


def save_token(access_token: str, expires_at_epoch: float) -> None:
    TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_STORE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"access_token": access_token, "expires_at": expires_at_epoch}, f)
    os.replace(tmp_path, TOKEN_STORE_PATH)
