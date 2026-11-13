# Plan: continuous AI-box execution and change-only publishing

Status: revision 5 implemented and code-reviewed; AI-box deployment checks pending.

## Review consensus

GPT-6.1 Sol and Claude Opus 5.5 independently reviewed the plan, identified
blockers in the initial draft, and explicitly approved revision 3 as READY
with no remaining blockers. Revisions addressed source-aware failures,
persistent vision cooldowns, stable pending output, retry-state validation,
deterministic presentation, and graceful shutdown.

That consensus applies to revision 3's zero-delay strategy. Revision 4 replaces
healthy-cycle scheduling with the adaptive policy below. Its first review
identified ambiguous activity-result ownership; revision 5 defines that
contract. GPT-6.1 Sol and Claude Opus 5.5 then independently approved the
unchanged revision 5 in two consecutive rounds:

| Approval round | GPT-6.1 Sol | Claude Opus 5.5 |
| --- | --- | --- |
| 1 | READY; no blockers | READY; no blockers |
| 2 | READY; no blockers | READY; no blockers |

No substantive plan edits occurred between those rounds. This section records
the completed reviews without changing the approved implementation design.
Neither design review constitutes
implementation or deployment verification.

## Implementation progress

The adaptive wrapper, source-aware cycle outcomes, persistent vision retry
state, child-owned activity hashes, one-pass/canonical mode, optional user
service, focused tests, and setup documentation are implemented.
`run_continuous.sh` holds the supervisor lock and delegates scheduling to
`src.continuous`, using Python's monotonic clock and signal handling directly
rather than repeatedly launching a clock helper from Bash.

Existing daily modes remain unchanged. The full local regression suite passed:
146 tests, with three Linux-only process fixtures skipped on Windows.
GPT-6.1 Sol and Claude Opus 5.5 independently returned CLEAR in two consecutive
rounds on the same corrected implementation. Their initial review caught a
photo-specific HTTP 400 incorrectly being treated as fatal configuration;
that was fixed and regression-tested before the two clear rounds.
Only this progress record was updated after the final approvals.
Windows has no Linux runtime or `flock`, so the Linux-only real-process tests
and bounded live AI-box installation/reboot/sample checks remain deployment
checks. No service has been installed/enabled and no live publishing run was
started here. Use the bounded rollout checklist in `AI_BOX_SETUP.md` before
enabling unattended operation.

## Goal and confirmed choices

Keep the main Thread Foto Graph pipeline available on the Ubuntu AI box,
checking frequently while active and less frequently while quiet. Publish
changed generated outputs after every successful cycle.

- Adaptive start-to-start intervals of 5, 15, and 30 minutes. After two
  consecutive unchanged successful checks, increase from 5 to 15 minutes;
  after two further unchanged checks, increase to 30 minutes.
- Any live-output change resets to 5 minutes and clears the quiet counter.
- Record each actual cycle start. Schedule from that start, not completion.
- Never overlap runs or replay missed cycles. On an overrun, skip missed
  slots and wait for the next scheduled slot at or after completion.
- Five-minute freshness is an approximate target during observed active
  periods. First activity after a quiet period may wait 30 minutes plus
  processing before appearing in published feeds.
- Use one collection/vision pass per adaptive cycle; keep daily-mode iteration
  configuration unchanged.
- Retry transient API failures with bounded backoff; stop for authentication,
  configuration, or Git problems requiring manual recovery.
- Cover the main topic graph only, not the gallery or outward tracker.
- Preserve existing daily/manual modes and normal append-only Git history.
  No amend, squash, force-push, or history rewriting.

## Current state and answer to the concern

The existing publisher already avoids commits when staged generated outputs
are byte-identical (`run_daily.sh`; covered by
`tests/test_run_daily.py::test_unchanged_output_creates_no_commit`).
Repeated file writes alone do not create Git changes.

`src/topic_graph.py` does not embed per-run generation timestamps or rotating
image URLs in normal published outputs. However:

- New replies, likes resolving actions, descriptions, text changes, and roots
  leaving the rolling lookback window can change published content.
- `data/history/` gains two date-named snapshots at each Pacific date rollover,
  even if the live feeds are unchanged.
