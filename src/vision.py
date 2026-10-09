"""Vision descriptions via Google Gemini Flash (free tier).

Each image is downloaded, sent to Gemini for a short text description, and the
result is cached by media_id so a given photo is only ever described once
(see cache.py). This keeps daily usage well under the free-tier request cap
even on days with 1,000+ reply photos.
"""
from __future__ import annotations

import base64
import time

import requests

from . import config

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
    resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=60)

    if resp.status_code == 429:
        raise QuotaExceededError("Gemini free-tier daily/minute quota exceeded")
    resp.raise_for_status()

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return "(no description available)"


def describe_images(media_items: list[dict], cache: dict, max_requests: int) -> int:
    """Describes uncached photo media items in place (adds 'description' key).

    Returns the number of new Gemini requests made. Stops early (leaving the
    remainder uncached for the next run) once max_requests or the free-tier
    quota is hit.
    """
    requests_made = 0
    for item in media_items:
        if item.get("media_type") != "IMAGE":
            continue
        media_id = item["id"]
        if media_id in cache:
            item["description"] = cache[media_id]
            continue
        if requests_made >= max_requests:
            continue  # leave uncached; picked up on the next scheduled run

        try:
            description = describe_image(item["media_url"])
        except QuotaExceededError:
            break
        except requests.RequestException as exc:
            description = f"(description unavailable: {exc})"

        cache[media_id] = description
        item["description"] = description
        requests_made += 1
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)

    return requests_made
