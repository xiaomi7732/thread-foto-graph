"""Private continuous-mode provider and per-photo retry eligibility."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .continuous import atomic_json, finite_seconds

PATH = Path(__file__).resolve().parent.parent / "cache" / "vision-retry.json"


class VisionRetry:
    def __init__(self, path: Path = PATH):
        self.path = path
        self.data = {"version": 1, "providers": {}, "media": {}}
        if path.exists():
            with path.open(encoding="utf-8") as source:
                self.data = json.load(source)
            if (
                not isinstance(self.data, dict) or type(self.data.get("version")) is not int
                or self.data["version"] != 1
            ):
                raise ValueError("Invalid vision retry state")
            for section in ("providers", "media"):
                records = self.data.get(section)
                if not isinstance(records, dict):
                    raise ValueError("Invalid vision retry section")
                for record in records.values():
                    if (
                        not isinstance(record, dict)
                        or type(record.get("count")) is not int or not 1 <= record["count"] <= 7
                        or finite_seconds(record.get("next_at")) is None
                        or not isinstance(record.get("reason"), str)
                    ):
                        raise ValueError("Invalid vision retry record")

    @staticmethod
    def provider_key(provider: str, model: str = "", endpoint: str = "") -> str:
        identity = hashlib.sha256(f"{model}\n{endpoint}".encode()).hexdigest()
        return f"{provider}:{identity}"

    def cooling(self, section: str, key: str) -> bool:
        return self.data[section].get(key, {}).get("next_at", 0) > time.time()

    def fail(self, section: str, key: str, reason: str, *, retry_at=None, quota=False) -> None:
        previous = self.data[section].get(key, {})
        count = min(previous.get("count", 0) + 1, 7)
        delay = 3600 if quota else min(60 * 2 ** (count - 1), 3600)
        deadline = time.time() + delay
        if finite_seconds(retry_at) is not None:
            deadline = max(deadline, retry_at)
        self.data[section][key] = {"count": count, "next_at": deadline, "reason": reason}
        self.save()

    def success(self, section: str, key: str) -> None:
        if self.data[section].pop(key, None) is not None:
            self.save()

    def prune(self, media_ids: set[str]) -> None:
        self.data["media"] = {key: value for key, value in self.data["media"].items() if key in media_ids}
        self.save()

    def save(self) -> None:
        atomic_json(self.path, self.data)
