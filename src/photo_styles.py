"""Public classifications for visible reply photos, without sharing private caches."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import continuous, threads_client, topic_graph
from .cache import description_text

STYLE_PATH = Path(__file__).resolve().parent.parent / "data" / "photo-styles.json"


def photo_style(entry: object) -> str:
    text = description_text(entry)
    match = re.search(
        r"\bPhoto type:\s*(.*?)(?=\s*(?:Watch-outs|Description|Photograph read):|\Z)",
        text or "", re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "Unknown"
    return " ".join(match.group(1).split()).rstrip(".") or "Unknown"


def load_styles(path: Path | None = None) -> dict[str, str]:
    path = path or STYLE_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict) or type(data.get("version")) is not int
        or data["version"] != 1 or not isinstance(data.get("styles"), dict)
        or any(
            not isinstance(key, str) or not key
            or not isinstance(value, str) or not value.strip()
            for key, value in data["styles"].items()
        )
    ):
        raise ValueError("Invalid photo style index")
    return data["styles"]


def write_styles(threads: list[dict], cache: dict, previous: dict[str, str]) -> None:
    styles = {}

    def walk(items: list[dict]) -> None:
        for item in items:
            if topic_graph._is_hidden(item):
                continue
            for photo in threads_client.photo_items(item):
                style = photo_style(cache.get(photo["id"]))
                if style.casefold() == "unknown":
                    style = previous.get(photo["id"], "Unknown")
                if style.casefold() != "unknown":
                    styles[photo["id"]] = style
            walk(item.get("replies", []))

    for thread in threads:
        if not topic_graph._is_hidden(thread):
            walk(thread.get("replies", []))
    continuous.atomic_json(STYLE_PATH, {
        "version": 1, "styles": dict(sorted(styles.items())),
    })