- API ordering changes can produce output differences. Continuous mode will
  canonicalize presentation order to avoid order-only commits; daily mode
  remains unchanged.
- Publishing every real change can produce many commits during busy activity.
  This policy cannot guarantee a small Git history. Publication batching
  would reduce commits, but delay updates to remote consumers.

The bigger risk of an immediate loop is resource usage, not empty commits:

- `src/main.py` fetches roots and every in-scope conversation on each cycle.
  Pagination adds requests; cache hits bypass vision, not Threads fetching.
- Up to `MAX_ITERATIONS` full collection passes can happen within one cycle.
- `VisionRun` budgets and circuit breakers reset each invocation. Per-run
  budgets are not daily quotas, and repeatedly pending photos may be retried.
- Each publish cycle also fetches/pulls Git, even when outputs remain unchanged.
- The current timer is daily; the service is a bounded oneshot. Neither
  currently implements continuous execution.
- Threads `_get()` currently raises on HTTP/network failure without a
  continuous-run retry policy. Some vision failures instead produce pending
  output and a successful pipeline exit.
- Dirty checkouts, unpushed commits, and push conflicts intentionally stop
  publication. Cache/progress is retained, not discarded.

No measured on-box Threads request rate or current account API allowance is
available from this analysis. Adaptive polling reduces idle collection,
Git operations, and process startups compared with an immediate loop, but
does not eliminate full fetching or guarantee staying within API limits.
Rate-limit and outage backoff must still apply.

Fixed intervals would be simpler; active-hours schedules would save more
overnight work at the cost of freshness. Webhooks or incremental fetching
could avoid more idle work but require separate API verification and design.
Publication batching reduces commits/pushes, not collection or inference.
Narrowing the lookback risks missing late replies and is not part of this plan.

## Proposed implementation

### 1. Define source-aware cycle outcomes

Keep default one-shot behavior compatible. Enable the new outcome contract
only when called by the continuous supervisor; never parse human logs.

| Wrapper exit | Meaning | Supervisor action |
| --- | --- | --- |
| 0 | Render and publish succeeded, including no-change or vision-pending output | Schedule the next adaptive cycle |
| 75 | Verified transient Threads collection/token-refresh failure | Bounded whole-cycle retry |
| 78 | Authentication or invalid configuration, including enabled vision providers | Stop; manual repair |
| 73 | Pipeline or supervisor lock contention | Stop visibly |
| 70 | Git/preflight/publishing/recovery failure | Stop; retain state |
| Other nonzero | Unknown error, malformed API response, hard pipeline timeout, or signal interruption | Stop; fail closed |

`run_daily.sh` must explicitly capture Python/timeout results instead of
letting `set -e` propagate an ambiguous native exit. Only Python's verified
transient outcome maps to 75. Every Git failure maps to 70, even if Git or
its timeout returned 75 or 124. Do not retry a Git failure as an API failure.
Retain default wrapper exit behavior when continuous mode is not enabled.

Classify collection and token-refresh failures in `src/threads_client.py`
and `src/threads_auth.py` using HTTP status and structured Meta error codes:

- Network/connection timeouts, HTTP 429, and HTTP 5xx are retryable.
- Explicit invalid/expired credentials are fatal, with authentication
  precedence over generic transient classification.
- Verify the documented Meta throttling codes before implementation; include
  representative 4/17/32/613 throttling and 190 authentication fixtures where
  confirmed applicable. Never treat every HTTP 400/403 as retryable.
- Unknown errors, schema changes, JSON decoding errors, and other 4xx fail closed.

Validate finite, nonnegative retry hints. Support Retry-After seconds and
HTTP dates; use documented usage-header recovery hints only after verifying
their scope and units. With no valid hint use the bounded backoff below.
Do not assume a Gemini daily-quota reset time or fabricate a Retry-After.

An enabled vision provider's explicit credential/configuration error is fatal
even if an alternate provider could succeed. Record incremental valid cache
progress but return 78 before rendering/publishing that cycle. Configured
absence of optional cloud fallback is not an auth error.

