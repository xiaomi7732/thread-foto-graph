# Thread Foto Graph

Builds a daily **topic graph** of your Threads (Meta) posts and replies from
the last 2 days, describing every photo in text (via Gemini Flash, free tier)
so **AI can read the compact text tree (`data/topics.md`) instead of
re-pulling raw images into its context on every response** — this is what was
burning AI's token budget.

Outputs:
- `data/topics.md` — compact text tree of topics → threads → replies, with
  photo descriptions inline instead of images, plus a permalink per reply for
  manual validation. Point AI at this file.
- `docs/graph.json` + `docs/index.html` — visual topic graph (graph view +
  click-through detail panel with full text, replies, photo descriptions and
  permalinks), published via GitHub Pages, for your own browsing (not for AI).

## How it works

1. Fetch your root posts from the last `LOOKBACK_DAYS` days via the Threads
   API, and the full reply tree for each (`src/threads_client.py`). The
   long-lived access token is auto-refreshed before it expires
   (`src/threads_auth.py`); auth is sent as an `Authorization: Bearer` header,
   never a query param, so it can't leak into logged URLs.
2. For every photo (root post or reply, including `CAROUSEL_ALBUM` covers) not
   already described, call Gemini for a one-to-two-sentence description
   (`src/vision.py`). The API key is sent as the `x-goog-api-key` header (not
   a query param) for the same reason. Descriptions are cached forever by
   media ID (`cache/media_cache.json`, local-only) so a photo is never
   re-described on later runs, and the cache is flushed to disk after *every*
   new description — an interrupted run never loses progress.
   - If Gemini's daily quota is hit mid-run, remaining photos are left
     un-described (not cached as a failure) and are clearly marked in the
     output as `(photo pending description - will be described on a future
     run)` — never silently indistinguishable from "no photo".
3. Cluster threads into topics by text similarity and write `topics.md` +
   `graph.json` (`src/topic_graph.py`). Reply/thread text is written in full
   (not truncated) in `topics.md` since it's meant to replace images, not
   compress text.

### Handling spiky days (free-tier safety net)

Gemini's free tier is **request-count limited per model, not $ cost** — and
varies a lot by model:
- `gemini-3.8-flash` (full Flash): ~20 requests/day
- `gemini-3.5-flash-lite` (what this project uses): ~500 requests/day

Flash-Lite is used here specifically because image description is a simple
task that doesn't need full Flash's reasoning, and 500/day comfortably covers
busy days. On days that still exceed the cap, the run stops at
`MAX_VISION_REQUESTS_PER_RUN` and leaves the rest uncached — they're picked up
automatically on the next scheduled run, since the cache persists across runs.
Gemini model availability/names change over time (several older model names
have been retired or renamed) — if calls start failing with 404, check
https://ai.google.dev/gemini-api/docs/models for current model IDs.

## Setup

1. **Python**: create a virtual environment and install dependencies.
   ```powershell
   cd C:\Users\xiaom\Repos\saars.foto.thread.graph
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
2. **Credentials**: copy `.env.example` to `.env` and fill in:
   - `THREADS_ACCESS_TOKEN` — a long-lived Threads API user access token for
     the account to watch (see "Obtaining a long-lived Threads token" below).
     It's auto-refreshed on subsequent runs, so you only need to set this once.
   - `GEMINI_API_KEY` — a free Google AI Studio API key
     (https://aistudio.google.com/apikey).
   - `GEMINI_MODEL` — defaults to `gemini-3.5-flash-lite` (500 free
     requests/day); see the rate-limit note above before changing it.
3. **Test it manually** (optionally scope to a couple of posts first with
   `MAX_ROOT_THREADS`, handy while validating setup):
   ```powershell
   $env:MAX_ROOT_THREADS="2"
   .venv\Scripts\python.exe -u -m src.main
   ```
   Check `data/topics.md` and `docs/graph.json` were created/updated.

## Obtaining a long-lived Threads token

1. Create a Meta app with the Threads API product, with your account added as
   a tester/developer.
2. Authorize it (`https://threads.net/oauth/authorize?...`) to get a
   short-lived code, exchange it for a short-lived token, then exchange that
   for a **long-lived token** (60 days) via
   `GET /access_token?grant_type=th_exchange_token&client_secret=...` — this
   one step needs your app secret, and only needs to be done once.
3. Paste the long-lived token into `.env` as `THREADS_ACCESS_TOKEN`.
   `src/threads_auth.py` automatically refreshes it (via
   `grant_type=th_refresh_token`, no app secret needed) before it expires on
   every subsequent run, so you shouldn't need to repeat this.

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

## Pointing AI at the text tree

Since the repo is public, give AI the raw URL so it reads text only:
```
https://raw.githubusercontent.com/xiaomi7732/thread-foto-graph/main/data/topics.md
```

## Notes

- `cache/media_cache.json` is local-only (gitignored) — it exists purely to
  avoid re-describing the same photo across runs on this machine. Nothing in
  this project ever deletes or clears it automatically; it only grows.
- Both the Threads token and the Gemini key are sent as request **headers**,
  never query params, so they can never end up embedded in a logged/committed
  error message (this previously caused a real leaked-key incident caught by
  GitHub's secret-scanning push protection — rotate immediately if it ever
  recurs).
- The Threads API and Gemini API surfaces can change; if a run starts failing
  with field/endpoint errors, check the current docs:
  - https://developers.facebook.com/docs/threads
  - https://ai.google.dev/gemini-api/docs
