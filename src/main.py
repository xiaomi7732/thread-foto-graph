"""Daily entry point: fetch threads -> describe new photos -> build topic graph.

Intended to run on a schedule (see README for Task Scheduler setup). After
this script finishes, the caller (scheduled wrapper) commits & pushes
data/topics.md and docs/graph.json so GitHub Pages and Muse can read them.
"""
from __future__ import annotations

import sys

from . import config, threads_client, topic_graph, vision
from .cache import load_cache, save_cache


def main() -> int:
    config.require("THREADS_ACCESS_TOKEN", config.THREADS_ACCESS_TOKEN)
    config.require("GEMINI_API_KEY", config.GEMINI_API_KEY)

    print(f"Fetching threads from the last {config.LOOKBACK_DAYS} day(s)...")
    threads = threads_client.collect_threads_with_replies(config.LOOKBACK_DAYS)
    print(f"Found {len(threads)} root thread(s).")

    cache = load_cache()
    requests_made = 0
    budget = config.MAX_VISION_REQUESTS_PER_RUN

    for thread in threads:
        # Root post photo (vision.describe_images expects a list of media items)
        made = vision.describe_images([thread], cache, budget - requests_made)
        requests_made += made
        made = vision.describe_images(thread.get("replies", []), cache, budget - requests_made)
        requests_made += made
        if requests_made >= budget:
            print("Vision request budget exhausted for this run; remaining photos "
                  "will be described on the next scheduled run.")
            break

    save_cache(cache)
    print(f"Made {requests_made} new vision request(s) this run.")

    graph = topic_graph.build_topic_graph(threads)
    print(f"Wrote data/topics.md and docs/graph.json "
          f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
