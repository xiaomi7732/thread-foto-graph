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
in-process, up to 3 times, with an exponential backoff (10s, 20s, 40s --
Gemini gives no Retry-After for these). A live test confirmed this is the
right fix: hammering the API ~5s apart mostly got 503 "high demand" errors,
while the same model spaced ~15s apart succeeded every time. If it still
fails after exhausting retries, describe_images() records
"(description unavailable: ...)" for that photo without caching it, so it's
retried fresh on the next run -- and the failure is printed to the run log,
so a run hitting a lot of these is visible there instead of only showing up
later in the committed markdown.

If Gemini is down for the whole run rather than just one flaky photo, every
remaining photo would otherwise pay its own full retry ladder before giving
up -- potentially hours. describe_images() guards against this two ways:
(1) a quota hit (QuotaExceededError) or MAX_CONSECUTIVE_TRANSIENT_FAILURES
in a row trips a "stop_calls" flag that is returned to the caller and fed
back into every later describe_images() call this run (main.py threads it
across each thread's root-photo and reply batches), so one bad batch stops
the whole run from making further new requests, not just itself; and (2) the
GitHub Actions workflow sets an explicit timeout-minutes as a backstop in
case (1) is ever bypassed.
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
# rate limiting. Unlike 429, Gemini gives no Retry-After for these, so there's
# nothing to honor like the 429 path does -- just an exponential backoff.
# A live A/B test confirmed this matters: hammering the API with ~5s between
# attempts produced mostly 503 "high demand" errors, but the exact same model
# spaced ~15s apart succeeded 3/3 -- so waiting longer, not switching models,
# is the fix. Read timeouts get the same treatment -- both are transient and
# tend to clear once the model has had a moment to catch up.
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_RETRY_BASE_DELAY_SECONDS = 10.0
TRANSIENT_RETRY_MAX_DELAY_SECONDS = 60.0

# Circuit breaker across an entire describe_images() run: if Gemini is
# genuinely down (not just one flaky photo), every photo would otherwise
# exhaust its own full retry ladder before giving up -- worst case roughly
# 4 * 60s timeouts + 10s + 20s + 40s per photo, which multiplied across a
# large backlog could keep a run alive for hours without making any
# progress. After this many consecutive failures (after each photo's own
# retries are already exhausted), stop attempting new calls for the rest
# of the run -- same "leave as pending, retry next run" behavior as a 429.
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 5


def _transient_retry_delay(retry_number: int) -> float:
    """Exponential backoff for retry `retry_number` (1-indexed): 10s, 20s,
    40s, ... capped at TRANSIENT_RETRY_MAX_DELAY_SECONDS so one photo can't
    stall a run indefinitely.
    """
    return min(TRANSIENT_RETRY_BASE_DELAY_SECONDS * (2 ** (retry_number - 1)), TRANSIENT_RETRY_MAX_DELAY_SECONDS)

# Matches "?key=..." / "&key=..." style query params so a stray secret can
# never make it into a committed file (data/topics.md) via an error message.
_QUERY_SECRET_RE = re.compile(r"([?&](?:key|access_token)=)[^&\s)]+", re.IGNORECASE)


def _sanitize(message: str) -> str:
    return _QUERY_SECRET_RE.sub(r"\1REDACTED", message)


class QuotaExceededError(RuntimeError):
    """Raised when Gemini returns a 429; caller should stop and resume later."""


class ImageFetchError(requests.RequestException):
    """Raised when fetching the source photo itself fails (stale/expired CDN
    URL, unrelated host down) -- distinct from a Gemini-call failure so it's
    never mistaken for a Gemini outage signal by describe_images()'s circuit
    breaker (see _is_outage_signal).
    """


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
    try:
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
    except requests.RequestException as exc:
        # Keep image-fetch failures (stale/expired CDN URL, unrelated host
        # down) distinct from Gemini-call failures -- they must never count
        # toward the Gemini-outage circuit breaker in describe_images()
        # (see ImageFetchError / _is_outage_signal below).
        raise ImageFetchError(str(exc)) from exc
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
            delay = _transient_retry_delay(retries_transient)
            print(
                f"  Gemini request timed out -- retrying in {delay:.0f}s "
                f"(attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                flush=True,
            )
            time.sleep(delay)
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
            delay = _transient_retry_delay(retries_transient)
            print(
                f"  Gemini server error ({resp.status_code}) -- retrying in "
                f"{delay:.0f}s (attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                flush=True,
            )
            time.sleep(delay)
            continue

        break

    resp.raise_for_status()

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return "(no description available)"


def _is_outage_signal(exc: requests.RequestException) -> bool:
    """True only for failures that indicate Gemini itself is struggling --
    a read timeout or connection failure that survived describe_image()'s
    own retries, or a 5xx that survived its retries too. Used by
    describe_images()'s circuit breaker so it doesn't mistake an unrelated
    *permanent* failure (e.g. a stale/403 photo URL, or a non-retryable 4xx
    straight from Gemini) for a Gemini outage and halt an otherwise-healthy
    run.
    """
    if isinstance(exc, ImageFetchError):
        # Never a Gemini signal -- the image host/CDN, not Gemini, failed.
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in TRANSIENT_STATUS_CODES


def describe_images(
    media_items: list[dict],
    cache: dict,
    max_requests: int,
    on_progress=None,
    stop_calls: bool = False,
    consecutive_transient_failures: int = 0,
) -> tuple[int, bool, int]:
    """Describes every uncached photo in each item in place, storing a
    'photo_descriptions' list on the item (one entry per photo -- a
    CAROUSEL_ALBUM can hold several, not just its cover image; see
    threads_client.photo_items). A list entry is None if that specific photo
    wasn't described this run (budget/quota/outage), which topic_graph.py
    renders as a "pending" placeholder rather than silently dropping it.

    `stop_calls`, if already True, means a previous call (this run) already
    decided to stop issuing new Gemini requests (quota hit or a sustained
    outage) -- every item is still walked so photo_descriptions is set
    correctly, but no new network calls are attempted. `consecutive_transient_
    failures` is the running count of consecutive outage-signal failures
    (see _is_outage_signal) from prior calls this run, so the circuit
    breaker can trip even when a single batch alone never reaches the
    threshold (e.g. main.py's root-photo batches are usually just one
    photo each). Both are returned as
    (requests_made, stop_calls, consecutive_transient_failures) so the
    caller can carry them forward across its own multiple describe_images()
    calls in the same run (see main.py) -- without this, a 429/outage
    detected on one batch wouldn't stop the very next batch from trying
    fresh calls anyway.

    Returns the number of new Gemini requests made. `on_progress`, if given,
    is called after every single new description so the caller can persist
    the cache immediately -- a slow or interrupted run never loses
    already-described photos.
    """
    requests_made = 0
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
            if stop_calls or requests_made >= max_requests:
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
                if not stop_calls:
                    print(f"  {exc} -- remaining photos in this batch left pending.", flush=True)
                stop_calls = True
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
                if _is_outage_signal(exc):
                    consecutive_transient_failures += 1
                    if consecutive_transient_failures >= MAX_CONSECUTIVE_TRANSIENT_FAILURES:
                        # Gemini looks genuinely down, not just one flaky photo --
                        # stop burning the rest of this run's budget/time on calls
                        # that are very unlikely to succeed; remaining photos are
                        # left pending for the next scheduled run instead.
                        print(
                            f"  {consecutive_transient_failures} consecutive Gemini outage-like "
                            "failures -- treating as an outage and stopping new requests for this run.",
                            flush=True,
                        )
                        stop_calls = True
                else:
                    # A permanent/unrelated failure (bad image URL, non-retryable
                    # 4xx) doesn't indicate Gemini itself is unhealthy -- don't
                    # let it count toward the outage breaker.
                    consecutive_transient_failures = 0
                time.sleep(MIN_SECONDS_BETWEEN_CALLS)
                continue

            cache[media_id] = description
            descriptions.append(description)
            if on_progress is not None:
                on_progress()
            requests_made += 1
            consecutive_transient_failures = 0
            time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        item["photo_descriptions"] = descriptions

    return requests_made, stop_calls, consecutive_transient_failures
