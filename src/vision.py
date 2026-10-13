"""Vision descriptions via Google Gemini Flash (free tier).

Each image is downloaded, sent to Gemini for a short text description, and the
result is cached by media_id so a given photo is only ever described once
(see cache.py). This keeps daily usage well under the free-tier request cap
even on days with 1,000+ reply photos.

On a 429 (rate limit), describe_image() retries in-process a couple of times
first -- but only when Gemini actually tells us how long to wait (its
Retry-After header or retryDelay body field). If neither is present, there's
nothing real to honor, so it fails fast (raises QuotaExceededError
immediately) rather than guessing a delay -- blind guessing was tried and
just burned run time without reliably succeeding. Either way,
describe_images() then leaves the photo as a "pending" placeholder for the
next scheduled run.

On a 5xx (the model itself briefly overloaded/unavailable -- common on the
free-tier "lite" model) or a read timeout, describe_image() also retries
in-process a couple of times with a short fixed backoff (Gemini gives no
Retry-After for these). If it still fails after that, describe_images()
records "(description unavailable: ...)" for that photo without caching it,
so it's retried fresh on the next run -- but the failure is now also printed
to the run log, so a run hitting a lot of these is visible there instead of
only showing up later in the committed markdown.
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

# How many times to retry a single 429 before giving up on it for this run.
# Only applies when Gemini actually gives us a Retry-After/retryDelay to
# honor -- see describe_image(). Kept small -- if Gemini is still
# rate-limiting after a couple of honored waits, the per-minute window
# probably isn't the issue (more likely the daily quota), and further
# retries just burn run time for no benefit.
MAX_429_RETRIES = 2
# Cap any single wait so one unusually large retryDelay can't stall a run.
MAX_RETRY_DELAY_SECONDS = 60.0

# 5xx responses mean the model itself is briefly overloaded/unavailable --
# this is common on the free-tier "lite" model and is unrelated to our own
# rate limiting. Unlike 429, Gemini gives no Retry-After for these, so a
# short fixed backoff is all there is to go on. Read timeouts get the same
# treatment -- both are transient and usually clear within a few seconds.
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 2
TRANSIENT_RETRY_DELAY_SECONDS = 5.0

# Matches "?key=..." / "&key=..." style query params so a stray secret can
# never make it into a committed file (data/topics.md) via an error message.
_QUERY_SECRET_RE = re.compile(r"([?&](?:key|access_token)=)[^&\s)]+", re.IGNORECASE)


def _sanitize(message: str) -> str:
    return _QUERY_SECRET_RE.sub(r"\1REDACTED", message)


class QuotaExceededError(RuntimeError):
    """Raised when Gemini returns a 429; caller should stop and resume later."""


def _parse_retry_delay(resp: requests.Response) -> float | None:
    """Extracts a retry delay (seconds) from a 429 response, if present.
    Gemini doesn't document a guaranteed mechanism for this -- there's no
    standard Retry-After header in its official error docs -- but checks,
    in order:
      1. A literal `Retry-After` header, either delay-seconds or an
         HTTP-date (both are valid per RFC 9110).
      2. A `retryDelay` field (e.g. "19s") inside the JSON error body's
         `google.rpc.RetryInfo` detail, which Gemini sometimes includes.
    Returns None if neither is present/parseable, so the caller can fall
    back to a fixed default wait.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            try:
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass

    try:
        body = resp.json()
    except ValueError:
        return None
    for detail in body.get("error", {}).get("details", []):
        delay = detail.get("retryDelay")
        if delay:
            try:
                return float(str(delay).rstrip("s"))
            except ValueError:
                continue
    return None


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

    retries_429 = 0
    retries_transient = 0
    while True:
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=60)
        except requests.Timeout:
            retries_transient += 1
            if retries_transient > MAX_TRANSIENT_RETRIES:
                raise
            print(
                f"  Gemini request timed out -- retrying in {TRANSIENT_RETRY_DELAY_SECONDS:.0f}s "
                f"(attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                flush=True,
            )
            time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
            continue

        if resp.status_code == 429:
            delay = _parse_retry_delay(resp)
            if delay is None:
                # Gemini gave us no Retry-After/retryDelay to honor -- we'd just
                # be guessing how long to wait, and a guessed delay has already
                # proven to burn several minutes of run time for no benefit (see
                # README). Fail fast instead of retrying blind.
                raise QuotaExceededError("Gemini free-tier daily/minute quota exceeded (no retry-after given)")
            if retries_429 >= MAX_429_RETRIES:
                raise QuotaExceededError("Gemini free-tier daily/minute quota exceeded")
            retries_429 += 1
            delay = min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS)
            print(
                f"  Gemini rate limit (429) -- retrying in {delay:.0f}s "
                f"(attempt {retries_429}/{MAX_429_RETRIES})...",
                flush=True,
            )
            time.sleep(delay)
            continue

        if resp.status_code in TRANSIENT_STATUS_CODES:
            retries_transient += 1
            if retries_transient > MAX_TRANSIENT_RETRIES:
                break  # Give up -- raise_for_status() below raises the HTTPError.
            print(
                f"  Gemini server error ({resp.status_code}) -- retrying in "
                f"{TRANSIENT_RETRY_DELAY_SECONDS:.0f}s (attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                flush=True,
            )
            time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
            continue

        break

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
                # cached description. This survived in-process retries (see
                # describe_image()), so it's worth a line in the run log --
                # otherwise a run full of these is invisible until someone
                # reads the committed markdown.
                print(f"  Gemini request failed, giving up for this run: {_sanitize(str(exc))}", flush=True)
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