Pass a unique cycle ID from supervisor to child. Write atomic, versioned
`cache/continuous-cycle.json` outcomes only in continuous mode, matching that
ID. Reset previous status before launch. Missing/mismatched/invalid status
on claimed success or Python-reported 75/78 fails closed. Read a verified
exit-75 record before choosing retry timing; do not infer a retry from a
native exit code alone. Failed early Git preflight needs no Python status.
Persist only sanitized codes/counters/deadlines, never credentials,
raw exception bodies, or signed media URLs.

Successful-cycle records must also include Boolean `live_changed` and
`eligible_unattempted` fields. The Python child owns these results; the
supervisor consumes them only after wrapper exit 0 confirms successful
publication or verified no-change publication. Missing fields, wrong types,
or invalid cycle identity fail closed, not as quiet activity.

### 2. Preserve vision retry state without delaying healthy Threads polling

Add a small continuous-only, versioned, atomic
`cache/vision-retry.json`, separate from successful description cache.
Read/update it under the existing pipeline lock. It survives cycle and
supervisor restarts; corruption fails closed rather than enabling hot retries.

Store provider-scoped outage/quota cooldowns and per-media-ID transient failure
counts, next-attempt times, and sanitized reason categories. Image-host
systemic failures get a shared fetch cooldown. Key provider state by provider
and relevant model/endpoint identity, not credentials; a configuration change
must not inherit unrelated model-outage state.

- Provider failure state survives successful rendering/publication and
  successful fallback. Skip cooling providers while healthy alternatives run.
- A failed media item is attempted only when eligible; other photos and
  Threads collection proceed normally.
- Use exponential cooldowns for transient provider/media failures, starting
  at 60 seconds and capped at 3600 seconds. Saturate counters rather than
  overflow. Honor a valid later provider not-before deadline without shortening it.
- For quota exhaustion with no reliable reset hint, probe no more frequently
  than hourly; this is an operational cooldown, not daily quota accounting.
- Vision cooldown probes do not exhaust the Threads retry counter or stop
  Threads polling. Persistent vision outages remain pending and retry at
  bounded probe frequency, not in an immediate loop.
- Reset a provider streak only on success from that provider; alternate
  success does not reset it. Clear per-media failure state only on successful
  description or genuine cached moderation, not merely successful publication.
- Persist failure state immediately with atomic saves, before continuing to
  another photo. Keep valid cache persistence unchanged.
- Remove obsolete per-media failure records when no longer in the collected
  scope; keep active provider cooldowns. Do not clear records merely because
  a collection cycle failed.

The existing Gemini path clamps long Retry-After waits. Explicitly authorize
a minimal continuous-mode adjustment: propagate the original not-before
deadline through internal retry exhaustion and the vision deadline. Never
make an internal retry earlier than requested. If the wait cannot fit the
current vision deadline, defer the provider and let the cycle finish; daily
mode retains its existing retry behavior.

In continuous mode, render retryable failures and cooldown skips using the
same stable pending placeholder, not changing exception messages. Preserve
genuine moderation/unavailable semantics. Detailed sanitized causes belong
in logs/private state, not published photo text. Do not cache failed attempts
as successful descriptions or invalidate existing valid descriptions.

### 3. Add an optional continuous runner

Add `run_continuous.sh`, invoking the existing `run_daily.sh --publish` once
per cycle. Keep publishing, clean-checkout checks, the generated-output
allowlist, timeouts, and bounded push recovery in the existing wrapper.

#### Adaptive cadence and activity

- Start the first cycle immediately at the 5-minute level.
- Maintain a quiet counter for the current interval. Two consecutive
  successful unchanged checks at 5 minutes select 15 minutes and reset the
  counter; two at 15 select 30. Stay at 30 while quiet.
- A live-output change selects 5 minutes and clears the counter. Inside
  `src.main`, after wrapper Git fast-forward preflight and before processing,
  hash the allowlisted live output bytes: `data/needs-reply.md`,
  `data/needs-like.md`, `data/actions.md`, `data/topics.md`, and `docs/graph.json`.
  Hash those paths again after rendering and write Boolean `live_changed`
  into the current cycle's atomic status record. Represent missing files with
  a distinct sentinel; file-read failures are fatal, not an unchanged result.
