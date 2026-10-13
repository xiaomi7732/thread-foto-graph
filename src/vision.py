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

If Gemini (or the image host the photos are fetched from) is down for the
whole run rather than just one flaky photo, every remaining photo would
otherwise pay its own full retry ladder before giving up -- potentially
hours. describe_images() guards against this three ways: (1) a quota hit
(QuotaExceededError), MAX_CONSECUTIVE_TRANSIENT_FAILURES Gemini-outage
signals in a row, or MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES image-download
failures in a row (tracked independently -- an ImageFetchError is never a
Gemini signal) trips a "stop_calls" flag that is returned to the caller and
fed back into every later describe_images() call this run (main.py threads
it across each thread's root-photo and reply batches), so one bad batch
stops the whole run from making further new requests, not just itself;
(2) the GitHub Actions workflow sets an explicit timeout-minutes as a
backstop in case (1) is ever bypassed.
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

# A second, independent circuit breaker: ImageFetchError (the source photo
# itself can't be downloaded -- expired/stale CDN URL, unrelated host down)
# is deliberately excluded from the Gemini-outage breaker above (it isn't a
# Gemini signal at all), but a broad image-host outage would otherwise be
# just as unbounded -- every photo pays a 30s connect/read timeout with no
# stopping condition. This threshold stops new per-photo attempts
# altogether (same stop_calls flag) once the image host itself looks down.
MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES = 5

# A third, independent circuit breaker: a non-transient Gemini HTTP error
# (a 4xx that isn't 429 -- e.g. 401/403 from a revoked/invalid API key, or
# 400 from a retired/renamed model) is an affirmative "Gemini is responding,
# just not retryably" signal, so an *isolated* one (bracketed by real
# outage failures) is deliberately excluded from the Gemini-outage breaker
# above and even clears its streak (see the `else` branch in
# describe_images()). But that same reasoning does nothing to bound a
# *persistent* one -- a revoked key fails identically on every photo and
# would otherwise burn the entire run's time/budget one doomed call at a
# time (the most likely whole-run Gemini failure mode in practice) with no
# stopping condition except the GitHub Actions workflow timeout, which
# would also mean no markdown/graph output ever gets committed.
MAX_CONSECUTIVE_NON_RETRYABLE_FAILURES = 5


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


# Prefix of the placeholder stored as a photo's "description" when Gemini
# ultimately failed (not pending -- see PENDING vs this). Shared with
# topic_graph.py so it can exclude this boilerplate from the TF-IDF
# clustering corpus -- it's identical across every failed photo in a run, so
# feeding it to the vectorizer artificially inflates similarity between
# otherwise-unrelated threads that both happened to have a failed photo.
DESCRIPTION_UNAVAILABLE_PREFIX = "(description unavailable:"

# Kept only so topic_graph._is_real_description() can still exclude it from
# the clustering corpus for any already-cached run that stored this literal
# string before GeminiResponseShapeError existed (see below) -- no longer
# produced by describe_image() itself. Historically this was returned as a
# *successful* description whenever Gemini answered 200 but its body lacked
# the expected candidate/text shape (e.g. a safety-filter block); that was a
# bug (round 10B review finding): it got permanently cached as if real, and
# silently reset all three circuit breakers every time it happened, letting
# a systemic cause (e.g. an account-level safety-filter change) burn the
# entire run's budget with no breaker ever tripping and poison the cache
# forever with no retry. Fixed by raising GeminiResponseShapeError instead,
# so this case now flows through the exact same non-cached,
# budget-charged, non-retryable-breaker-counted path as any other
# non-transient Gemini failure.
NO_DESCRIPTION_TEXT = "(no description available)"

# Gemini's candidate.finishReason values that genuinely indicate the model
# declined to answer for content/policy reasons. Deliberately an explicit
# allowlist, NOT a truthiness check on finishReason -- it's present on
# *every* candidate, including a normal healthy completion ("STOP") or a
# truncated-but-otherwise-fine one ("MAX_TOKENS"); treating any truthy
# value as a block would misclassify those (and any genuinely unexpected
# response shape, which still carries "STOP") as a deterministic content
# block, permanently caching them and incorrectly resetting every circuit
# breaker (round 11B review finding).
_BLOCKING_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "PROHIBITED_CONTENT",
        "RECITATION",
        "BLOCKLIST",
        "SPII",
        "IMAGE_SAFETY",
        "LANGUAGE",
    }
)


