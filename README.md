# Thread Foto Graph

Builds a daily **topic graph** of your Threads (Meta) posts and replies from
the last 2 days, describing every photo in text (via Gemini Flash, free tier)
so **Muse can read the compact text tree (`data/topics.md`) instead of
re-pulling raw images into its context on every response** — this is what was
burning Muse's token budget.

Outputs:
- `data/topics.md` — compact text tree of topics → threads → replies, with
  photo descriptions inline instead of images. Point Muse at this file.
- `docs/graph.json` + `docs/index.html` — visual topic graph, published via
  GitHub Pages, for your own browsing (not for Muse).

## How it works

1. Fetch your root posts from the last `LOOKBACK_DAYS` days via the Threads
   API, and the full reply tree for each (`src/threads_client.py`).
2. For every photo (root post or reply) not already described, call Gemini
   Flash for a one-to-two-sentence description (`src/vision.py`). Descriptions
   are cached forever by media ID (`cache/media_cache.json`, local-only) so
   a photo is never re-described on later runs.
3. Cluster threads into topics by text similarity and write `topics.md` +
   `graph.json` (`src/topic_graph.py`).

### Handling spiky days (free-tier safety net)

Gemini Flash's free tier is ~1,500 requests/day. On days with 1,000+ reply
photos across all threads, the run caps itself at `MAX_VISION_REQUESTS_PER_RUN`
and leaves the rest uncached — they're picked up automatically on the next
scheduled run, since the cache persists across runs.

## Setup

1. **Python**: create a virtual environment and install dependencies.
   ```powershell
   cd C:\Users\xiaom\Repos\saars.foto.thread.graph
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
2. **Credentials**: copy `.env.example` to `.env` and fill in:
   - `THREADS_ACCESS_TOKEN` — a long-lived Threads API user access token for
     the account to watch.
   - `GEMINI_API_KEY` — a free Google AI Studio API key
     (https://aistudio.google.com/apikey).
3. **Test it manually**:
   ```powershell
   .venv\Scripts\python.exe -m src.main
   ```
   Check `data/topics.md` and `docs/graph.json` were created/updated.

## Running on a schedule (Windows Task Scheduler)

`run_daily.ps1` runs the pipeline and commits/pushes any changes.

1. Open Task Scheduler → **Create Task…**
2. **General**: name it `Thread Foto Graph`, run whether user is logged on or not.
3. **Triggers**: New → Daily, pick a time (e.g., 6:00 AM).
4. **Actions**: New →
   - Program/script: `powershell.exe`
   - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\xiaom\Repos\saars.foto.thread.graph\run_daily.ps1"`
5. Save. Ensure your git credentials for this repo are cached (e.g., via the
   Git Credential Manager) so `git push` in the task doesn't prompt for a
   password.

## Publishing to GitHub Pages

1. On GitHub: **Settings → Pages**.
2. Source: **Deploy from a branch**, branch `main`, folder `/docs`.
3. Save. Your graph will be viewable at
   `https://xiaomi7732.github.io/thread-foto-graph/` after the next push.

## Pointing Muse at the text tree

Since the repo is public, give Muse the raw URL so it reads text only:
```
https://raw.githubusercontent.com/xiaomi7732/thread-foto-graph/main/data/topics.md
```

## Notes

- `cache/media_cache.json` is local-only (gitignored) — it exists purely to
  avoid re-describing the same photo across runs on this machine.
- The Threads API and Gemini API surfaces can change; if a run starts failing
  with field/endpoint errors, check the current docs:
  - https://developers.facebook.com/docs/threads
  - https://ai.google.dev/gemini-api/docs
