"""Daily entry point: fetch threads -> describe new photos -> build topic graph.

Intended to run on a schedule (see README for Task Scheduler setup). After
this script finishes, the caller (scheduled wrapper) commits & pushes
data/needs-reply.md, data/needs-like.md, data/actions.md, data/topics.md and
docs/graph.json so GitHub Pages and Muse
can read them.
"""
from __future__ import annotations

import datetime as dt
import sys

from . import config, threads_client, topic_graph, vision
from .cache import load_cache, save_cache


def main() -> int:
    config.require("THREADS_ACCESS_TOKEN", config.THREADS_ACCESS_TOKEN)
    config.require("THREADS_USERNAME", config.THREADS_USERNAME)
    config.require("GEMINI_API_KEY", config.GEMINI_API_KEY)

    cache = load_cache()
    requests_made = 0
    budget = config.MAX_VISION_REQUESTS_PER_RUN
    # Once True (a 429 with no retry-after, a sustained Gemini outage -- see
    # vision.MAX_CONSECUTIVE_TRANSIENT_FAILURES, a sustained image-host
    # outage -- see vision.MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES, or a
    # sustained non-retryable Gemini error e.g. a revoked API key -- see
    # vision.MAX_CONSECUTIVE_NON_RETRYABLE_FAILURES), no further batch below
    # should attempt new calls either. consecutive_failures,
    # consecutive_image_fetch_failures, and consecutive_non_retryable_failures
    # are the running failure streaks (see vision._is_outage_signal) across
    # batches -- main.py's root-photo batches are usually just one photo
    # each, so the streaks must be threaded across calls or any circuit
    # breaker would almost never see enough failures in a row to trip.
    # describe_images() returns all four back to us each time so they carry
    # across every thread's root-photo and reply batches, not just the one
    # batch either was first detected in.
    stop_calls = False
    consecutive_failures = 0
    consecutive_image_fetch_failures = 0
    consecutive_non_retryable_failures = 0
    budget_exhausted_announced = False
    seen_uncached_photo_ids = set()

    scope = (
        f"last {config.LOOKBACK_DAYS} day(s)"
        if config.MAX_ROOT_THREADS is None
        else f"last {config.LOOKBACK_DAYS} day(s), capped to {config.MAX_ROOT_THREADS} post(s)"
    )

    for iteration in range(1, config.MAX_ITERATIONS + 1):
        cache_at_iteration_start = dict(cache)
        new_recognition_needed = False

        def find_new_uncached_photos(items):
            nonlocal new_recognition_needed
            for item in items:
                for photo in threads_client.photo_items(item):
                    media_id = photo["id"]
                    if media_id not in cache and media_id not in seen_uncached_photo_ids:
                        seen_uncached_photo_ids.add(media_id)
                        new_recognition_needed = True

        print(
            f"Iteration {iteration}/{config.MAX_ITERATIONS}: fetching threads ({scope})...",
            flush=True,
        )
        threads = threads_client.collect_threads_with_replies(
            config.LOOKBACK_DAYS, config.MAX_ROOT_THREADS
        )
        print(f"Found {len(threads)} root thread(s).", flush=True)

        def persist_progress():
            # Flush to disk after every single new description, so a slow or
            # interrupted run never has to re-describe an already-processed photo.
            save_cache(cache)

        for i, thread in enumerate(threads, start=1):
            # Root post photo (vision.describe_images expects a list of media items)
            find_new_uncached_photos([thread])
            (
                made,
                stop_calls,
                consecutive_failures,
                consecutive_image_fetch_failures,
                consecutive_non_retryable_failures,
            ) = vision.describe_images(
                [thread],
                cache,
                budget - requests_made,
                persist_progress,
                stop_calls,
                consecutive_failures,
                consecutive_image_fetch_failures,
                consecutive_non_retryable_failures,
            )
            requests_made += made
            # Walk the full nested reply tree (not just top-level replies) so
            # every reply-to-a-reply photo gets described too. Newest-first, so
            # a budget/quota-limited run spends its vision requests on the most
            # recent activity (what's actually actionable soon) instead of
            # working through the backlog in arrival order and never reaching
            # the latest replies. Parsed to real datetimes (not raw string
            # comparison) since ISO 8601 strings don't sort correctly across
            # differing UTC offsets.
            def _reply_sort_key(reply: dict) -> dt.datetime:
                raw = reply.get("timestamp")
                if not raw:
                    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
                try:
                    return threads_client.parse_timestamp(raw)
                except ValueError:
                    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

            all_replies = sorted(
                threads_client.iter_replies(thread.get("replies", [])),
                key=_reply_sort_key,
                reverse=True,
            )
            find_new_uncached_photos(all_replies)
            (
                made,
                stop_calls,
                consecutive_failures,
                consecutive_image_fetch_failures,
                consecutive_non_retryable_failures,
            ) = vision.describe_images(
                all_replies,
                cache,
                budget - requests_made,
                persist_progress,
                stop_calls,
                consecutive_failures,
                consecutive_image_fetch_failures,
                consecutive_non_retryable_failures,
            )
            requests_made += made
            print(
                f"[{i}/{len(threads)}] thread {thread['id']}: "
                f"{len(all_replies)} replies processed "
                f"({requests_made} vision requests so far)",
                flush=True,
            )
            if requests_made >= budget and not budget_exhausted_announced:
                print(
                    "Vision request budget exhausted for this run; remaining photos "
                    "will be described on the next scheduled run.",
                    flush=True,
                )
                budget_exhausted_announced = True
            # Deliberately do NOT break when the budget is exhausted or
            # stop_calls is True: describe_images() must still be called for
            # every remaining thread so photo_descriptions gets set (even if
            # only to pending placeholders) -- otherwise topic_graph.py would
            # see a *missing* key for unvisited threads (which it treats as "no
            # photos at all", not "pending"), silently dropping their photos
            # and misfiling photo replies into needs-like.md. Once either
            # condition holds, each remaining describe_images() call is a cheap
            # no-network walk (see vision.py), so looping on is fine.

        save_cache(cache)
        if cache != cache_at_iteration_start:
            new_recognition_needed = True
        if not new_recognition_needed:
            print("No new image recognition needed; stopping.", flush=True)
            break
        if iteration < config.MAX_ITERATIONS:
            print("New image recognition work found; fetching again for new photos.", flush=True)
        else:
            print(
                f"Reached the {config.MAX_ITERATIONS}-iteration limit; "
                "remaining new photos will be picked up on the next run.",
                flush=True,
            )

    print(f"Made {requests_made} new vision request(s) this run.", flush=True)

    graph = topic_graph.build_topic_graph(threads)
    print(
        f"Wrote data/needs-reply.md, data/needs-like.md, data/actions.md, "
        f"data/topics.md, data/history/ and docs/graph.json "
        f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
