# AI box setup: step-by-step

Follow these steps on the Ubuntu AI box, as your normal user. Commands are
Bash unless explicitly marked otherwise. Do not run the pipeline with sudo.

The repository is already cloned. This guide assumes `~/thread-foto-graph`;
substitute your actual clone directory if different. The optional systemd
templates also need updating if you use a different directory.

**First milestone:** describe a few photos with Qwen only, without publishing
anything or installing a timer. Do not enable scheduling until that works.

## 1. Check the checkout

```bash
cd ~/thread-foto-graph
git status --short
git branch --show-current
```

The branch should be `plan/qwen-vision-fallback`. If Git reports unexpected
changes, preserve and review them before updating; do not reset or discard
them. If the checkout is clean and on the correct branch:

```bash
git pull --ff-only
```

Keep any Windows scheduled pipeline and GitHub workflow stopped while setting
up this clone. They do not share the box's execution lock.

## 2. Install Python dependencies

```bash
sudo apt update
sudo apt install -y git python3-venv vim
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use the `.venv/bin/python` commands below; activating the environment is not
required.

## 3. Download the pipeline's explicit instruct model

```bash
ollama pull qwen3-vl:4b-instruct
ollama list
curl -fsS http://127.0.0.1:11434/api/tags
```

**Checkpoint:** the model list includes `qwen3-vl:4b-instruct`, and the curl
request returns JSON.

The bare `qwen3-vl:4b` alias can select a thinking model. The pipeline uses
the explicit instruct variant and sends `think: false` to reserve its short
output budget for the description. Your existing Open WebUI model can stay
unchanged.

Leave Ollama on `127.0.0.1:11434`. No firewall opening, SSH tunnel, or WebUI
API configuration is needed for the pipeline.

## 4. Create or edit the private configuration

```bash
umask 077
[ -f .env ] || cp .env.example .env
chmod 600 .env
vim .env
```

In vim, press `i` to edit. Press Esc, type `:wq`, then press Enter to save
and exit.

Fill in your existing `THREADS_ACCESS_TOKEN` and `THREADS_USERNAME` privately.
Do not paste tokens into chat, Git, screenshots, or command-line arguments.
Edit the existing settings below rather than appending duplicate entries:

```dotenv
VISION_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3-vl:4b-instruct
MAX_ROOT_THREADS=2
MAX_ITERATIONS=1
MAX_LOCAL_VISION_REQUESTS_PER_RUN=5
MAX_VISION_REQUESTS_PER_RUN=5
```

Keep `LOOKBACK_DAYS` and any `HARD_CUTOFF_DATE` appropriate for your existing
Threads project. A cutoff or short lookback can exclude photos you expected
to test.

**Checkpoint:** start with `ollama`, not `auto`. Gemini must not hide a local
inference problem during this first test. A Gemini key is not required in
this mode.

## 5. Optional: migrate existing photo descriptions

Skip this step if you intentionally want a fresh cache.

While both pipeline copies are stopped, copy only
`cache/media_cache.json` from an existing clone into this clone's `cache`
directory. Git does not transfer it. Do not overwrite an existing destination
cache without first preserving or merging its contents.

On the box, prepare a private destination directory:

```bash
mkdir -p cache
chmod 700 cache
```

For example, from **PowerShell on your laptop**, replace the source path,
`YOUR_USER`, and `BOX_IP`:

```powershell
scp "C:\path\to\thread-foto-graph\cache\media_cache.json" YOUR_USER@BOX_IP:~/thread-foto-graph/cache/media_cache.json
```

Back on the box after copying:

```bash
chmod 600 cache/media_cache.json
```

Do not copy the whole cache directory: `token_store.json` contains refreshed
Threads credentials. Never commit either cache file.

Old descriptions remain usable and have `legacy/unknown` provider attribution.
Migrating them avoids reprocessing the same photos; it does not exercise Qwen
for those cache hits.

## 6. Run the small local-only test

```bash
bash run_daily.sh
```

This command:

- Fetches up to two root posts in the configured date window.
- Makes at most five local inference attempts.
- Never calls Gemini in `ollama` mode.
- Writes local output files and saves each new description to the cache.
- Does not commit, push, install a service, or start a timer.

For a successfully described uncached photo, look for:

```text
Vision: ollama / qwen3-vl:4b-instruct
```

The final summary should show local requests and zero Gemini requests.
If the run has only cache hits, or finds no photos, **local vision has not
been tested yet**. Use recent posts with uncached photos; do not delete the
existing cache just to force a test.

Immediately after inference:

```bash
ollama ps
git --no-pager diff --stat
```

Check `PROCESSOR` for GPU use. `100% CPU` means the intended acceleration
is not being used; review the box's Ollama/Vulkan configuration before scaling
up. No result here implies a Qwen-versus-Gemini quality comparison.

## 7. Inspect descriptions and attribution

Read `data/needs-reply.md` and the run's pending/unavailable messages. Generate
the private offline report:

```bash
.venv/bin/python -m src.vision_review
```

It writes `cache/vision-review.html`, showing descriptions, provider/model,
generation time, and fallback reason. The report is gitignored, not published.
Legacy entries are marked unknown rather than being guessed as Gemini.

To add current post references without making vision calls:

```bash
.venv/bin/python -m src.vision_review --refresh
```

On a headless box, copy the static report to your laptop with scp and open it
there. For example, from **PowerShell on your laptop**:

```powershell
scp YOUR_USER@BOX_IP:~/thread-foto-graph/cache/vision-review.html .
```

The static report does not include photo previews. On a box with a local
browser, previews are available through:

```bash
.venv/bin/python -m src.vision_review --serve
```

Open `http://127.0.0.1:8091` on that same machine; Ctrl+C stops the server.
It is not accessible through the existing WebUI port. Do not expose this
private review server to the LAN or internet.