- Exclude dated history and private timestamps/state from activity detection.
  A history-only rollover can publish without resetting the interval.
  Pending-to-described photo output is a live change; cooldown/log changes
  alone are not.
- The supervisor uses the validated `live_changed` result only after wrapper
  exit 0. No baseline is maintained across cycles or restarts. Post-preflight
  files are the baseline for that cycle; a Git fast-forward by itself is not
  activity. Restart begins at 5 minutes even when the first cycle is unchanged.
- Record `eligible_unattempted=true` when otherwise eligible pending photos
  were not attempted because the per-cycle provider budget or vision deadline
  ran out. Eligibility means a configured provider/image source would permit
  a new attempt now in a fresh cycle, excluding cached moderation and active
  provider/media/image-host cooldowns. It is not simply the pending-photo count.
- If either `live_changed` or `eligible_unattempted` is true, select 5 minutes
  and clear the quiet counter. Promote only when both are false. Cooldown-only
  pending photos may count as quiet; eligible budget/deadline backlog cannot.
- Set one collection/vision pass for this opt-in mode, rather than following
  `MAX_ITERATIONS` with repeated full collections within the cycle. Photos
  arriving during processing are picked up on the next scheduled cycle.
- On supervisor restart, start immediately at 5 minutes; existing persistent
  provider/media cooldowns remain applicable.

#### Start-to-start timing and overruns

Record each actual cycle start with a monotonic clock for scheduling and a
UTC timestamp for logs. Use Python's `time.monotonic()` through a small helper
invoked with the existing virtualenv interpreter; do not substitute Bash
`SECONDS`, `date`, or wall-clock timestamps. Inject this clock in tests.
After a successful cycle, choose the next interval
from the activity result and compute:

```text
now = monotonic_now()
next_due = actual_cycle_start_monotonic + chosen_next_interval
if next_due < now:
    missed = ceil((now - next_due) / chosen_next_interval)
    next_due += missed * chosen_next_interval
wait = max(0, next_due - now)
```

Calculate skipped slots directly; do not execute a cycle for each overdue
slot. Wait interruptibly and record the next actual start as a fresh anchor.
With a 5-minute interval, a 10:00 start and 10:02 finish means next start at
10:05. A 10:00 start and 10:07 finish means next start at 10:10.
An exact on-time finish may start the next cycle at that slot, but runs never
overlap. Do not retain an overdue anchor that causes catch-up bursts.
Wall-clock changes must not alter monotonic cadence.

#### Failures, locks, and shutdown

- On verified transient Threads failures, allow five retries after the initial
  failure, with exponential delays starting at 30 seconds and capped at 900
  seconds. Wait for the later of that delay and valid Retry-After. If a hint
  requires more than 3600 seconds, stop visibly rather than retry earlier.
  Stop after retry exhaustion.
- Do not silently shorten a long provider-requested wait. Stop visibly if
  it exceeds the supported wait bound.
- Reset the Threads retry counter only after an end-to-end successful cycle,
  whether changed, unchanged, or vision-pending. Provider/media counters are
  independent and follow section 2. A failed retry never resets its streak.
- Failed collection never counts as quiet. Freeze the adaptive quiet counter
  during retries; use failure backoff rather than the normal
  cadence. Each retry records its own actual start. After success, resume
  start-to-start scheduling from that start using the resulting activity level.
- Publish valid pending output and continue scheduled Threads collection.
  Provider/media cooldowns suppress vision attempts, not the whole cycle.
- Do not treat cached moderation, configured absence of cloud fallback,
  ordinary cache hits, or ordinary per-photo pending results as fatal failures.
- Stop for fatal outcomes. Never automatically discard files, push retained
  commits, resolve conflicts, or delete Git recovery state.
- Trap termination: set a stop flag, interrupt adaptive or failure-backoff waits,
  but let an already active bounded cycle finish. Do not forward the graceful
  signal into rendering, commit, push, or rebase. Wait correctly even if a
  shell `wait` is interrupted by a trap, and begin no further cycles.
