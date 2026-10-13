"""Vision descriptions via Google Gemini Flash (free tier).

Each image is downloaded, sent to Gemini for a short text description, and the
result is cached by media_id so a given photo is only ever described once
(see cache.py). This keeps daily usage well under the free-tier request cap
even on days with 1,000+ reply photos.
"""
from __future__ import annotations

import base64
import re
import time

import requests

from . import config, threads_client

GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

PROMPT = (
    "Describe this photo in one or two concise sentences, focusing on the "
    "subject matter and any context clues relevant to a social media post. "
    "Do not mention that you are an AI or that this is a described image."
)

# Free tier is ~10-15 requests/minute; stay comfortably under that.
MIN_SECONDS_BETWEEN_CALLS = 5

# Matches "?key=..." / "&key=..." style query params so a stray secret can
# never make it into a committed file (data/topics.md) via an error message.
_QUERY_SECRET_RE = re.compile(r"([?&](?:key|access_token)=)[^&\s)]+", re.IGNORECASE)


def _sanitize(message: str) -> str:
    return _QUERY_SECRET_RE.sub(r"\1REDACTED", message)


class QuotaExceededError(RuntimeError):
    """Raised when Gemini returns a 429; caller should stop and resume later."""


def describe_image(image_url: str) -> str:
    img_resp = requests.get(image_url, timeout=30)
    img_resp.raise_for_status()
    mime_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
    b64_data = base64.b64encode(img_resp.content).decode("ascii")

    body = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": b64_data}},
                ]
            }
        ]
    }
    url = GEMINI_URL_TMPL.format(model=config.GEMINI_MODEL)
    # Key goes in a header, never the URL/query string -- so it can never leak
    # into an exception message (which includes response.url) or into any
    # generated file that embeds error text.
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=body, timeout=60)

    if resp.status_code == 429:
        raise QuotaExceededError("Gemini free-tier daily/minute quota exceeded")
    resp.raise_for_status()

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return "(no description available)"


def describe_images(media_items: list[dict], cache: dict, max_requests: int, on_progress=None) -> int:
    """Describes every uncached photo in each item in place, storing a
    'photo_descriptions' list on the item (one entry per photo -- a
    CAROUSEL_ALBUM can hold several, not just its cover image; see
    threads_client.photo_items). A list entry is None if that specific photo
    wasn't described this run (budget/quota), which topic_graph.py renders as
    a "pending" placeholder rather than silently dropping it.

    Returns the number of new Gemini requests made. `on_progress`, if given,
    is called after every single new description so the caller can persist
    the cache immediately -- a slow or interrupted run never loses
    already-described photos.
    """
    requests_made = 0
    quota_hit = False
    for item in media_items:
        photos = threads_client.photo_items(item)
        if not photos:
            continue

        descriptions = []
        for photo in photos:
            media_id = photo["id"]
            if media_id in cache:
                descriptions.append(cache[media_id])
                continue
            if quota_hit or requests_made >= max_requests:
                # Leave as None (pending) -- topic_graph.py distinguishes
                # this from "no photo" via the list entry existing at all.
                descriptions.append(None)
                continue

            try:
                description = describe_image(photo["media_url"])
            except QuotaExceededError as exc:
                # Don't abandon the whole batch silently: keep looping so
                # later cache-hit items/photos still get their description,
                # just stop making new network calls. This used to be a
                # silent no-op from the caller's point of view (no print),
                # which made "why is this photo still pending?" undiagnosable
                # from run logs alone -- print once per describe_images() call
                # so it's visible exactly when/why it happened.
                if not quota_hit:
                    print(f"  {exc} -- remaining photos in this batch left pending.", flush=True)
                quota_hit = True
                descriptions.append(None)
                continue
            except requests.RequestException as exc:
                # Do NOT cache failures -- a transient/auth error should be
                # retried on the next run, not permanently "stuck" as a fake
                # cached description.
                descriptions.append(f"(description unavailable: {_sanitize(str(exc))})")
                requests_made += 1
                time.sleep(MIN_SECONDS_BETWEEN_CALLS)
                continue

            cache[media_id] = description
            descriptions.append(description)
            if on_progress is not None:
                on_progress()
            requests_made += 1
            time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        item["photo_descriptions"] = descriptions

    return requests_made
