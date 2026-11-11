# Plan: local Qwen vision with Gemini fallback

Status: implementation prepared for on-box setup. Phase 1 skipped at the
user's request; live inference, quality, installation, and timer/reboot checks
remain deployment tasks, not verified claims.
Branch: `plan/qwen-vision-fallback`.

## Goal and boundaries

Run the photo-description pipeline on the Ubuntu AI box using Ollama's
`qwen3-vl:4b` first, with Gemini as an optional fallback. Preserve the existing
four-section photography prompt, generated outputs, carousel ordering,
media-ID cache compatibility, and incremental progress saving. Record provider
and model provenance for quality review from the first implementation.

Use Ollama directly at `http://127.0.0.1:11434`, not through Open WebUI.
Keep Ollama loopback-only; no new firewall rules, public endpoint, or
Tailscale connection is needed for an on-box pipeline.

Qwen's suitability is a hypothesis to evaluate, not a promise of Gemini-level
accuracy or throughput. Neither provider's descriptions are ground truth.

## Baseline integration points (before implementation)

- `src/vision.py`: image download, Gemini requests/retries, prompt,
  per-photo processing, cache population, and cross-batch circuit breakers.
- `src/main.py`: currently requires a Gemini key unconditionally and carries
  Gemini request counts and stop/failure state across batches and iterations.
- `src/config.py` and `.env.example`: provider configuration.
- `src/cache.py`: persistent media-ID-to-string descriptions, saved atomically.
- `tests/test_vision.py` and `tests/test_main.py`: existing regression coverage.
- `.github/workflows/thread-foto-graph.yml`: manually triggered, hosted
  Gemini pipeline with its own Actions cache and generated-output publishing.
- `run_daily.ps1`: Windows execution/publishing wrapper; leave its default
  Gemini behavior intact.

## Phase 1: skipped by request

Do not run the proposed comparative evaluation or make it a code-readiness
gate. Quality and throughput remain unmeasured. Use provenance and the
private review report for subsequent on-box inspection.

## Phase 2: provider selection and fallback

Proposed configuration:

```dotenv
# Existing deployments stay Gemini-only unless explicitly opted in.
VISION_PROVIDER=gemini

# AI box setting: auto means Ollama first, then Gemini when configured.
# VISION_PROVIDER=auto
# ollama means local-only, with no cloud fallback.
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3-vl:4b
```

Keep the existing Gemini model/key settings. Require a Gemini key in
`gemini` mode; allow `ollama` mode without one. In `auto` mode, allow a missing
key but log once that cloud fallback is disabled. Reject invalid provider
configuration before starting work.

Implementation:

1. Separate image downloading from provider requests. Download once per
   attempted photo and reuse the encoded bytes for fallback; a source-image
   fetch failure is not a provider failure and must not invoke Gemini.
2. Use the existing `requests` dependency for Ollama's `/api/chat` endpoint:
   send the model, existing prompt, base64 `images`, and `stream: false`.
   Read the description from `message.content`.
3. In the new Ollama path, reject empty, truncated, or structurally unusable
   descriptions before caching. Require the four expected labels, meaningful
   description/read/type sections, and permit an empty Watch-outs value.
   Preserve existing Gemini-only behavior while retaining its current
   response/error validation.
4. Fall back on local connection failure, bounded inference timeout,
   unavailable model, server error, or unusable output. Avoid long local
   retry ladders ahead of cloud fallback. Configure connect/read timeouts
   separately so loading the model is not mistaken for an immediate outage.
5. Disable a persistently unavailable local provider for the remainder of
   that run, across every root/reply batch and iteration. Keep malformed
   per-photo output distinct from a definite endpoint/model outage.
6. Preserve Gemini's existing retry, throttling, quota, moderation, and
   sanitization behavior. Do not use fallback to circumvent an explicit
   content-policy refusal.
7. Leave unsuccessful photos uncached and retryable on a future run.
   Preserve pending/unavailable output semantics and genuine moderation
   outcomes rather than caching transport errors as successful descriptions.

Keep the implementation focused: small provider functions and explicit
run-scoped state, not a general plugin framework.

