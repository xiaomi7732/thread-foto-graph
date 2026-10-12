# Thread Foto Graph

Builds a daily **topic graph** of your Threads (Meta) posts and replies from
the last 2 days, describing every photo in text (via Gemini Flash, free tier)
so **AI can read the compact text tree (`data/topics.md`) instead of
re-pulling raw images into its context on every response** — this is what was
burning AI's token budget.

On top of that, the output is **actionable-first, not just text-first**:
since Muse's job here is to watch threads and suggest replies to replies, we
only spell out replies you (`THREADS_USERNAME`) haven't responded to yet.
Branches you've already answered collapse to a one-line count (or disappear
entirely from the primary read) instead of being re-read in full every time
— the full conversation is never lost, it just moves to an on-demand archive
file (see below).

Outputs (smallest/most-frequent read first):
- `data/actions.md` — **flat action list, no topic clustering, no reply tree
  to walk.** Split into `## Needs Reply` (has a photo — worth a thoughtful
  written reply) and `## Needs Like` (text-only — a like is usually enough),
  with items grouped under a `### <thread>` header written **once per
  thread** (not once per reply, which would just repeat the same
  text/link over and over for a busy thread). Each reply bullet is otherwise
  self-contained: its own text/photo description plus a one-line note on
  what it's replying to if it's nested under another reply. **Point
  Muse/AI at this file for day-to-day use** — its size tracks the number of
  outstanding actions, not total conversation size, so it stays small even
  as threads accumulate hundreds of already-answered replies.
- `data/topics.md` — compact, **actionable-first** text tree of topics →
  threads → still-unanswered replies, with photo descriptions inline instead
  of images, `**[NEEDS REPLY]**`/`**[NEEDS LIKE]**` markers, real nesting
  preserved, and a permalink per reply for manual validation. Useful when
  thread/topic structure or reply hierarchy matters; otherwise `actions.md`
  is the smaller read.
- `data/threads/<thread_id>.md` — one file per root thread with the **full,
  unpruned** conversation (every reply, answered or not). Not loaded by
  default; fetch a specific one only if deeper context on that thread is
  needed.
- `docs/graph.json` + `docs/index.html` — visual topic graph (graph view +
  click-through detail panel with full text, replies, photo descriptions,
  permalinks, and `needs_response`/`action` flags per reply), published via
  GitHub Pages, for your own browsing (not for AI).

## How it works

1. Fetch your root posts from the last `LOOKBACK_DAYS` days via the Threads
   API, and the full reply tree for each (`src/threads_client.py`). The
   long-lived access token is auto-refreshed before it expires
   (`src/threads_auth.py`); auth is sent as an `Authorization: Bearer` header,
   never a query param, so it can't leak into logged URLs.
2. For every photo (root post or reply) not already described, call Gemini
   for a one-to-two-sentence description (`src/vision.py`). `CAROUSEL_ALBUM`
   items are expanded via the `children{media_type,media_url}` field
   (`src/threads_client.py`'s `photo_items()`) so **every photo in a
   multi-image carousel is described individually** — not just its cover
   image, which is all the API returns by default. The API key is sent as
   the `x-goog-api-key` header (not a query param) for the same reason.
   Descriptions are cached forever by each individual photo's media ID
   (`cache/media_cache.json`, local-only) so a photo is never re-described on
   later runs, and the cache is flushed to disk after *every* new
   description — an interrupted run never loses progress.
   - If Gemini's daily quota is hit mid-run, remaining photos are left
     un-described (not cached as a failure) and are clearly marked in the
     output as `(photo pending description - will be described on a future
     run)` — never silently indistinguishable from "no photo".
3. Cluster threads into topics by text similarity, prune each thread's reply
   tree down to what's actionable, and write `actions.md` + `topics.md` +
   the per-thread archives + `graph.json` (`src/topic_graph.py`). Reply/thread text is
   written in full (not truncated) wherever it's shown, since the whole
   point is to replace images, not compress text.

### Flat action list (what to point Muse at day-to-day)

`data/actions.md` is the leanest read: it skips topic clustering and the
reply tree entirely, listing only the replies that still need a response as
flat, self-contained bullets (own text + photo description + a one-line
"replying to" note if nested), grouped under one `### <thread>` header per
thread -- written once, not once per reply, so a busy thread's snippet/link
doesn't repeat for every one of its replies -- split into `## Needs Reply`
and `## Needs Like`. Its size tracks the *number of outstanding actions*, not
total conversation volume, so a 300-reply giveaway thread that's mostly
already-answered doesn't bloat it — only the genuinely unanswered items do.
`data/topics.md` keeps the fuller tree/topic view for when hierarchy matters.

### Actionable-first pruning (what keeps topics.md small)

A reply is considered **"needs a response"** if it's not your own reply
(`THREADS_USERNAME`) and you haven't directly replied to it yet (no direct
child reply authored by you). `src/topic_graph.py`'s `_prune_for_action()`
walks each thread's reply tree and:
- **Drops entirely** any branch where every reply in it is already resolved
  (your own replies, or replies you've already responded to, with nothing
  unanswered underneath) — these never get written out in `topics.md`.