- Hold a separate supervisor lock for the supervisor lifetime; retain the
  existing pipeline lock per cycle. Duplicate supervisors and overlapping
  manual/daily execution stop visibly rather than spin on contention.
- Close the supervisor lock descriptor in launched children so a descendant
  cannot retain it after supervisor exit. The per-cycle pipeline lock stays
  held until that cycle finishes.

Keep the policy small and explicit: no worker pool, daemon framework,
webhooks, or incremental Threads collection in this change.

### 4. Add an opt-in systemd service

Add `deploy/thread-foto-graph-continuous.service` for the long-lived runner.
Keep the existing daily service/timer unchanged.

- Use the normal user, explicit working directory, restrictive umask, and
  journald logging.
- Retain per-cycle pipeline and Git timeouts, but do not apply the daily
  service's 55-minute oneshot limit to the entire supervisor lifetime.
- Use `KillMode=mixed`: graceful SIGTERM goes to the supervisor first; only
  stop-timeout expiry forcibly kills the remaining service group. Graceful
  stop waits for the current cycle; it is not immediate cancellation.
- Set `TimeoutStopSec` from a validated upper bound covering the entire cycle:
  Git preflight, pipeline hard timeout and kill grace, staging/commit, all
  push/fetch/rebase attempts, recovery abort, and sleeps. Derive the bound
  during implementation rather than guessing from Python's timeout alone.
  Validate deployment timeout overrides against that bound, or reject them.
- Forced termination, crashes, and power loss may leave dirty output,
  unpushed commits, or Git recovery state. Next startup stops for manual
  recovery; no automatic cleanup. Graceful stop must avoid those interruptions.
- Do not automatically restart fatal failures; require manual repair/restart.
- Document disabling the daily timer and stopping its active service before
  enabling continuous mode. Keep hosted/Windows publishers inactive too.

### 5. Stabilize continuous presentation and verify operational behavior

Continuous mode sorts roots and recursively sorts sibling replies by parsed
timestamp plus media ID, with a deterministic missing/invalid-timestamp policy,
before clustering/rendering. Keep carousel photo order intact and preserve
parent/child structure, newest-first vision priority, hidden/action semantics,
and legacy daily presentation. Do not sort JSON arrays blindly after rendering.
Test shuffled equivalent API inputs for identical output bytes.

Extend existing tests instead of adding new dependencies:

- `tests/test_run_daily.py`: repeated unchanged publish cycles create no
  extra commits; real changes publish normally; safety failures retain state.
- `tests/test_topic_graph.py`: repeated identical input on the same mocked
  Pacific date produces identical allowlisted output bytes; date rollover
  adds expected snapshots without modifying unchanged live feeds.
- `tests/test_main.py` / `tests/test_vision.py`: structured outcomes distinguish
  auth failures, transient errors, quota/outage deferral, healthy fallback,
  ordinary pending photos, and moderation without poisoning cache entries.
- Cover collection and token refresh, confirmed Meta body-error fixtures,
  malformed API responses, credential/configuration precedence, Git/Python
  exit-code collisions, hard timeout, and cycle-ID/status corruption.
- Cross-cycle vision tests: outage plus successful fallback, no fallback,
  alternating failure reasons with stable output, per-photo eligibility,
  quota without reset hint, and successful-pending publication cannot erase
  cooldowns. Include state reload across supervisor restarts.
- Retry hints: seconds/HTTP dates, invalid/nonfinite values, internal Gemini
  retry exhaustion, long waits exceeding a cycle deadline, and no early retry.
- Focused supervisor tests with mocked child commands, sleeps, and temporary
  Git remotes: adaptive success, no-change behavior, bounded retry/backoff
  and Retry-After, retry exhaustion, fatal stop, supervisor/pipeline lock
  contention, stale-status rejection, and clean termination.
- Use a fake monotonic clock to cover 5/15/30 transitions, live-change reset
  at every level, history-only rollover, pending-to-described changes,
  cooldown-only changes, unchanged first/restarted cycles, failed-cycle counter
  freezing, supervisor restart, and wall-clock jumps.
