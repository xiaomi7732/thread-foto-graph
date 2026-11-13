"""Opt-in adaptive scheduling and the private, source-aware cycle contract."""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "cache" / "continuous-cycle.json"
LIVE_PATHS = (
    "data/needs-reply.md", "data/needs-like.md", "data/actions.md",
    "data/topics.md", "docs/graph.json",
)
INTERVALS = (300, 900, 1800)
STOP_TIMEOUT = 6000


class CycleFailure(RuntimeError):
    def __init__(self, code: int, reason: str, retry_at: float | None = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.retry_at = retry_at


def enabled() -> bool:
    return bool(os.environ.get("CONTINUOUS_CYCLE_ID"))


def finite_seconds(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def retry_after(headers: dict) -> float | None:
    value = headers.get("Retry-After")
    delay = finite_seconds(value)
    if delay is None and isinstance(value, str):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delay = finite_seconds(max(0, target.timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            pass
    return None if delay is None else time.time() + delay


def check_threads_response(response) -> None:
    """Meta body codes are needed even for throttling reported as HTTP 400."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    status = response.status_code
    if code in (102, 190) or status == 401:
        raise CycleFailure(78, "threads_auth")
    # Graph error-handling/rate-limiting docs: 32 is Pages-specific, not Threads.
    if code in (4, 17, 613) or status == 429 or 500 <= status < 600:
        raise CycleFailure(75, "threads_transient", retry_after(response.headers))
    if status == 403 or code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        raise CycleFailure(78, "threads_permission")
    if error or status >= 400:
        raise CycleFailure(1, "threads_response")
    response.raise_for_status()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as output:
        json.dump(data, output, allow_nan=False)
    os.replace(tmp, path)


def write_status(code: int, reason: str, **fields) -> None:
    atomic_json(STATUS_PATH, {
        "version": 1, "cycle_id": os.environ["CONTINUOUS_CYCLE_ID"],
        "code": code, "reason": reason, **fields,
    })


def read_status(code: int, cycle_id: str, path: Path = STATUS_PATH) -> dict:
    with path.open(encoding="utf-8") as source:
        record = json.load(source)
    if (
        not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
        or record.get("cycle_id") != cycle_id
        or type(record.get("code")) is not int or record["code"] != code
    ):
        raise ValueError("Invalid cycle identity or outcome")
    if code == 0 and any(type(record.get(key)) is not bool for key in ("live_changed", "eligible_unattempted")):
        raise ValueError("Missing Boolean cycle activity")
    if code == 75:
        hint = record.get("retry_at")
        if hint is not None and finite_seconds(hint) is None:
            raise ValueError("Invalid retry deadline")
    return record


def live_hashes(root: Path = ROOT) -> dict:
    result = {}
    for relative in LIVE_PATHS:
        try:
            result[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except FileNotFoundError:
            result[relative] = None
    return result


def canonical_threads(threads: list[dict]) -> None:
    from .threads_client import parse_timestamp

    def key(item):
        try:
            timestamp = parse_timestamp(item.get("timestamp", "")).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            timestamp = float("-inf")
        return timestamp, str(item["id"])

    def siblings(items):
        items.sort(key=key, reverse=True)
        for item in items:
            siblings(item.get("replies", []))

    siblings(threads)


def next_due(start: float, interval: int, now: float) -> float:
    due = start + interval
    if due < now:
        due += math.ceil((now - due) / interval) * interval
    return due


class Cadence:
    def __init__(self):
        self.level = 0
        self.quiet = 0

    def succeeded(self, record: dict) -> int:
        if record["live_changed"] or record["eligible_unattempted"]:
            self.level = self.quiet = 0
        elif self.level < 2:
            self.quiet += 1
            if self.quiet == 2:
                self.level += 1
                self.quiet = 0
        return INTERVALS[self.level]


def validate_limits() -> float:
    git = finite_seconds(os.environ.get("GIT_TIMEOUT_SECONDS", "120"))
    pipeline = finite_seconds(os.environ.get("PIPELINE_TIMEOUT_SECONDS", "3000"))
    if git is None or pipeline is None or git <= 0 or pipeline <= 0:
        raise ValueError("Timeouts must be finite and positive")
    # 8 preflight + 3 staging/commit + 9 push/recovery + 1 abort; all kill graces.
    bound = 21 * (git + 5) + pipeline + 30 + 30
    if bound > STOP_TIMEOUT:
        raise ValueError("Cycle timeout overrides exceed the 6000s service stop bound")
    return bound


def supervise(root: Path = ROOT, *, clock=time.monotonic, waiter=None, launch=None) -> int:
    stop = threading.Event()
    wait = waiter if waiter is not None else stop.wait
    spawn = launch if launch is not None else subprocess.Popen
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    cadence = Cadence()
    failures = 0
    path = root / "cache" / "continuous-cycle.json"
    try:
        try:
            validate_limits()
        except ValueError:
            print("Invalid timeout configuration; manual repair required.", flush=True)
            return 78
        while not stop.is_set():
            cycle_id = str(uuid.uuid4())
            path.unlink(missing_ok=True)
            started = clock()
            print(f"Cycle {cycle_id} started {datetime.now(timezone.utc).isoformat()}", flush=True)
            child = spawn(
                ["bash", str(root / "run_daily.sh"), "--publish"],
                cwd=root, env={**os.environ, "CONTINUOUS_CYCLE_ID": cycle_id},
                close_fds=True,
            )
            # SIGTERM sets our flag only; the bounded child is allowed to finish.
            code = child.wait()
            if stop.is_set():
                return code
            if code not in (0, 75, 78):
                print(f"Cycle stopped with exit {code}; inspect logs and retained Git state.", flush=True)
                return code if code > 0 else 1
            record = read_status(code, cycle_id, path)
            if code == 78:
                return code
            if code == 75:
                failures += 1
                if failures > 5:
                    print("Threads retry limit reached; manual restart required.", flush=True)
                    return 75
                delay = min(30 * 2 ** (failures - 1), 900)
                hint = record.get("retry_at")
                remaining = max(0, hint - time.time()) if hint is not None else 0
                if remaining > 3600:
                    print("Retry-After exceeds supported wait; manual restart required.", flush=True)
                    return 75
                delay = max(delay, remaining)
                print(f"Threads retry {failures}/5 in {delay:.1f}s", flush=True)
            else:
                failures = 0
                interval = cadence.succeeded(record)
                now = clock()
                delay = max(0, next_due(started, interval, now) - now)
                print(f"Adaptive interval {interval // 60}min; waiting {delay:.1f}s", flush=True)
            if wait(delay):
                break
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Continuous runner stopped: {type(exc).__name__}; inspect configuration/private state.", flush=True)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "validate-status":
        try:
            code = int(sys.argv[2])
            if code in (0, 75, 78):
                read_status(code, os.environ["CONTINUOUS_CYCLE_ID"])
            return 0
        except (OSError, ValueError, TypeError, KeyError):
            return 1
    if len(sys.argv) == 2 and sys.argv[1] == "validate-limits":
        try:
            validate_limits()
            return 0
        except ValueError:
            return 78
    return supervise() if len(sys.argv) == 1 else 2


if __name__ == "__main__":
    sys.exit(main())
