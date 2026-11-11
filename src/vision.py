"""Cached vision descriptions via Gemini or local Ollama with cloud fallback.

Images are downloaded once per attempt and descriptions are cached by media
ID with provider/model provenance. Legacy string entries remain readable.
VisionRun retains independent provider budgets and breakers across batches.

Gemini honors explicit 429 retry delays and uses bounded exponential retries
for transient failures. Local inference gets one attempt per photo before
optional fallback. Provider failures never poison the cache or disable the
other healthy provider; source-host outages and the run deadline stop both.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from . import config, threads_client
from .cache import description_text, make_entry

GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

PROMPT = (
    "Respond in plain text using exactly these four labeled sections:\n"
    "Description: Describe this photo in one or two concise sentences, focusing "
    "on the subject matter and any context clues relevant to a social media post.\n"
    "Photograph read: Give a brief photography-focused interpretation in one "
    "concise sentence, noting composition, light, and mood where relevant. "
    "Base the read on visible details; do not invent camera settings or intent.\n"
    "Photo type: Classify the overall style or feel in one concise phrase "
    "(for example, snapshot, artistic, documentary, or storytelling). Choose "
    "the closest fit based only on what is visible; do not invent intent.\n"
    "Watch-outs: Leave this section empty unless there is a clearly visible "
    "technical observation relevant before praising a photographic choice "
    "(such as clipped highlights, flare, focus softness, a tilted horizon, "
    "or background clutter overlapping the subject). Empty is a valid and "
    "common value; do not search for faults. Use at most one or two short "
    "clauses of observable facts only: no scores, grades, overall verdicts, "
    "praise, blame, or good/bad composition judgments. Do not prescribe fixes. "
    "When softness, high-key exposure, or motion blur could be deliberate, "
    "state that intent cannot be determined from the image. For example: "
    "'Sun disc clipped to white with halo and flare streaks' or "
    "'Soft overall; intentional or missed focus not determinable from the image'. "
    "These notes are private drafting input, never text to quote to the photographer.\n"
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

# Broader safety net: strip the entire query string off any URL in a
# sanitized message, not just a named key/access_token param. requests' own
# exception text (e.g. "404 Client Error: ... for url: <url>") embeds the
# full request URL verbatim, and urllib3's own connection-failure text
# (ConnectionError/ConnectTimeout -- the most common image-host failure
# mode) reports a scheme-less "... with url: <path>?<query>" instead.
# Either way, a source photo's CDN media_url can carry its own signed/
# session query params unrelated to our own credentials, and those end up
# committed verbatim into data/topics.md via ImageFetchError's message
# (round 14A/15A review findings) unless redacted here too.
_URL_QUERY_RE = re.compile(r"((?:https?://|with url: )[^\s?]+)\?([^\s]*)")


def _redact_url_query(match: re.Match) -> str:
    # A trailing ')' on the captured query is ambiguous: it can be part of
    # an enclosing wrapper (e.g. "(Caused by ... url: .../x?sig=abc)"), in
    # which case it must be preserved *outside* the redaction, not swallowed
    # into it (round 15B review finding -- a URL path containing a literal
    # '(' / ')', e.g. "/foo(bar).jpg?sig=abc", must still be fully
    # redacted). Treat it as a wrapper paren only if it isn't needed to
    # balance a '(' that's actually part of the URL itself.
    prefix, query = match.group(1), match.group(2)
    trailing = ""
    if query.endswith(")") and (prefix + query).count("(") < (prefix + query).count(")"):
        trailing = ")"
        query = query[:-1]
    return f"{prefix}?REDACTED{trailing}"


def _sanitize(message: str) -> str:
    message = _QUERY_SECRET_RE.sub(r"\1REDACTED", message)
    return _URL_QUERY_RE.sub(_redact_url_query, message)


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


class VisionDeadlineExceeded(requests.Timeout):
    """The run's processing deadline has been reached."""