- **Keeps** a reply if it itself still needs a response, marking it
  **`[NEEDS REPLY]`** if it has a photo (worth a thoughtful written reply) or
  **`[NEEDS LIKE]`** if it's text-only (a like is usually enough) --
  `_action_category()` makes that call. Ancestors of a kept reply are kept
  too (unmarked, as context) so Muse can see what it's replying within —
  already-answered siblings/descendants just collapse into a one-line
  `(+N more replies already answered under this, see archive)` note.
- A thread where every reply is already answered collapses to a single
  `All N replies already answered. ✅` line.

None of this loses data — the full, unpruned conversation for every thread is
always written to `data/threads/<thread_id>.md`, linked from `topics.md`.

**Caveat:** for a high-volume, non-conversational thread (e.g. a "post your
photo" giveaway with hundreds of one-off entries), most replies are
legitimately unanswered, so the actionable list can still be long — pruning
only removes what's *already resolved*, it doesn't cap or sample the backlog.

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
   - `THREADS_USERNAME` — your own Threads username, used to tell "already
     answered by me" replies apart from everyone else's (drives the
     actionable-first pruning in `actions.md`/`topics.md`).
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
   Check `data/actions.md`, `data/topics.md`, `data/threads/`, and
   `docs/graph.json` were created/updated.

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

## Running on a schedule (GitHub Actions)

Alternative to Task Scheduler if you'd rather not keep a machine running:
`.github/workflows/thread-foto-graph.yml` runs the same pipeline on GitHub's
hosted runners and commits/pushes any changes itself.

1. **Secrets** (Settings → Secrets and variables → Actions → **Secrets**
   tab) — anything that grants access to an account/API:
   - `THREADS_ACCESS_TOKEN` — your long-lived Threads token.
   - `GEMINI_API_KEY` — your Gemini API key.
2. **Variables** (same page, **Variables** tab instead) — plain config, not
   sensitive on its own:
   - `THREADS_USERNAME` (required — no default).
   - `GEMINI_MODEL`, `LOOKBACK_DAYS`, `MAX_ROOT_THREADS`,
     `MAX_VISION_REQUESTS_PER_RUN` — all optional, same defaults as
     `src/config.py` if left unset.
3. That's it — the workflow is already scheduled (`cron: "0 13 * * *"`, i.e.
   13:00 UTC daily; edit the cron line to change it) and also supports
   **Actions → Update Thread Topic Graph → Run workflow** for an on-demand
   run, optionally overriding `lookback_days`/`max_root_threads` for that
   run only (handy for testing without touching the repo variables).

Notes on how it differs from the Task Scheduler setup:
- `cache/*.json` (described-photo cache + the auto-refreshed Threads token)
  is gitignored and local-only by design, so on a hosted runner — which has
  no persistent disk between runs — it's restored/saved via `actions/cache`
  instead (keyed so the latest snapshot always carries forward to the next
  run). This cache can in principle be evicted by GitHub (e.g. if the
  workflow doesn't run for an extended period); if that happens, the run
  falls back to bootstrapping straight from the `THREADS_ACCESS_TOKEN`
  secret and re-describing any photos described since the last surviving
  cache snapshot (Gemini free-tier calls, not a correctness problem — see
  `src/vision.py`'s caching).
- The job authenticates its `git push` using the automatically-provided
  `GITHUB_TOKEN` (via `permissions: contents: write`), not your personal
  git credentials.

## Publishing to GitHub Pages

1. On GitHub: **Settings → Pages**.
2. Source: **Deploy from a branch**, branch `main`, folder `/docs`.
3. Save. Your graph will be viewable at
   `https://xiaomi7732.github.io/thread-foto-graph/` after the next push.

## Pointing AI at the text tree

Since the repo is public, give AI the raw URL so it reads text only:
```
https://raw.githubusercontent.com/xiaomi7732/thread-foto-graph/main/data/actions.md
```
This is the leanest, flat action list — just what still needs a reply or a
like, each self-contained. Use `data/topics.md` (same raw-URL pattern) if
thread/topic structure matters, or drill into a specific
`data/threads/<thread_id>.md` if full conversation history on a thread is
needed.

## Notes

- `cache/media_cache.json` is local-only (gitignored) — it exists purely to
  avoid re-describing the same photo across runs on this machine. Nothing in
  this project ever deletes or clears it automatically; it only grows.
- `data/threads/<thread_id>.md` archive files accumulate similarly to the
  cache — nothing deletes them once a thread rolls out of the
  `LOOKBACK_DAYS` window, even though `actions.md`/`topics.md` stop
  mentioning it. This is intentional (so old archive links never go stale),
  but means the `data/threads/` folder only grows over time.
- Both the Threads token and the Gemini key are sent as request **headers**,
  never query params, so they can never end up embedded in a logged/committed
  error message (this previously caused a real leaked-key incident caught by
  GitHub's secret-scanning push protection — rotate immediately if it ever
  recurs).
- The Threads API and Gemini API surfaces can change; if a run starts failing
  with field/endpoint errors, check the current docs:
  - https://developers.facebook.com/docs/threads
  - https://ai.google.dev/gemini-api/docs
