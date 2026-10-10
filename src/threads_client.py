"""Thin client for the Meta Threads API (graph.threads.net).

Fetches the authenticated user's recent root posts and, for each, the full
reply tree via the `conversation` edge. Collects every photo (root + replies)
so the vision step can describe them instead of Muse pulling raw images.

Reference: https://developers.facebook.com/docs/threads
API surfaces occasionally change; verify field names against current docs if
requests start failing with "Unknown field" errors.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import requests

from . import config, threads_auth

BASE_URL = "https://graph.threads.net/v1.0"

ROOT_FIELDS = "id,text,timestamp,media_type,media_url,permalink,shortcode"
REPLY_FIELDS = "id,text,timestamp,media_type,media_url,username,root_post{id}"


def _get(path: str, params: dict) -> dict:
    # Auto-refreshes the long-lived token when it's close to expiring.
    # Sent as a header (not a query param) so it can never leak into an
    # exception message that embeds the request URL.
    headers = {"Authorization": f"Bearer {threads_auth.get_valid_access_token()}"}
    resp = requests.get(f"{BASE_URL}/{path}", params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _paginate(path: str, params: dict) -> Iterable[dict]:
    next_params = dict(params)
    next_path = path
    while True:
        data = _get(next_path, next_params)
        yield from data.get("data", [])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        # Subsequent pages are full URLs; strip the base so _get can reuse it.
        next_path = next_url.replace(f"{BASE_URL}/", "")
        next_params = {}


def get_recent_root_threads(
    lookback_days: int = config.LOOKBACK_DAYS, max_threads: int | None = None
) -> list[dict]:
    """Returns the user's own root posts from the last `lookback_days` days.

    If `max_threads` is set, stops after collecting that many (posts are
    returned newest-first, so this caps to the N most recent posts) --
    useful for quick/manual test runs without scanning everything.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    posts = []
    for post in _paginate("me/threads", {"fields": ROOT_FIELDS, "limit": 25}):
        ts = dt.datetime.fromisoformat(post["timestamp"].replace("Z", "+00:00"))
        if ts < cutoff:
            break  # me/threads is returned newest-first
        posts.append(post)
        if max_threads is not None and len(posts) >= max_threads:
            break
    return posts


def get_conversation(thread_id: str) -> list[dict]:
    """Returns every reply (flattened) under a root thread, including photos."""
    return list(_paginate(f"{thread_id}/conversation", {"fields": REPLY_FIELDS, "limit": 50}))


def collect_threads_with_replies(
    lookback_days: int = config.LOOKBACK_DAYS, max_threads: int | None = None
) -> list[dict]:
    """Returns root posts, each annotated with its full list of replies."""
    roots = get_recent_root_threads(lookback_days, max_threads)
    for root in roots:
        root["replies"] = get_conversation(root["id"])
    return roots