Run the pipeline again if desired:

```bash
bash run_daily.sh
```

Already described photos should be cache hits. New arrivals can still consume
the configured per-run budget.

## 8. Enable optional Gemini fallback

Only after local inference works:

```bash
vim .env
```

Set `VISION_PROVIDER=auto` and fill in `GEMINI_API_KEY` privately. Preserve your
known-working `GEMINI_MODEL`. Then:

```bash
bash run_daily.sh
```

Healthy Qwen results should still use `ollama`. Gemini is used only when the
local attempt cannot provide a usable result or the local budget is exhausted,
subject to its own budget and refusal handling.

Optional controlled fallback test, only when you have an uncached photo:

```bash
VISION_PROVIDER=auto \
OLLAMA_MODEL=thread-foto-graph-fallback-test-model-missing \
bash run_daily.sh
```

This deliberately requests a nonexistent model for that one run. It does not
stop Ollama, download a model, or change `.env`. Look for an Ollama-unavailable
message followed by `Vision: gemini`, then check the report for the Gemini
provider and `ollama_unavailable` fallback reason.

**Only cache hits do not verify fallback.** If the test makes no new requests,
wait for an uncached photo rather than deleting successful cache entries.
Cloud quota or key/model errors can also leave photos pending.

## 9. Stop here until the first milestone succeeds

Before moving on, confirm:

- At least one uncached photo succeeded with provider `ollama`.
- The description and private report are readable.
- A later run reuses that cached description.
- If cloud fallback is wanted, its credentials and request budget are set.

Live installation and these checks happen on your box; they were not performed
by the implementation's offline regression suite.

## 10. Optional: prepare publishing

Publishing is a separate step. It uses the checked-out branch's upstream,
not hard-coded `main`. Keep initial deployment work on the feature branch.
Do not publish the two-post smoke output to production.

Configure your Git identity and an OS-protected credential helper. A
fine-grained PAT needs Contents read/write for this repository and permission
under its branch rules. Supply the PAT through the helper, not a remote URL,
script, `.env`, or shell history. Do not use Git's plaintext credential store.

Check authentication and the target without publishing:

```bash
git branch --show-current
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
git push --dry-run
```

The wrapper refuses dirty checkouts and pre-existing unpushed commits. Review
the smoke output:

```bash
git status --short
git --no-pager diff --stat
```

If these are only the expected generated smoke outputs, you can preserve them
in a local stash before a full publishing run. This does not touch the ignored
vision/token caches:

```bash
git stash push --include-untracked -m "AI box smoke outputs before publishing" -- \
  data/needs-reply.md data/needs-like.md data/actions.md data/topics.md \
  data/threads data/history docs/graph.json
git status --short
```

Keep the stash for reference; do not automatically pop stale smoke outputs
over newly generated output. If other changes remain, review them separately.
Do not use a blanket reset or stash private credentials.

Edit `.env` before the normal run: clear/remove `MAX_ROOT_THREADS`, restore
`MAX_ITERATIONS=3`, and choose appropriate local and Gemini request budgets.
The example's five-attempt caps are for testing, not full backlog processing.
The template's operational defaults are not quality or throughput benchmarks.

With a clean checkout and working authentication:

```bash
bash run_daily.sh --publish
```

This fast-forwards first, runs the pipeline, and commits/pushes only the
generated-output allowlist. It preserves cache progress if publishing fails.
Git commands have individual time limits; conflicts or interrupted recovery
need manual inspection, not force-pushing or deleting Git recovery files.

## 11. Optional: enable scheduling after publishing works

These are user-level units, installed as your normal user:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/thread-foto-graph.service deploy/thread-foto-graph.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start thread-foto-graph.service
journalctl --user -u thread-foto-graph.service -n 100 --no-pager
```

**Checkpoint:** the manual service run succeeds and publishes to the intended
branch before enabling the timer.

```bash
systemctl --user enable --now thread-foto-graph.timer
systemctl --user list-timers thread-foto-graph.timer
sudo loginctl enable-linger "$USER"
```

The template runs at 06:00 local time with up to five minutes of jitter.
Lingering lets the user service run after logout and reboot. After a later
reboot, check the timer list and service logs again.

To stop scheduled runs:

```bash
systemctl --user stop thread-foto-graph.timer
systemctl --user status thread-foto-graph.service --no-pager
```

Stopping the timer does not stop an already running service. Wait for it to
finish, or deliberately stop the service, before starting the hosted manual
Gemini backup. There is no automatic failover if the whole box is offline.

## Troubleshooting checkpoints

| Symptom | Next check |
| --- | --- |
| Ollama curl fails | `systemctl status ollama --no-pager` and `journalctl -u ollama -n 100 --no-pager` (sudo if needed). |
| Local model unavailable | Confirm `ollama list` contains the exact instruct tag used in `.env`. |
| All cache hits / no local requests | No new image was analyzed; use recent uncached photos within the scan window. |
| Pending photos | Check budgets, date window, run deadline, and logged provider errors. Pending is not the same as no photo. |
| Local read timeout | Check GPU use and concurrent WebUI activity before increasing `OLLAMA_READ_TIMEOUT_SECONDS`. |
| Gemini fails | Check the private key/model configuration, quota, and fallback logs; never paste credentials into a report. |
| Checkout dirty / unpushed commits | Inspect `git status`; preserve and resolve outputs or failed publishing before retrying. |
| Another run holds the lock | Check for the active manual/service run. Do not delete the lock file to bypass a live run. |
| CRLF / `pipefail` error | `sed -i 's/\r$//' run_daily.sh`; the repository's `.gitattributes` enforces LF for shell files. |
