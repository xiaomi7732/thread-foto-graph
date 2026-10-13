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

    scope = (
        f"last {config.LOOKBACK_DAYS} day(s)"
        if config.MAX_ROOT_THREADS is None
        else f"last {config.LOOKBACK_DAYS} day(s), capped to {config.MAX_ROOT_THREADS} post(s)"
    )
    print(f"Fetching threads ({scope})...", flush=True)
    threads = threads_client.collect_threads_with_replies(
        config.LOOKBACK_DAYS, config.MAX_ROOT_THREADS
    )
    print(f"Found {len(threads)} root thread(s).", flush=True)

    cache = load_cache()
    requests_made = 0
    budget = config.MAX_VISION_REQUESTS_PER_RUN
    # Once True (a 429 with no retry-after, or a sustained Gemini outage --
    # see vision.MAX_CONSECUTIVE_TRANSIENT_FAILURES), no further batch below
    # should attempt new Gemini calls either. describe_images() returns this
    # back to us each time so it carries across every thread's root-photo
    # and reply batches, not just the one batch it was first detected in.
    stop_calls = False

    def persist_progress():
        # Flush to disk after every single new description, so a slow or
        # interrupted run never has to re-describe an already-processed photo.
        save_cache(cache)

    for i, thread in enumerate(threads, start=1):
        # Root post photo (vision.describe_images expects a list of media items)
        made, stop_calls = vision.describe_images(
            [thread], cache, budget - requests_made, persist_progress, stop_calls
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
        made, stop_calls = vision.describe_images(
            all_replies, cache, budget - requests_made, persist_progress, stop_calls
        )
        requests_made += made
        print(
            f"[{i}/{len(threads)}] thread {thread['id']}: "
            f"{len(all_replies)} replies processed "
            f"({requests_made} vision requests so far)",
            flush=True,
        )
        if requests_made >= budget:
            print(
                "Vision request budget exhausted for this run; remaining photos "
                "will be described on the next scheduled run.",
                flush=True,
            )
            break
        if stop_calls:
            print(
                "Stopping new Gemini requests for this run (quota hit or a "
                "sustained outage); remaining threads' photos are left pending "
                "and will be retried on the next scheduled run.",
                flush=True,
            )
            break

    save_cache(cache)
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