## Phase 3: budgets, cache, and observability

- Keep `MAX_VISION_REQUESTS_PER_RUN` as the existing Gemini POST-attempt
  budget, including Gemini retries. Local attempts must not consume it.
- Add a separately named local-attempt budget and an overall run time limit.
  Provisional defaults: 200 local attempts, 2700s vision deadline, 3s local
  connect timeout, and 180s local read timeout. Count local failures as
  attempts. These values are not benchmark results.
- Gemini quota/budget exhaustion disables Gemini, not Qwen. Local failure or
  budget exhaustion can use Gemini if enabled and within its own budget.
  Stop new image work only when neither provider can proceed, a source-host
  outage trips the shared image-fetch breaker, or the run time limit is hit.
- Carry provider-specific budgets and failure state across batches and
  iterations. Local success must not erase a sustained Gemini failure streak.
- Preserve legacy cached strings through a backward-compatible reader,
  existing placeholder handling, per-media-ID deduplication, and immediate
  atomic saves. Do not invalidate existing descriptions merely because the
  preferred provider changes.
- Log provider choice, sanitized fallback reason, attempts, elapsed time,
  cache hits, and pending counts. Keep provenance out of photo text and topic
  clustering; persist it in cache records as described below.

### Provenance and quality review (initial implementation)

Store new successful descriptions as structured records under the existing
media-ID keys in `cache/media_cache.json`:

```json
{
  "123456789": {
    "description": "Description: ...\nPhotograph read: ...\nPhoto type: ...\nWatch-outs:",
    "provider": "ollama",
    "model": "qwen3-vl:4b",
    "described_at": "2026-11-11T18:30:00Z",
    "fallback_reason": null
  }
}
```

- Record the provider and configured model used for the successful result,
  not merely the preferred provider. Use `gemini` and the selected Gemini
  model for cloud results. The timestamp is the UTC description-generation
  time, never refreshed on a cache hit.
- For a Gemini fallback, store a short sanitized reason code such as
  `ollama_timeout`, `ollama_unavailable`, or `ollama_invalid_output`.
  Direct provider calls have no fallback reason. Never persist raw exception
  bodies, credentials, or signed image URLs as provenance.
- Read both legacy strings and new records. Label legacy provenance
  `legacy/unknown`, with unknown model/time; do not infer Gemini or invent
  metadata. Do not re-describe photos just to populate provenance.
- Extract description strings before existing placeholder checks,
  Watch-outs splitting, and output rendering. Keep normal AI-facing outputs
  and clustering inputs unchanged; persist description and metadata together
  in the same atomic save. Adapt existing cached moderation outcomes without
  changing their semantics or presenting them as valid photo descriptions.
- Add a private, local review report pairing each description with its
  media ID, photo/post reference, provider, model, original timestamp, and
  fallback reason. Clearly distinguish pending, unavailable, and legacy
  entries. Include photo previews when current images are available;
  otherwise show the reference without triggering new vision requests.
- Generate the report on demand from the cache and current Threads data.
  Do not persist signed media URLs or image bytes in the report. Keep report
  files explicitly gitignored and outside GitHub Pages and the publishing
  allowlist; escape all model and Threads text when rendering HTML.

## Phase 4: Ubuntu execution and Git publishing

1. Clone the repository on the box, create a Python virtual environment, and
   install the existing requirements. Keep tokens in a private `.env` or
   protected environment file, outside version control.
2. Privately transfer the existing `cache/media_cache.json` once if available,
   while neither pipeline is writing it. A fresh clone otherwise has no
   description cache and will reprocess the backlog.
3. Add a Linux wrapper and systemd service/timer. Run as the normal account,
   use explicit paths, bound execution time, retain logs, and serialize timer
   and manual executions with the same lock.
4. Before processing, require a clean checkout and update with `git pull
   --ff-only`; fail visibly on unexpected local changes or divergence.
5. Commit only the existing generated-output allowlist, and only after the
   pipeline succeeds. Never stage everything. Preserve cache even if pushing
   fails, retain unpushed output, and report actionable failures.
6. Use bounded push recovery. Generated-file conflicts must stop for review;
   never force-push or blindly discard another run's results.
