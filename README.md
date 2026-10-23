# Thread Foto Graph

Builds a daily **topic graph** of your Threads (Meta) posts and replies from
the last `LOOKBACK_DAYS` days (4 by default), describing every photo in text (via Gemini Flash, free tier)
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
- `data/needs-reply.md` — **flat list of photo replies that still need a
  reply**, no topic clustering, no reply tree to walk, items grouped under a
  `### <thread>` header written **once per thread**. This carries the bulk
  of the data (full photo descriptions), since a thoughtful reply usually
  needs to reference what's in the photo.
- `data/needs-like.md` — **flat list of text-only replies that still need a
  like.** No photo descriptions to carry, so this file stays small (in
  practice roughly a fifth the size of `needs-reply.md`) — point a
  like-only worker/cron at this file specifically rather than the combined
  `actions.md`, so it isn't paying to load reply-drafting content it never
  uses.
- `data/actions.md` — **combined convenience copy of the two files above**,
  for a human skimming everything in one place. Each reply bullet is
  otherwise self-contained: its own text/photo description plus a one-line
  note on what it's replying to if it's nested under another reply. **Point
  Muse/AI at `needs-reply.md`/`needs-like.md` directly for day-to-day,
  per-worker use** — their combined size tracks the number of outstanding
  actions, not total conversation size, so they stay small even as threads
  accumulate hundreds of already-answered replies.
- `data/topics.md` — compact, **actionable-first** text tree of topics →
  threads → still-unanswered replies, with photo descriptions inline instead
  of images, `**[NEEDS REPLY]**`/`**[NEEDS LIKE]**` markers, real nesting
  preserved, and a permalink per reply for manual validation. Useful when
  thread/topic structure or reply hierarchy matters; otherwise
  `needs-reply.md`/`needs-like.md` is the smaller read.
- `data/threads/<thread_id>.md` — one file per root thread with the **full,
  unpruned** conversation (every reply, answered or not). Not loaded by
  default; fetch a specific one only if deeper context on that thread is
  needed.
- `data/history/needs-reply-<yyyy-mm-dd>.md` /
  `data/history/needs-like-<yyyy-mm-dd>.md` — a dated snapshot of each
  day's `needs-reply.md`/`needs-like.md`, written every run and overwritten
  through the day until the date rolls over (dated by Pacific local time, not
  UTC, so the calendar day matches the local day), at which point that day's
  file becomes a frozen historical record. Not for day-to-day reads (point at
  the live files for that) — useful for "what was still outstanding on a given day"
  drill-down.
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
   - On a 429 (rate limit), a single photo is retried in-process up to twice
     — but only when Gemini actually tells us how long to wait, via its
     `Retry-After` header or `retryDelay` body field. If neither is present,
     there's nothing real to honor, so it fails fast immediately instead of
     guessing a delay (an earlier version fell back to a fixed ~20s wait,
     but a blind guess just burns run time without reliably succeeding).
     If it's still rate-limited after an honored wait, or the daily quota
     is hit, remaining photos are left un-described (not cached as a
     failure) and are clearly marked in the output as `(photo pending
     description - will be described on a future run)` — never silently
     indistinguishable from "no photo". A quota/rate-limit hit is also
     logged (`Gemini free-tier daily/minute quota exceeded -- remaining
     photos in this batch left pending.`) so it's visible in the run log,
     not just inferable from pending markers after the fact.
   - On a 5xx (the model itself briefly overloaded -- Google's error body
     literally says "This model is currently experiencing high demand",
     common on the free-tier "lite" model) or a read timeout, a single photo
     is retried in-process up to 3 times with an **exponential backoff**
     (10s, 20s, 40s). A live A/B test confirmed this is the right fix:
     hammering the API ~5s apart mostly got 503s, while the same model
     spaced ~15s apart succeeded every time — so waiting longer on retry,
     not switching models, clears it. If it's still failing after exhausting
     retries, the photo is recorded as `(description unavailable: ...)`
     without being cached, so it's retried fresh on the next run, and the
     failure is printed to the run log for visibility.
   - **Trust boundary:** a Gemini description replaces your own eyes on a
     photo, which is fine for low-stakes drafting (a playful reply), but
     treat it as a draft input, not ground truth, before anything
     public-facing that depends on getting photo content exactly right
     (e.g. crediting/describing a feature carousel or an IG roundup) — spot
     check the actual photo first.
3. Cluster threads into topics by text similarity, prune each thread's reply
   tree down to what's actionable, and write `needs-reply.md` +
   `needs-like.md` + `actions.md` + `topics.md` +
   the per-thread archives + `graph.json` (`src/topic_graph.py`). Reply/thread text is
   written in full (not truncated) wherever it's shown, since the whole
   point is to replace images, not compress text.