def _timeout(deadline: float | None, seconds: float) -> float:
    remaining = seconds if deadline is None else min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise VisionDeadlineExceeded("Vision run time limit reached")
    return remaining


def fetch_image(image_url: str, deadline: float | None = None) -> tuple[str, str]:
    try:
        response = requests.get(image_url, timeout=_timeout(deadline, 30))
        response.raise_for_status()
        mime_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return mime_type, base64.b64encode(response.content).decode("ascii")
    except VisionDeadlineExceeded:
        raise
    except requests.RequestException as exc:
        raise ImageFetchError(str(exc)) from exc


def describe_image(
    image_url: str,
    max_attempts: int | None = None,
    *,
    image_data: tuple[str, str] | None = None,
    deadline: float | None = None,
) -> tuple[str, int]:
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
    mime_type, b64_data = image_data if image_data is not None else fetch_image(image_url, deadline)

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

    def wait(delay: float) -> None:
        if deadline is not None and time.monotonic() + delay >= deadline:
            exc = VisionDeadlineExceeded("Vision run time limit reached during Gemini retry")
            exc.gemini_request_attempts = post_attempts
            raise exc
        time.sleep(delay)

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
        try:
            timeout = _timeout(deadline, 60)
        except VisionDeadlineExceeded as exc:
            exc.gemini_request_attempts = post_attempts
            raise
        post_attempts += 1
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
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
            wait(delay)
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
            wait(delay)
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
                wait(delay)
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
            wait(delay)
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
    except (KeyError, IndexError, TypeError, AttributeError):
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
        body = data if isinstance(data, dict) else {}
        feedback = body.get("promptFeedback")
        block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if not block_reason:
            candidates = body.get("candidates")
            candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
            finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
            if isinstance(finish_reason, str) and finish_reason in _BLOCKING_FINISH_REASONS:
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
# like "OTHER"/"BLOCK_REASON_UNSPECIFIED" that aren't in
# _BLOCKING_FINISH_REASONS) or candidate.finishReason (checked against
# _BLOCKING_FINISH_REASONS since round 11B). The cached string alone can't
# tell us which path produced it, so this is the broader, safe-to-trust
# superset: anything in it is left alone as a genuine block; anything not
# in it is treated as possible fallout from the pre-11B finishReason
# truthiness bug and gets a retry.
#
# Note: the real Gemini enum value is "BLOCK_REASON_UNSPECIFIED" (no "ED"
# on "BLOCK"). "BLOCKED_REASON_UNSPECIFIED" is kept alongside it only so
# any already-cached entries written under the old, misspelled version of
# this set aren't needlessly re-requested -- it isn't a value Gemini can
# actually emit (round 13B review finding: the misspelling meant a
# genuine BLOCK_REASON_UNSPECIFIED block was never recognized as trusted,
# so it was self-healed and re-requested identically forever).
_TRUSTED_BLOCK_REASONS = _BLOCKING_FINISH_REASONS | {
    "OTHER",
    "BLOCK_REASON_UNSPECIFIED",
    "BLOCKED_REASON_UNSPECIFIED",
}


def _is_stale_cached_placeholder(value: object) -> bool:
    """True for a cached "description" written by a now-fixed bug in a past
    version of describe_image()/describe_images() that should never be
    trusted as a real cache hit -- see the cases in describe_images()'s
    cache-read self-heal:
    1. The exact legacy NO_DESCRIPTION_TEXT literal (cached forever before
       GeminiResponseShapeError existed).
    2. A "blocked by Gemini, reason: X" placeholder whose reason isn't a
       trusted/known block reason -- written during the brief window where
       finishReason was checked by truthiness instead of against the
       allowlist (e.g. "reason: STOP", "reason: MAX_TOKENS").
    3. A falsy (empty/whitespace-only) cached value -- written by a now-
       fixed bug where an empty Gemini text part was treated as a
       successful description (round 12B review finding) before the fix
       that stopped new ones from being cached. A pre-existing empty
       entry would otherwise be trusted as a real cache hit forever.
    """
    value = description_text(value)
    if not value or not value.strip():
        return True
    if value == NO_DESCRIPTION_TEXT:
        return True
    match = _STALE_BLOCK_PLACEHOLDER_RE.match(value)
    return bool(match) and match.group(1) not in _TRUSTED_BLOCK_REASONS