7. Authenticate through a credential helper or equivalent protected storage.
   Use a fine-grained PAT restricted to this repository with Contents
   read/write, subject to branch rules. Never embed the PAT in a remote URL,
   script, log, or committed file.
8. Document setup, manual execution, timer management, cache migration,
   provenance/legacy labels, private review-report generation, failure
   recovery, and switching back to Gemini-only mode in `README.md`.
   Write Linux shell files with LF endings.

Do not sync the whole cache directory through Git: it also holds refreshed
Threads-token state. Keep the existing secret/cache ignore rules.

## Phase 5: backup and rollout

Start with the AI box as the only scheduled publisher. Retain the hosted
workflow as an explicitly Gemini-only manual backup, regardless of the local
provider preference. Avoid simultaneous box and hosted runs: GitHub workflow
concurrency does not lock out external Git publishers.

Provider fallback and machine failover are different:

- If Ollama fails while Python is running, the same run can call Gemini.
- If the box is offline, its timer cannot run or invoke any fallback.
  Manually trigger the hosted workflow after disabling/confirming the local
  publisher is stopped. Its independent cache may cause duplicate analysis.

Automatic hosted takeover, shared-cache synchronization, Tailscale access,
self-hosted runners, and a distributed publisher lock are deferred. They
require a separate design if unattended machine-level failover is needed.

Roll out with a small local photo budget and inspect outputs before enabling
the normal timer. To roll back, stop the timer and choose Gemini-only mode
or use the hosted backup. Existing valid cached descriptions remain valid.

## Validation and acceptance

Extend the existing unittest files with focused cases for:

- Cache hits and duplicate IDs bypassing both providers; carousel ordering,
  Watch-outs separation, and incremental persistence staying intact.
- Mixed legacy-string and structured-record cache reads, atomic round trips,
  existing placeholder handling, and unchanged description-only outputs.
- Accurate provider/model/timestamp for local and direct Gemini success,
  sanitized reasons for Gemini fallback, metadata preservation on cache
  hits, and `legacy/unknown` without fabricated attribution or new AI calls.
- Review-report attribution, legacy/pending/moderated states, escaped HTML,
  and exclusion of secrets, signed media URLs, and report files from Git
  publishing.
- Local success making no Gemini call, including with no Gemini key.
- Timeout, missing model, empty/malformed output, and fallback success,
  reusing one image download without caching failed local results.
- Independent provider state across batches/iterations: Gemini quota,
  authentication failure, or exhausted budget cannot disable healthy Qwen;
  local failures cannot repeatedly stall every photo.
- Image-download failures not calling either provider; both-provider failure
  leaving retryable output; explicit moderation not triggering bypass.
- Correct Gemini retry-attempt accounting and separate local budget limits.
- Default Gemini-only compatibility and local-only mode without cloud calls.
- Linux wrapper failures: no publish after pipeline failure, lock contention,
  dirty checkout, no-op output, push rejection, and retained cache/progress.

Run focused vision tests with:

```bash
python -m unittest discover -s tests -p 'test_vision*.py'
```

Then run the full existing suite after the cross-provider state integration:

```bash
python -m unittest discover -s tests
```

Complete a small live on-box run, a controlled Ollama-unavailable fallback
run, a second cache-hit run, and a reboot/timer check before production
enablement. Confirm the private report distinguishes Qwen, direct Gemini,
Gemini fallback, and legacy descriptions without modifying normal outputs.
Confirm Ollama remains loopback-only and no credentials, signed URLs, private
reports, or cache/token files enter generated commits.

These live checks and installation are intentionally left for the AI box.
The branch contains the provider implementation, backward-compatible cache,
private report/server, Linux wrapper, systemd templates, hosted Gemini-only
backup setting, setup instructions, and automated regression tests.

Code-readiness checks completed: all 104 automated tests pass, including
temporary-remote publishing/recovery tests and loopback review-server tests.
Bash syntax, Unix deployment-file line endings, whitespace checks, and
private-artifact ignore rules were also checked. No live model calls,
comparative evaluation, timer installation, or AI-box deployment was performed.