### Flat action lists (what to point Muse at day-to-day)

`data/needs-reply.md` and `data/needs-like.md` are the leanest reads: they
skip topic clustering and the reply tree entirely, listing only the replies
that still need a response as flat, self-contained bullets (own text + photo
description + a one-line "replying to" note if nested), grouped under one
`### <thread>` header per thread -- written once, not once per reply, so a
busy thread's snippet/link doesn't repeat for every one of its replies.
They're split into two separate files (rather than one combined file)
because they're asymmetric in size and consumed differently: `needs-reply.md`
carries full photo descriptions (in practice ~80% of the combined content),
while `needs-like.md` is just one-liners (text + link). A worker that only
decides likes (e.g. an hourly like-browsing cron) should read
`needs-like.md` directly -- it has no use for the much bigger reply-drafting
content and shouldn't pay to load it on every run. `data/actions.md` is kept
as a combined convenience copy of both, for a human skimming everything at
once. Their combined size tracks the *number of outstanding actions*, not
total conversation volume, so a 300-reply giveaway thread that's mostly
already-answered doesn't bloat them — only the genuinely unanswered items do.
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
automatically on the next run, since the cache persists across runs.
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
     actionable-first pruning in `needs-reply.md`/`needs-like.md`/`topics.md`).
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
   Check `data/needs-reply.md`, `data/needs-like.md`, `data/actions.md`,
   `data/topics.md`, `data/threads/`, `data/history/`, and `docs/graph.json`
   were created/updated.

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

## Running on demand (GitHub Actions)

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
     `MAX_VISION_REQUESTS_PER_RUN`, `HARD_CUTOFF_DATE` — all optional, same
     defaults as `src/config.py` if left unset.
3. Start a run with **Actions → Update Thread Topic Graph → Run workflow**.
   You can optionally override `lookback_days`/`max_root_threads`/
   `hard_cutoff_date` for that run only (handy for testing, or for pushing
   out a new cutoff immediately, without touching the repo variables).

The workflow runs only when manually triggered; it has no scheduled trigger.
Its `concurrency:` group prevents overlapping runs from racing to commit and
push changes.

Notes on how it differs from the Task Scheduler setup:
- `cache/*.json` (described-photo cache + the auto-refreshed Threads token)
  is gitignored and local-only by design, so on a hosted runner — which has
  no persistent disk between runs — it's restored/saved via
  [`actions/cache`](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
  instead: it's a key-value blob store scoped to this repo (separate from
  git entirely), keyed so the latest snapshot always carries forward to the
  next run (`key` includes the unique run ID so a save never no-ops;
  `restore-keys` is a prefix match so the most recent entry is always
  restored). GitHub evicts a cache entry if it goes **7 days unused**, or
  under **least-recently-used** pressure once the repo's cache storage
  exceeds **10 GB** (the cached files are only a couple KB). If an entry is ever
  evicted anyway, the run falls back to bootstrapping straight from the
  `THREADS_ACCESS_TOKEN` secret and re-describing any photos described
  since the last surviving cache snapshot (Gemini free-tier calls, not a
  correctness problem — see `src/vision.py`'s caching).
- **Security note:** GitHub's cache storage isn't meant for secrets —
  anyone who can trigger a `pull_request`-triggered workflow against this
  repo could in principle read cache contents, including the auto-refreshed
  Threads token in `cache/token_store.json`. This workflow only triggers on
  `workflow_dispatch` (never `pull_request`), so that avenue
  isn't currently exposed — but avoid adding a `pull_request` trigger to
  this workflow (or any workflow that restores this cache key) without
  reconsidering this. Excluding `token_store.json` from the cached path
  would close the gap entirely, at the cost of breaking the "token renews
  itself forever" design in `src/threads_auth.py` (every run would treat
  the `THREADS_ACCESS_TOKEN` secret as freshly issued instead of actually
  refreshing it, so you'd need to manually replace that secret before it
  expires, roughly every 60 days).