class ImageFetchError(requests.RequestException):
    """Raised when fetching the source photo itself fails (stale/expired CDN
    URL, unrelated host down) -- distinct from a Gemini-call failure so it's
    never mistaken for a Gemini outage signal by describe_images()'s circuit
    breaker (see _is_outage_signal).
    """


class GeminiResponseShapeError(requests.RequestException):
    """Raised when Gemini answers 200 but its response body doesn't contain
    the expected candidate/text shape *and* carries no recognizable
    block/finish reason (so describe_image() can't attribute it to routine,
    per-photo content moderation -- see the `block_reason` branch next to
    where this is raised, which returns a normal cacheable description
    instead for that case). This is therefore treated as a potential
    systemic problem (API bug, response-schema change) rather than
    expected content-specific behavior: deliberately a
    requests.RequestException (not a plain success) so describe_images()
    treats it like any other Gemini-side failure -- never cached, charged
    against the run's request budget, and counted toward the
    non-retryable-failure circuit breaker -- instead of silently resetting
    every breaker and permanently caching the placeholder as if it were a
    real description.
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


def describe_image(image_url: str, max_attempts: int | None = None) -> tuple[str, int]:
    """Returns (description, gemini_request_attempts). The attempt count
    includes every actual POST sent to Gemini this call, not just 1 -- a
    single photo can cost several real Gemini requests (429/5xx/timeout
    retries) before succeeding or giving up, and each one counts against
    Gemini's real per-minute/per-day quota even though only one photo was
    described. Any exception raised below (QuotaExceededError, HTTPError,
    the original Timeout/ConnectionError, GeminiResponseShapeError) has
    `.gemini_request_attempts` set on it too, so describe_images() can
    charge the run's budget accurately even on failure.

    `max_attempts`, if given, caps the total number of Gemini POSTs this
    call will make (across all of its own retry ladders combined) -- once
    reached, any retry that would otherwise be attempted instead gives up
    immediately (same as exhausting that ladder's own retry count). Without
    this, a single photo could consume several requests past the caller's
    remaining MAX_VISION_REQUESTS_PER_RUN budget, since describe_images()
    only checks the budget *before* starting a photo, not mid-retry.
    """
    try:
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        # Reading .content can itself raise a streaming-related
        # RequestException (e.g. ChunkedEncodingError on a truncated body)
        # -- keep it inside this try block so it's wrapped as ImageFetchError
        # too, same as every other image-host failure, instead of escaping
        # bare and being misclassified downstream as a Gemini-side failure.
        mime_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        b64_data = base64.b64encode(img_resp.content).decode("ascii")
    except requests.RequestException as exc:
        # Keep image-fetch failures (stale/expired CDN URL, unrelated host
        # down) distinct from Gemini-call failures -- they must never count
        # toward the Gemini-outage circuit breaker in describe_images()
        # (see ImageFetchError / _is_outage_signal below).
        raise ImageFetchError(str(exc)) from exc

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
    post_attempts = 0
    # Beyond a plain Timeout/ConnectionError, an overloaded/misbehaving
    # backend can also manifest as a truncated or corrupted response body
    # (ChunkedEncodingError, ContentDecodingError) or a malformed JSON body
    # on an ostensible 200 (caught separately below, as json.JSONDecodeError
    # / requests.exceptions.JSONDecodeError) -- all are treated as the same
    # kind of transient failure, sharing the one retry ladder/counter.
    TRANSIENT_CONNECTION_ERRORS = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )

    def _attempts_exhausted() -> bool:
        # True once this call has used up every attempt the caller said it
        # may use (see max_attempts docstring above) -- a retry ladder being
        # otherwise willing to try again must still stop here.
        return max_attempts is not None and post_attempts >= max_attempts

    while True:
        post_attempts += 1
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=60)
        except TRANSIENT_CONNECTION_ERRORS as exc:
            retries_transient += 1
            if retries_transient > MAX_TRANSIENT_RETRIES or _attempts_exhausted():
                exc.gemini_request_attempts = post_attempts
                raise
            delay = _transient_retry_delay(retries_transient)
            if isinstance(exc, requests.Timeout):
                kind = "timed out"
            elif isinstance(exc, requests.ConnectionError):
                kind = "connection failed"
            else:
                kind = "response body corrupted"
            print(
                f"  Gemini request {kind} -- retrying in {delay:.0f}s "
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
                err = QuotaExceededError("Gemini free-tier daily/minute quota exceeded (no retry-after given)")
                err.gemini_request_attempts = post_attempts
                raise err
            if retries_429 >= MAX_429_RETRIES or _attempts_exhausted():
                err = QuotaExceededError("Gemini free-tier daily/minute quota exceeded")
                err.gemini_request_attempts = post_attempts
                raise err
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
            if retries_transient <= MAX_TRANSIENT_RETRIES and not _attempts_exhausted():
                delay = _transient_retry_delay(retries_transient)
                print(
                    f"  Gemini server error ({resp.status_code}) -- retrying in "
                    f"{delay:.0f}s (attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                    flush=True,
                )
                time.sleep(delay)
                continue
            # Retries exhausted -- fall through to raise_for_status() just
            # below, in this same iteration, so it raises the HTTPError for
            # this still-bad `resp` (do NOT `break`/`continue` here, or the
            # loop exits without ever validating/raising on this response).

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            exc.gemini_request_attempts = post_attempts
            raise

        try:
            data = resp.json()
        except ValueError as exc:
            # Malformed/truncated JSON on an ostensible 200 -- same kind of
            # transient backend hiccup as a dropped connection, so it shares
            # the connection-error retry ladder/counter above rather than
            # failing (and undercharging the budget) on the first occurrence.
            retries_transient += 1
            if retries_transient > MAX_TRANSIENT_RETRIES or _attempts_exhausted():
                exc.gemini_request_attempts = post_attempts
                raise
            delay = _transient_retry_delay(retries_transient)
            print(
                f"  Gemini response body malformed -- retrying in {delay:.0f}s "
                f"(attempt {retries_transient}/{MAX_TRANSIENT_RETRIES})...",
                flush=True,
            )
            time.sleep(delay)
            continue

        break

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not text:
            # An empty/whitespace-only text part is the close cousin of the
            # missing-candidate shape below (and of the documented "STOP
            # with no parts" quirk) -- Gemini answered 200 but produced
            # nothing usable. Route it through the same KeyError/IndexError
            # handling so it's never cached as a fake successful
            # description (which would render as permanently "pending" and
            # silently reset every circuit breaker every run).
            raise KeyError("empty text")
    except (KeyError, IndexError):
        # promptFeedback.blockReason only ever appears on an actual block,
        # so it's safe to treat by plain truthiness. finishReason, however,
        # is present on *every* candidate including a normal, healthy
        # generation ("STOP"), truncation ("MAX_TOKENS"), etc. -- it must
        # be matched against an explicit set of genuinely blocking values,
        # not truthiness, or a "STOP"-with-no-parts response (a documented,
        # reproducible Gemini quirk -- see googleapis/js-genai#1769 -- and
        # the exact kind of API bug/schema-change GeminiResponseShapeError
        # below exists to catch) would be misclassified as a deterministic
        # content block: permanently cached and silently resetting every
        # circuit breaker, instead of being retried/counted as the
        # potential systemic problem it actually is.
        block_reason = data.get("promptFeedback", {}).get("blockReason")
        if not block_reason:
            finish_reason = (data.get("candidates") or [{}])[0].get("finishReason")
            if finish_reason in _BLOCKING_FINISH_REASONS:
                block_reason = finish_reason
        if block_reason:
            # A genuine, content-specific safety/policy block -- Gemini
            # processed the request successfully and declined to describe
            # *this particular photo*. This is a deterministic, per-photo
            # outcome (retrying changes nothing), not evidence that the
            # API key/model/connection itself is broken -- so unlike the
            # genuinely-unexpected-shape case below, it's safe (and
            # correct) to treat as a normal successful return: cached so
            # this photo isn't retried every run forever, and never
            # counted toward the non-retryable-failure circuit breaker
            # (which exists to catch a *systemic* problem, not routine
            # per-content moderation). Tagged with
            # DESCRIPTION_UNAVAILABLE_PREFIX so it's still excluded from
            # topic_graph's TF-IDF clustering corpus like any other
            # non-description placeholder.
            return (
                f"{DESCRIPTION_UNAVAILABLE_PREFIX} blocked by Gemini, reason: "
                f"{block_reason})",
                post_attempts,
            )
        exc = GeminiResponseShapeError(
            "Gemini response missing expected candidate/text with no "
            "recognizable block/finish reason (likely an API bug or "
            "response-schema change)"
        )
        exc.gemini_request_attempts = post_attempts
        raise exc
    return text, post_attempts


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
    if isinstance(
        exc,
        (
            requests.Timeout,
            requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
            requests.exceptions.JSONDecodeError,
        ),
    ):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in TRANSIENT_STATUS_CODES


_STALE_BLOCK_PLACEHOLDER_RE = re.compile(
    re.escape(DESCRIPTION_UNAVAILABLE_PREFIX) + r" blocked by Gemini, reason: (.+)\)$"
)

# Used only by the cache self-heal below. Cached "blocked by Gemini"
# placeholders can come from either promptFeedback.blockReason (always
# checked by plain truthiness, never buggy -- can legitimately be values
# like "OTHER"/"BLOCKED_REASON_UNSPECIFIED" that aren't in
# _BLOCKING_FINISH_REASONS) or candidate.finishReason (checked against
# _BLOCKING_FINISH_REASONS since round 11B). The cached string alone can't
# tell us which path produced it, so this is the broader, safe-to-trust
# superset: anything in it is left alone as a genuine block; anything not
# in it is treated as possible fallout from the pre-11B finishReason
# truthiness bug and gets a retry.
_TRUSTED_BLOCK_REASONS = _BLOCKING_FINISH_REASONS | {"OTHER", "BLOCKED_REASON_UNSPECIFIED"}


def _is_stale_cached_placeholder(value: str) -> bool:
    """True for a cached "description" written by a now-fixed bug in a past
    version of describe_image()/describe_images() that should never be
    trusted as a real cache hit -- see the two cases in describe_images()'s
    cache-read self-heal:
    1. The exact legacy NO_DESCRIPTION_TEXT literal (cached forever before
       GeminiResponseShapeError existed).
    2. A "blocked by Gemini, reason: X" placeholder whose reason isn't a
       trusted/known block reason -- written during the brief window where
       finishReason was checked by truthiness instead of against the
       allowlist (e.g. "reason: STOP", "reason: MAX_TOKENS").
    """
    if value == NO_DESCRIPTION_TEXT:
        return True
    match = _STALE_BLOCK_PLACEHOLDER_RE.match(value)
    return bool(match) and match.group(1) not in _TRUSTED_BLOCK_REASONS


def describe_images(
    media_items: list[dict],
    cache: dict,
    max_requests: int,
    on_progress=None,
    stop_calls: bool = False,
    consecutive_transient_failures: int = 0,
    consecutive_image_fetch_failures: int = 0,
    consecutive_non_retryable_failures: int = 0,
) -> tuple[int, bool, int, int, int]:
    """Describes every uncached photo in each item in place, storing a
    'photo_descriptions' list on the item (one entry per photo -- a
    CAROUSEL_ALBUM can hold several, not just its cover image; see
    threads_client.photo_items). A list entry is None if that specific photo
    wasn't described this run (budget/quota/outage), which topic_graph.py
    renders as a "pending" placeholder rather than silently dropping it.

    `stop_calls`, if already True, means a previous call (this run) already
    decided to stop issuing new Gemini requests (quota hit, a sustained
    Gemini outage, a sustained image-host outage, or a sustained
    non-retryable Gemini error such as a revoked API key -- see
    MAX_CONSECUTIVE_NON_RETRYABLE_FAILURES) -- every item is still walked so
    photo_descriptions is set correctly, but no new network calls are
    attempted. `consecutive_transient_failures`, `consecutive_image_fetch_failures`,
    and `consecutive_non_retryable_failures` are the running streaks (see
    _is_outage_signal and the MAX_CONSECUTIVE_*_FAILURES constants) from
    prior calls this run, so any of the three circuit breakers can trip
    even when a single batch alone never reaches its threshold (e.g.
    main.py's root-photo batches are usually just one photo each). All five
    are returned as (requests_made, stop_calls, consecutive_transient_failures,
    consecutive_image_fetch_failures, consecutive_non_retryable_failures) so
    the caller can carry them forward across its own multiple
    describe_images() calls in the same run (see main.py) -- without this,
    an outage/quota/auth failure detected on one batch wouldn't stop the
    very next batch from trying fresh calls anyway.

    `requests_made` counts actual Gemini POST attempts, not photos -- a
    single photo can cost several real Gemini requests (429/5xx/timeout
    retries inside describe_image()) before succeeding or giving up, and
    each one counts against Gemini's real per-minute/per-day quota, so this
    is what `max_requests` (MAX_VISION_REQUESTS_PER_RUN) is actually
    budgeting against. `on_progress`, if given, is called after every single
    new description so the caller can persist the cache immediately -- a
    slow or interrupted run never loses
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
            if media_id in cache and _is_stale_cached_placeholder(cache[media_id]):
                # Self-heal pre-existing-bug artifacts written by now-fixed
                # versions of this function: the exact NO_DESCRIPTION_TEXT
                # literal (cached forever before GeminiResponseShapeError
                # existed), and the brief window (commit 34c82a2, fixed by
                # 6b2cb9b) where a finishReason of e.g. STOP/MAX_TOKENS was
                # wrongly treated as a genuine block and cached as
                # '(description unavailable: blocked by Gemini, reason:
                # STOP)'. Treat either as stale/invalid instead of trusting
                # it as a cache hit, so any photo poisoned by either past
                # bug gets a real chance to be re-described (or correctly
                # re-classified as a genuine block) on this run.
                del cache[media_id]
            if media_id in cache:
                descriptions.append(cache[media_id])
                continue
            if stop_calls or requests_made >= max_requests:
                # Leave as None (pending) -- topic_graph.py distinguishes
                # this from "no photo" via the list entry existing at all.
                descriptions.append(None)
                continue

            try:
                description, attempts = describe_image(
                    photo["media_url"], max_attempts=max(1, max_requests - requests_made)
                )
            except QuotaExceededError as exc:
                # Don't abandon the whole batch silently: keep looping so
                # later cache-hit items/photos still get their description,
                # just stop making new network calls. This used to be a
                # silent no-op from the caller's point of view (no print),
                # which made "why is this photo still pending?" undiagnosable
                # from run logs alone -- print once per describe_images() call
                # so it's visible exactly when/why it happened.
                requests_made += getattr(exc, "gemini_request_attempts", 1)
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
                descriptions.append(f"{DESCRIPTION_UNAVAILABLE_PREFIX} {_sanitize(str(exc))})")
                if isinstance(exc, ImageFetchError):
                    # No Gemini call was ever attempted for this photo -- don't
                    # charge it against this run's Gemini request budget, and
                    # leave the Gemini outage streak untouched either way (a
                    # single unrelated bad photo URL shouldn't erase a real
                    # streak of Gemini failures bracketing it). Only count it
                    # toward the *image-host* breaker if it actually looks like
                    # the host is struggling (timeout/connection/5xx, via the
                    # original exception preserved on __cause__) -- a
                    # permanent per-photo failure (404/403, bad URL) recurs
                    # identically every run since it's never cached, and would
                    # otherwise permanently stop_calls every run forever even
                    # though the image host is perfectly healthy.
                    if _is_outage_signal(exc.__cause__ or exc):
                        consecutive_image_fetch_failures += 1
                        # A real outage signal of any kind (Gemini or image-host)
                        # means the *previous* non-retryable Gemini streak (if any)
                        # wasn't actually a sustained run of the same persistent
                        # error -- it was interrupted by an unrelated outage, so
                        # don't let it carry forward and eventually trip that
                        # breaker on a non-consecutive sequence.
                        consecutive_non_retryable_failures = 0
                        if consecutive_image_fetch_failures >= MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES:
                            print(
                                f"  {consecutive_image_fetch_failures} consecutive photo-download "
                                "failures -- treating as an image-host outage and stopping new "
                                "requests for this run.",
                                flush=True,
                            )
                            stop_calls = True
                    else:
                        # A permanent, unrelated-to-the-host failure (bad URL,
                        # 404/403) -- the host itself is fine, so don't let it
                        # count toward (or clear) the image-host outage streak.
                        pass
                else:
                    requests_made += getattr(exc, "gemini_request_attempts", 1)
                    consecutive_image_fetch_failures = 0
                    if _is_outage_signal(exc):
                        consecutive_transient_failures += 1
                        consecutive_non_retryable_failures = 0
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
                        # A non-transient Gemini response (e.g. a 4xx that isn't
                        # 429) is an affirmative "Gemini is responding, just not
                        # retryably" signal -- unlike ImageFetchError, it's safe
                        # (and correct) to let it clear the outage streak.
                        consecutive_transient_failures = 0
                        # But a *persistent* non-retryable error (e.g. a revoked
                        # API key returning 401/403 on every call, or a retired
                        # model name returning 400) is unbounded on its own --
                        # it never looks like an "outage" so the breaker above
                        # never trips, and it would otherwise burn the entire
                        # run's budget one doomed call at a time. Track it with
                        # its own independent streak/breaker.
                        consecutive_non_retryable_failures += 1
                        if consecutive_non_retryable_failures >= MAX_CONSECUTIVE_NON_RETRYABLE_FAILURES:
                            print(
                                f"  {consecutive_non_retryable_failures} consecutive non-retryable Gemini "
                                "errors (e.g. invalid API key/model) -- stopping new requests for this run.",
                                flush=True,
                            )
                            stop_calls = True
                time.sleep(MIN_SECONDS_BETWEEN_CALLS)
                continue

            if description:
                # Defense in depth: describe_image() should never return a
                # falsy "success" (an empty/whitespace-only description is
                # now rejected there too), but never cache one here either
                # if it somehow did -- caching "" would be a permanent,
                # unrecoverable false "pending" forever (see the empty-text
                # finding this guards against).
                cache[media_id] = description
            descriptions.append(description)
            if on_progress is not None:
                on_progress()
            requests_made += attempts
            consecutive_transient_failures = 0
            consecutive_image_fetch_failures = 0
            consecutive_non_retryable_failures = 0
            time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        item["photo_descriptions"] = descriptions

    return (
        requests_made,
        stop_calls,
        consecutive_transient_failures,
        consecutive_image_fetch_failures,
        consecutive_non_retryable_failures,
    )