_WATCH_OUTS_RE = re.compile(
    r"^Watch-outs:[ \t]*(.*?)(?=^(?:Description|Photograph read|Photo type):|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def split_watch_outs(description: str | None) -> tuple[str | None, str]:
    """Separate drafting-only notes from the description used for clustering.

    Cache entries remain plain strings, including the new labeled section;
    legacy descriptions without it are kept unchanged (no cache invalidation).
    """
    if not description:
        return description, ""
    match = _WATCH_OUTS_RE.search(description)
    if not match:
        return description, ""
    text = (description[:match.start()] + description[match.end():]).strip()
    return text, " ".join(match.group(1).split())


class OllamaResponseError(requests.RequestException):
    """Local inference returned an unusable description."""


class OllamaRefusalError(requests.RequestException):
    """Local inference explicitly declined the request; do not bypass it."""


_LOCAL_SECTIONS = re.compile(
    r"\ADescription:[ \t]*(.+?)\nPhotograph read:[ \t]*(.+?)"
    r"\nPhoto type:[ \t]*(.+?)\nWatch-outs:[ \t]*(.*?)\Z",
    re.DOTALL,
)
_LOCAL_REFUSAL = re.compile(
    r"\A(?:(?:I'm |I am )?sorry[,.!:\s]+(?:but\s+)?)?"
    r"(?:I cannot|I can't|I won't|I will not|I'm unable to|I am unable to|"
    r"I am not able to|I'm not able to|I must decline to)\s+"
    r"(?:(?:\w+ly|in good conscience)\s+){0,3}"
    r"(?:comply|help|assist|provide|describe|analyze|analyse|process|fulfil|fulfill)\b",
    re.IGNORECASE,
)


def describe_local(image_data: tuple[str, str], deadline: float) -> str:
    with requests.Session() as session:
        # Local image payloads must not inherit ambient proxies or credentials.
        session.trust_env = False
        response = session.post(
            f"{config.OLLAMA_BASE_URL}/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [{"role": "user", "content": PROMPT, "images": [image_data[1]]}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 512},
            },
            timeout=(
                _timeout(deadline, config.OLLAMA_CONNECT_TIMEOUT_SECONDS),
                _timeout(deadline, config.OLLAMA_READ_TIMEOUT_SECONDS),
            ),
            allow_redirects=False,
        )
    response.raise_for_status()
    if 300 <= response.status_code < 400:
        raise requests.RequestException("Ollama redirects are not followed")
    try:
        data = response.json()
        message = data.get("message", {})
        if message.get("refusal"):
            raise OllamaRefusalError("Ollama declined to describe this photo")
        text = message.get("content", "").strip()
        normalized = text.replace("\r\n", "\n").replace("\u2019", "'").replace("\u2018", "'")
        match = _LOCAL_SECTIONS.fullmatch(normalized)
        refusal_text = match.group(1).strip() if match else normalized
        if _LOCAL_REFUSAL.match(refusal_text):
            raise OllamaRefusalError("Ollama declined to describe this photo")
        if data.get("done") is not True or data.get("done_reason") == "length":
            raise OllamaResponseError("Ollama description was incomplete")
        if not match or not all(part.strip() for part in match.groups()[:3]):
            raise OllamaResponseError("Ollama description missing expected sections")
    except (ValueError, AttributeError, TypeError) as exc:
        raise OllamaResponseError("Ollama response had an invalid shape") from exc
    return text


