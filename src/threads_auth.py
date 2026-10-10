"""Keeps the Threads long-lived access token alive indefinitely.

Bootstrap: on first run, seeds cache/token_store.json from the
THREADS_ACCESS_TOKEN env var (the long-lived token you obtained once via
Meta's OAuth flow). Every run after that, if the stored token is within
REFRESH_MARGIN_DAYS of expiring, it's refreshed via Meta's refresh endpoint
-- which only needs the *current* token, not the app secret or a redirect.

As long as this script runs at least once before the refresh margin runs
out (default: refresh whenever <10 days remain, on a ~60-day-lived token),
the token renews itself forever with no manual re-authorization.

Reference: https://developers.facebook.com/docs/threads/get-started/long-lived-tokens
"""
from __future__ import annotations

import time

import requests

from . import config, token_store

REFRESH_URL = "https://graph.threads.net/refresh_access_token"
REFRESH_MARGIN_DAYS = 10


def _refresh(current_token: str) -> dict:
    resp = requests.get(
        REFRESH_URL,
        params={"grant_type": "th_refresh_token", "access_token": current_token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()  # {"access_token": ..., "token_type": "bearer", "expires_in": seconds}


def get_valid_access_token() -> str:
    stored = token_store.load_token()

    if stored is None:
        # First run: bootstrap from the long-lived token in .env.
        seed_token = config.require("THREADS_ACCESS_TOKEN", config.THREADS_ACCESS_TOKEN)
        # Treat a freshly-provided token as if issued now with the standard
        # ~60 day lifetime; it will be refreshed well before that on the
        # next run if the margin is reached.
        expires_at = time.time() + (60 * 24 * 60 * 60)
        token_store.save_token(seed_token, expires_at)
        return seed_token

    access_token = stored["access_token"]
    seconds_remaining = stored["expires_at"] - time.time()

    if seconds_remaining < REFRESH_MARGIN_DAYS * 24 * 60 * 60:
        data = _refresh(access_token)
        access_token = data["access_token"]
        expires_at = time.time() + data["expires_in"]
        token_store.save_token(access_token, expires_at)

    return access_token
