"""Host-memory admission checks for loopback Ollama on Linux."""
from __future__ import annotations

import ipaddress
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config

CONFIRM_SECONDS = 5
WARNING_INTERVAL_SECONDS = 900
MEMINFO = Path("/proc/meminfo")


def supported() -> bool:
    if sys.platform != "linux" or config.OLLAMA_MIN_AVAILABLE_MEMORY_MIB == 0:
        return False
    host = urlsplit(config.OLLAMA_BASE_URL).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def snapshot() -> dict[str, int]:
    required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    values = {}
    for line in MEMINFO.read_text(encoding="ascii").splitlines():
        key, _, raw = line.partition(":")
        if key in required:
            parts = raw.split()
            if len(parts) != 2 or parts[1] != "kB":
                raise ValueError("Invalid memory units")
            values[key] = int(parts[0])
    if (
        values.keys() != required
        or not 0 <= values["MemAvailable"] <= values["MemTotal"]
        or values["MemTotal"] <= 0
        or not 0 <= values["SwapFree"] <= values["SwapTotal"]
    ):
        raise ValueError("Invalid memory counters")
    return values


class LocalMemoryGuard:
    def __init__(self, retry=None):
        self.retry = retry
        self.last_warning_at = (
            retry.data.get("local_memory_warning_at") if retry is not None else None
        )

    def warn(self, memory: dict[str, int] | None) -> None:
        now = time.time()
        if (
            self.last_warning_at is not None
            and 0 <= now - self.last_warning_at < WARNING_INTERVAL_SECONDS
        ):
            return
        if memory is None:
            details = "host memory metrics unavailable"
        else:
            details = (
                f"MemAvailable={memory['MemAvailable'] // 1024}MiB "
                f"minimum={config.OLLAMA_MIN_AVAILABLE_MEMORY_MIB}MiB; "
                f"swap_free={memory['SwapFree'] // 1024}/{memory['SwapTotal'] // 1024}MiB"
            )
        print(
            f"WARNING: Local inference deferred: {details}. "
            "Collection continues; no reboot, service restart, or model unload was requested.",
            flush=True,
        )
        self.last_warning_at = now
        if self.retry is not None:
            self.retry.data["local_memory_warning_at"] = now
            self.retry.save()

    def ready(self, deadline: float) -> bool:
        if not supported():
            return True
        try:
            memory = snapshot()
            if memory["MemAvailable"] >= config.OLLAMA_MIN_AVAILABLE_MEMORY_MIB * 1024:
                return True
            if deadline - time.monotonic() >= CONFIRM_SECONDS:
                time.sleep(CONFIRM_SECONDS)
                memory = snapshot()
                if memory["MemAvailable"] >= config.OLLAMA_MIN_AVAILABLE_MEMORY_MIB * 1024:
                    return True
        except (OSError, UnicodeError, ValueError):
            memory = None
        self.warn(memory)
        return False