- **Concurrent check-ins:** the `concurrency:` block only serializes other
  runs *of this workflow* -- it does nothing to stop a push from outside
  Actions (e.g. a local `git push` to `main`, or another workflow) while a
  run is mid-flight. A long run (uncapped `lookback_days`/`max_root_threads`
  describing many new photos can take 20+ minutes) checks out `main` once
  at the start, so if `main` moves before that run's own push at the end,
  git rejects it as a non-fast-forward (`! [rejected] main -> main (fetch
  first)`) -- this happened for real in
  [run 38326570220](https://github.com/xiaomi7732/thread-foto-graph/actions/runs/38326570220/job/115066509428),
  where a local push landed while the job was busy. The "Commit and push
  changes" step now retries: on a rejected push it `git fetch`es and
  `git rebase`s onto the latest `main`, then pushes again (up to 5
  attempts with a short backoff). This is safe because these commits only
  ever touch generated data files, so replaying ours on top of someone
  else's newer commit is just "last writer wins" with no real merge
  conflict expected. If you're pushing to `main` by hand, you can otherwise
  just avoid doing so while a run shows as in-progress in the Actions tab.
- **A failed push does not mean the new vision descriptions are wasted --
  but only because of a deliberate workaround.** `actions/cache`'s
  cache-save only happens in a "Post" cleanup hook that runs after all
  regular steps -- and GitHub Actions skips Post hooks for earlier steps
  if a *later* regular step fails outright. Concretely, in
  [run 38342491737](https://github.com/xiaomi7732/thread-foto-graph/actions/runs/38342491737/job/115116392611)
  the pipeline made 186 new Gemini vision calls, then "Commit and push
  changes" failed (the non-fast-forward race above) -- and because that
  step failed without `continue-on-error`, the cache's save-on-exit hook
  was skipped entirely (shows as "skipped" in the job's step list), so all
  186 newly-described photos were lost at the end of the run and had to be
  re-described (burning Gemini quota again) on the next one. The fix:
  "Commit and push changes" now has `continue-on-error: true`, and a final
  "Fail job if commit/push ultimately failed" step explicitly fails the
  job afterwards if needed -- this lets every step's cleanup Post hook
  (including the cache save) run normally, while still surfacing a genuine
  push failure as a failed run in the Actions UI.
- The job authenticates its `git push` using the automatically-provided
  `GITHUB_TOKEN` (via `permissions: contents: write`), not your personal
  git credentials.

## Publishing to GitHub Pages

1. On GitHub: **Settings → Pages**.
2. Source: **Deploy from a branch**, branch `main`, folder `/docs`.
3. Save. Your graph will be viewable at
   `https://xiaomi7732.github.io/thread-foto-graph/` after the next push.

### Reply image thumbnail gallery

The separate **Generate Reply Image Gallery** action runs after a successful
**Update Thread Topic Graph** run on `main`, or manually from the Actions tab.
It uses exactly the root threads in the latest `docs/graph.json` and fetches
fresh reply media URLs, without calling Gemini or changing the topic graph.
It includes **all visible reply images** (answered or not, including nested
replies and every image in a carousel), but not root-post photos, videos, or
hidden reply branches.

After the workflow is pushed to `main` and run, use the gallery link in the
run summary (also linked from the graph). With Pages publishing `/docs`,
the gallery is at `https://xiaomi7732.github.io/thread-foto-graph/reply-images.html`;
with the repository-root Pages source, it is under `/docs/reply-images.html`.
The downloadable Markdown is hosted as `reply-images.txt` (saved as
`reply-images.md` by the download button) so a repository-root Jekyll build
serves it unchanged without conflicting with the HTML gallery. `docs/.nojekyll`
also supports publishing `/docs` directly. The existing repository-root Jekyll
site is left unchanged. Thumbnails are grouped by topic and thread;
each has direct **Reply** and **Thread** links. Use **Copy Markdown** for a
linked thumbnail, **Copy reply link**, or the page-wide copy buttons. Copying
Markdown copies the embed text, not image files. If the browser blocks clipboard
access, a selected text box lets you copy manually.

Click **Favorite** on individual images, then **Show favorites** to review only
your picks. **Copy favorite reply links** copies one link per selected reply
(deduplicated when several selected photos belong to the same reply).
**Reset favorites** clears all saved picks. Selections are saved in this
browser's local storage, so refreshing or reopening the same gallery keeps
them. Stable photo IDs retain picks when the gallery refreshes image URLs.
Saved picks outside the current gallery are retained and counted separately;
only currently displayed picks contribute to copied reply links.
Selections are not uploaded to the repository or synced across browsers,
devices, or different origins (local files and GitHub Pages are separate).
Updates from multiple gallery tabs are serialized with Web Locks and applied
to the latest stored picks, so one tab cannot overwrite another tab's
unrelated picks or restore selections cleared by Reset. Browsers without
Web Locks support (or a secure context such as HTTPS or localhost) do not
write favorites; they show an explicit page-only warning instead.
Clearing site data or private browsing can remove them. If browser storage
is blocked, full, or invalid, the gallery explicitly warns that persistence
is unavailable; picking and copying still work for the current page.
Failed local edits remain selected and are retried as individual changes on
the next edit or Reset, rather than silently disappearing after storage recovers.

The workflow commits `data/reply-images.md` plus
`docs/reply-images.html` / `docs/reply-images.txt`, and uploads a
`reply-image-gallery` artifact. The run summary links to the published page
and Markdown. It reuses the existing `THREADS_ACCESS_TOKEN` and `GH_PATH`
secrets, writer concurrency group, and token/vision cache. No additional
secrets or Pages configuration are needed.

Graph and gallery runs share a queued writer lock so pending updates are not
replaced by gallery runs. If an external push changes `docs/graph.json` during
generation, the gallery workflow regenerates against the new topics before
retrying publication.

Threads' image URLs expire; the gallery deliberately uses remote images rather
than archiving other people's photos. Rerun the action to refresh expired
thumbnails. The gallery and Markdown are public, like the existing graph.
To generate locally using the configured Threads token:

```powershell
.venv\Scripts\python.exe -m src.reply_gallery
```

## Pointing AI at the text tree

Since the repo is public, give AI the raw URL so it reads text only. Point a
reply-drafting worker at the (larger) reply list:
```
https://raw.githubusercontent.com/xiaomi7732/thread-foto-graph/main/data/needs-reply.md
```
Point a like-only worker at the (much smaller) like list instead, so it
isn't paying to load reply-drafting content it never uses:
```
https://raw.githubusercontent.com/xiaomi7732/thread-foto-graph/main/data/needs-like.md
```
This is the leanest, flat action list — just what still needs a reply or a
like, each self-contained. `data/actions.md` (same raw-URL pattern) is a
combined copy of both, handy for a human skimming everything at once. Use
`data/topics.md` if thread/topic structure matters, or drill into a specific
`data/threads/<thread_id>.md` if full conversation history on a thread is
needed.

## Notes

- `cache/media_cache.json` is local-only (gitignored) — it exists purely to
  avoid re-describing the same photo across runs on this machine. Nothing in
  this project ever deletes or clears it automatically; it only grows.
- `data/threads/<thread_id>.md` archive files accumulate similarly to the
  cache — nothing deletes them once a thread rolls out of the
  `LOOKBACK_DAYS` window, even though `needs-reply.md`/`needs-like.md`/
  `actions.md`/`topics.md` stop
  mentioning it. This is intentional (so old archive links never go stale),
  but means the `data/threads/` folder only grows over time.
- `LOOKBACK_DAYS` filters by each **root post's** age, not its replies' —
  `get_conversation` always fetches every reply on an in-window root post
  regardless of when that reply itself arrived. So a slow-to-answer
  conversation (e.g. interview-style replies trickling in over a day or
  two) is still fully tracked right up until the *root post itself* ages
  past the window, at which point the whole thread (and anything still
  unanswered on it) simply stops being scanned. Default is `4` days (not
  `2`) specifically to give that kind of trickle-in exchange enough runway
  to actually get answered before it ages out unnoticed.
- `HARD_CUTOFF_DATE` (optional, unset by default) is an absolute floor on
  top of `LOOKBACK_DAYS`: root posts older than this date are never
  scanned, even if `LOOKBACK_DAYS` would otherwise include them. Useful to
  permanently wall off an old post (e.g. a one-off giveaway thread with
  hundreds of stale replies) without having to keep `LOOKBACK_DAYS` itself
  small. Accepts an ISO 8601 date or datetime, e.g. `2026-01-01` or
  `2026-01-01T00:00:00Z`. Can be set persistently via the `HARD_CUTOFF_DATE`
  repo variable, or overridden for a single on-demand run via the
  `hard_cutoff_date` **Run workflow** input (see "Alternative: GitHub
  Actions" above) -- handy when you need the new cutoff to take effect
  immediately rather than waiting for the next run.
- Replies you've hidden via Threads' own hide/unhide-reply feature (e.g. a
  scam/spam reply) are excluded from `needs-reply.md`/`needs-like.md`/
  `actions.md`/`topics.md` entirely -- they're resolved, not actionable.
  They're never silently dropped from history, though: the full
  `data/threads/<id>.md` archive (and the visual graph) still show them,
  clearly flagged 🚫 **[HIDDEN]**, alongside everything else in the
  conversation.
- Both the Threads token and the Gemini key are sent as request **headers**,
  never query params, so they can never end up embedded in a logged/committed
  error message (this previously caused a real leaked-key incident caught by
  GitHub's secret-scanning push protection — rotate immediately if it ever
  recurs).
- The Threads API and Gemini API surfaces can change; if a run starts failing
  with field/endpoint errors, check the current docs:
  - https://developers.facebook.com/docs/threads
  - https://ai.google.dev/gemini-api/docs