@dataclass
class VisionRun:
    """Provider budgets and breakers shared by every batch in one run."""

    provider: str = field(default_factory=lambda: config.VISION_PROVIDER)
    gemini_budget: int = field(default_factory=lambda: config.MAX_VISION_REQUESTS_PER_RUN)
    local_budget: int = field(default_factory=lambda: config.MAX_LOCAL_VISION_REQUESTS_PER_RUN)
    deadline: float = field(default_factory=lambda: time.monotonic() + config.VISION_RUN_TIMEOUT_SECONDS)
    gemini_requests: int = 0
    local_requests: int = 0
    gemini_disabled: bool = False
    local_disabled: bool = False
    stop_calls: bool = False
    transient_failures: int = 0
    image_failures: int = 0
    non_retryable_failures: int = 0
    local_failures: int = 0
    local_reason: str | None = None
    cache_hits: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)

    def can_local(self) -> bool:
        return (
            self.provider in {"auto", "ollama"} and not self.local_disabled
            and self.local_requests < self.local_budget
        )

    def can_gemini(self) -> bool:
        return (
            (self.provider == "gemini" or (self.provider == "auto" and bool(config.GEMINI_API_KEY)))
            and not self.gemini_disabled and self.gemini_requests < self.gemini_budget
        )

    def stopped(self) -> bool:
        return (
            self.stop_calls or time.monotonic() >= self.deadline
            or not (self.can_local() or self.can_gemini())
        )

    def image_failed(self, exc: ImageFetchError) -> None:
        if _is_outage_signal(exc.__cause__ or exc):
            self.image_failures += 1
            if self.image_failures >= MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES:
                self.stop_calls = True
                print("Photo host outage -- stopping new image requests for this run.", flush=True)

    def describe(self, image_url: str) -> tuple[str | None, dict | None]:
        image_data = None
        fallback_reason = None
        if self.can_local():
            try:
                image_data = fetch_image(image_url, self.deadline)
            except ImageFetchError as exc:
                self.image_failed(exc)
                print(f"Photo download failed: {_sanitize(str(exc))}", flush=True)
                return f"{DESCRIPTION_UNAVAILABLE_PREFIX} {_sanitize(str(exc))})", None
            except VisionDeadlineExceeded:
                self.stop_calls = True
                return None, None
            self.image_failures = 0
            self.local_requests += 1
            try:
                text = describe_local(image_data, self.deadline)
            except OllamaRefusalError:
                self.local_failures = 0
                text = f"{DESCRIPTION_UNAVAILABLE_PREFIX} blocked by Ollama)"
                return text, make_entry(text, "ollama", config.OLLAMA_MODEL)
            except VisionDeadlineExceeded:
                self.stop_calls = True
                return None, None
            except requests.RequestException as exc:
                self.local_failures += 1
                if isinstance(exc, OllamaResponseError):
                    fallback_reason = "ollama_invalid_output"
                    self.local_disabled = self.local_failures >= 3
                else:
                    fallback_reason = "ollama_timeout" if isinstance(exc, requests.Timeout) else "ollama_unavailable"
                    self.local_disabled = True
                self.local_reason = fallback_reason
                print(f"Ollama failed ({fallback_reason}); trying Gemini if available.", flush=True)
            else:
                self.local_failures = 0
                self.local_reason = None
                print(f"Vision: ollama / {config.OLLAMA_MODEL}", flush=True)
                return text, make_entry(text, "ollama", config.OLLAMA_MODEL)
        elif self.provider == "auto":
            fallback_reason = self.local_reason if self.local_disabled else "ollama_budget_exhausted"

        if not self.can_gemini() or time.monotonic() >= self.deadline:
            return None, None
        try:
            kwargs = {"deadline": self.deadline}
            if image_data is not None:
                kwargs["image_data"] = image_data
            text, attempts = describe_image(
                image_url, max_attempts=self.gemini_budget - self.gemini_requests, **kwargs
            )
        except ImageFetchError as exc:
            self.image_failed(exc)
            print(f"Photo download failed: {_sanitize(str(exc))}", flush=True)
            return f"{DESCRIPTION_UNAVAILABLE_PREFIX} {_sanitize(str(exc))})", None
        except VisionDeadlineExceeded as exc:
            self.gemini_requests += getattr(exc, "gemini_request_attempts", 0)
            self.stop_calls = True
            return None, None
        except QuotaExceededError as exc:
            self.gemini_requests += getattr(exc, "gemini_request_attempts", 1)
            self.image_failures = 0
            self.gemini_disabled = True
            print(f"{exc} -- Gemini disabled for this run; local vision can continue.", flush=True)
            return None, None
        except requests.RequestException as exc:
            self.gemini_requests += getattr(exc, "gemini_request_attempts", 1)
            self.image_failures = 0
            if _is_outage_signal(exc):
                self.transient_failures += 1
                self.non_retryable_failures = 0
                self.gemini_disabled = self.transient_failures >= MAX_CONSECUTIVE_TRANSIENT_FAILURES
            else:
                self.transient_failures = 0
                self.non_retryable_failures += 1
                self.gemini_disabled = self.non_retryable_failures >= MAX_CONSECUTIVE_NON_RETRYABLE_FAILURES
            print(f"Gemini request failed: {_sanitize(str(exc))}", flush=True)
            return f"{DESCRIPTION_UNAVAILABLE_PREFIX} {_sanitize(str(exc))})", None
        else:
            self.gemini_requests += attempts
            self.transient_failures = self.image_failures = self.non_retryable_failures = 0
            if not text or not text.strip():
                return None, None
            print(f"Vision: gemini / {config.GEMINI_MODEL}; fallback={fallback_reason or 'none'}", flush=True)
            return text, make_entry(text, "gemini", config.GEMINI_MODEL, fallback_reason)


