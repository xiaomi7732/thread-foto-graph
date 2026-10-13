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

ROOT_FIELDS = (
    "id,text,timestamp,media_type,media_url,permalink,shortcode,"
    "children{id,media_type,media_url}"
)
REPLY_FIELDS = (
    "id,text,timestamp,media_type,media_url,username,permalink,root_post{id},"
    "replied_to{id},children{id,media_type,media_url}"
)


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


def parse_timestamp(raw: str) -> dt.datetime:
    """Parses a Threads API timestamp (ISO 8601, e.g. with a trailing 'Z')
    into a timezone-aware datetime, so callers can compare/sort by actual
    chronological order rather than raw string order (which breaks across
    differing UTC offsets)."""
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _resolve_hard_cutoff() -> dt.datetime | None:
    raw = config.HARD_CUTOFF_DATE
    if not raw:
        return None
    return parse_timestamp(raw)


def get_recent_root_threads(
    lookback_days: int = config.LOOKBACK_DAYS, max_threads: int | None = None
) -> list[dict]:
    """Returns the user's own root posts from the last `lookback_days` days.

    If `max_threads` is set, stops after collecting that many (posts are
    returned newest-first, so this caps to the N most recent posts) --
    useful for quick/manual test runs without scanning everything.

    If `HARD_CUTOFF_DATE` is set, it acts as an absolute floor on top of
    `lookback_days`: posts older than it are never scanned, even if
    `lookback_days` would otherwise include them.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    hard_cutoff = _resolve_hard_cutoff()
    if hard_cutoff is not None and hard_cutoff > cutoff:
        cutoff = hard_cutoff
    posts = []
    for post in _paginate("me/threads", {"fields": ROOT_FIELDS, "limit": 25}):
        ts = parse_timestamp(post["timestamp"])
        if ts < cutoff:
            break  # me/threads is returned newest-first
        posts.append(post)
        if max_threads is not None and len(posts) >= max_threads:
            break
    return posts


def get_conversation(thread_id: str) -> list[dict]:
    """Returns every reply (flattened, in API order) under a root thread,
    including photos. Each item carries `replied_to` (immediate parent media
    ID) so callers can reconstruct the actual reply tree -- see
    `_nest_replies`.
    """
    return list(_paginate(f"{thread_id}/conversation", {"fields": REPLY_FIELDS, "limit": 50}))


def _nest_replies(root_id: str, flat_replies: list[dict]) -> list[dict]:
    """Rebuilds the real reply-to-reply tree from the Threads API's flat
    `conversation` list, using each reply's `replied_to` (immediate parent).

    Without this, every reply looks like a flat, independent response to the
    root post -- losing which replies are actually nested under another
    reply (e.g. the thread owner replying to a specific commenter).
    """
    by_id = {reply["id"]: reply for reply in flat_replies}
    for reply in flat_replies:
        reply.setdefault("replies", [])

    top_level = []
    for reply in flat_replies:
        parent_id = (reply.get("replied_to") or {}).get("id")
        if parent_id and parent_id != root_id and parent_id in by_id:
            by_id[parent_id]["replies"].append(reply)
        else:
            # Parent is the root post itself (or unknown/missing) -> top-level.
            top_level.append(reply)
    return top_level


def iter_replies(replies: list[dict]):
    """Depth-first walk over a nested reply tree (see `_nest_replies`),
    yielding every reply dict regardless of nesting depth. Used by callers
    (vision description, counting) that need every reply, not just top-level
    ones.
    """
    for reply in replies:
        yield reply
        yield from iter_replies(reply.get("replies", []))


def photo_items(item: dict) -> list[dict]:
    """Returns every actual photo inside this thread/reply as its own
    {id, media_url} dict.

    A plain IMAGE is a single photo. A CAROUSEL_ALBUM's own media_url is only
    its *cover* image -- the rest of its photos live in `children` (fetched
    via ROOT_FIELDS/REPLY_FIELDS) and must be described individually, or
    they're silently missed entirely.
    """
    media_type = item.get("media_type")
    if media_type == "IMAGE":
        media_url = item.get("media_url")
        if not media_url:
            # Mirror the CAROUSEL_ALBUM branch below -- a photo with no URL
            # at all can never be fetched, so don't surface it as a photo to
            # describe (which would otherwise raise an unactionable,
            # permanently-recurring ImageFetchError on every run).
            return []
        return [{"id": item["id"], "media_url": media_url}]
    if media_type == "CAROUSEL_ALBUM":
        children = (item.get("children") or {}).get("data", [])
        return [
            {"id": child["id"], "media_url": child["media_url"]}
            for child in children
            if child.get("media_type") == "IMAGE" and child.get("media_url")
        ]
    return []


def collect_threads_with_replies(
    lookback_days: int = config.LOOKBACK_DAYS, max_threads: int | None = None
) -> list[dict]:
    """Returns root posts, each annotated with its nested reply tree
    (`replies`, where each reply may itself have nested `replies`)."""
    roots = get_recent_root_threads(lookback_days, max_threads)
    for root in roots:
        flat_replies = get_conversation(root["id"])
        root["replies"] = _nest_replies(root["id"], flat_replies)
    return roots