- Validate child-owned preflight/render hash comparison, missing-file sentinel,
  fatal read errors, Boolean field/type validation, history-only changes,
  preflight Git changes alone, successful publish/no-change consumption,
  rejected-push status rejection, and no stale cross-cycle baseline.
- Cover eligible unattempted budget/deadline backlog holding the 5-minute
  level versus cooldown-only pending photos allowing quiet promotion.
- Verify start-to-start deadlines, short and long runs, exact-boundary finishes,
  10:00/10:07/10:10 overrun behavior, skipped slots, no overlap, and no catch-up
  bursts. Confirm one collection/vision pass per adaptive cycle without changing
  normal daily iteration behavior.
- Add bounded real-process fixtures for graceful stop during rendering,
  Git publication/recovery, adaptive waits, and backoff. Verify no next cycle, no surviving
  descendant or held lock, and clean checkout after successful publication
  (or retained unpushed commit after a genuine push failure). Test forced-stop
  recovery separately; mocked flock/timeout alone cannot establish this.

Do not create a live infinite loop during automated validation. Run bounded
fixtures without Threads, Gemini, Ollama, or GitHub calls.

Run targeted checks, then the full existing unittest suite:

```bash
python -m unittest discover -s tests -p 'test_run*.py'
python -m unittest discover -s tests -p 'test_main.py'
python -m unittest discover -s tests -p 'test_vision.py'
python -m unittest discover -s tests -p 'test_topic_graph.py'
python -m unittest discover -s tests
bash -n run_daily.sh run_continuous.sh
git diff --check
```

Verify Bash/deployment files retain LF endings and private status artifacts
are gitignored and excluded from publication.

### 6. Document operations and perform bounded on-box rollout

Update `README.md` and `AI_BOX_SETUP.md` with:

- Daily versus continuous operation and exact start/stop/log commands.
- Adaptive intervals and start-to-start timing, quiet counter transitions,
  skipped missed slots, and no overlapping runs or catch-up bursts.
- No-change commits versus genuine changes and daily snapshots.
- Full-fetch cost, one-pass adaptive cycles, per-run versus daily budgets,
  and the first-activity-after-quiet freshness tradeoff.
- Retry/cooldown behavior and manual Git/auth recovery.
- One scheduled publisher at a time and rollback to the daily timer.
- Graceful stop may wait for the active bounded cycle; forced cancellation
  requires checking retained output/Git state.
- A foreground manual Ctrl-C can signal Python/Git directly and counts as
  forced interruption, not graceful shutdown. Use `systemctl --user stop`
  for graceful service operation. Shutdown/reboot may wait for the full
  configured cycle bound; document that wait rather than treating it as a hang.
- Even a transient Git transport failure intentionally stops the service.
  Journal/service-status checks are required so a stopped publisher is not
  mistaken for fresh feeds; notification integration remains optional.

Before unattended enablement, inspect a bounded live sample on the AI box:
cycle duration, Threads request volume, provider attempts, pending photos,
output changes, and commit count. Do not assert an API allowance without
current account evidence. Inspect whether cycle durations fit the active
interval and whether quiet periods actually reduce requests. This policy
does not guarantee published photo descriptions within five minutes.
Stop if traffic is excessive; changing intervals, lookback, or publishing
frequency requires revisiting the agreed choices.

## Todos and dependencies

1. Define source-aware outcomes, token-refresh classification, and private status.
2. Add continuous-only persistent vision cooldowns and stable pending output.
3. Implement the adaptive supervisor, child-owned activity results/counters, monotonic
   scheduling, one-pass cycles, retry counters, locks, and graceful stop.
4. Add the optional continuous service and validate its stop-timeout bound.
5. Stabilize presentation, add regression coverage, and run integration checks.
6. Update operations documentation and the on-box rollout checklist.

Todo 2 depends on 1. Todo 3 depends on 1 and 2. Todo 4 depends on 3.
Todo 5 depends on 1, 2, 3, and 4. Todo 6 depends on 3 and 4.

## Out of scope

History compaction, publication batching, daily cloud-quota accounting,
shared caches, distributed publisher locks, automatic hosted takeover,
gallery/outward scheduling, webhooks, and incremental API fetching.