def describe_images(
    media_items: list[dict],
    cache: dict,
    max_requests: int,
    on_progress=None,
    stop_calls: bool = False,
    consecutive_transient_failures: int = 0,
    consecutive_image_fetch_failures: int = 0,
    consecutive_non_retryable_failures: int = 0,
    attempted_media_ids: dict[str, str | None] | None = None,
    *,
    state: VisionRun | None = None,
) -> tuple[int, bool, int, int, int]:
    """Attach description strings while retaining provenance in the cache.

    The returned attempt count remains Gemini-only for existing callers.
    Pass one VisionRun across batches to retain independent provider state.
    """
    if state is None:
        state = VisionRun(
            gemini_budget=max_requests,
            stop_calls=stop_calls,
            transient_failures=consecutive_transient_failures,
            image_failures=consecutive_image_fetch_failures,
            non_retryable_failures=consecutive_non_retryable_failures,
        )
    before = state.gemini_requests
    if attempted_media_ids is None:
        attempted_media_ids = {}

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
                descriptions.append(description_text(cache[media_id]))
                state.cache_hits.add(media_id)
                continue
            if media_id in attempted_media_ids:
                descriptions.append(attempted_media_ids[media_id])
                continue
            if state.stopped():
                # Leave as None (pending) -- topic_graph.py distinguishes
                # this from "no photo" via the list entry existing at all.
                descriptions.append(None)
                state.pending.add(media_id)
                continue

            attempted_media_ids[media_id] = None
            before_photo = state.gemini_requests
            description, entry = state.describe(photo["media_url"])
            attempted_media_ids[media_id] = description
            if entry is not None:
                cache[media_id] = entry
            descriptions.append(description)
            if description is None:
                state.pending.add(media_id)
            if entry is not None and on_progress is not None:
                on_progress()
            if state.gemini_requests > before_photo:
                time.sleep(min(MIN_SECONDS_BETWEEN_CALLS, max(0, state.deadline - time.monotonic())))

        parsed = [split_watch_outs(description) for description in descriptions]
        item["photo_descriptions"] = [description for description, _ in parsed]
        watch_outs = [notes for _, notes in parsed]
        if any(watch_outs):
            item["watch_outs"] = watch_outs
        else:
            item.pop("watch_outs", None)

    return (
        state.gemini_requests - before,
        state.stopped(),
        state.transient_failures,
        state.image_failures,
        state.non_retryable_failures,
    )
