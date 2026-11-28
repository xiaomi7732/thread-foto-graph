"""Generate a reply-photo contact sheet for the latest graph's root threads."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from . import config, threads_client, topic_graph
from .cache import load_cache
from .photo_styles import load_styles, photo_style

ROOT_DIR = Path(__file__).resolve().parent.parent

STYLE_CATEGORIES = (
    ("Macro", r"macro|close[\s-]?up|micro"),
    ("Animals", r"wildlife|animal|bird|pet|dog|cat|insect|butterfly"),
    ("Plants & flowers", r"plant|flower|floral|botanical|flora"),
    ("Architecture", r"architectur\w*|building|interior|cityscape"),
    ("Street & documentary", r"street|documentary|candid|photojournal\w*|travel|urban"),
    ("Portrait", r"portrait\w*|people|person|selfie|fashion|family|wedding"),
    ("Landscape", r"landscape\w*|nature|scenery|scenic|seascape|nightscape|astro\w*|sky|sunset|sunrise"),
    ("Food & still life", r"food|still[\s-]?life|product|tabletop"),
    ("Abstract & experimental", r"abstract|experimental|minimal\w*|icm|intentional camera movement"),
)


def _style_category(style: str) -> str:
    if style.strip().casefold().rstrip(".") in ("", "unknown", "unclassified"):
        return "Unknown"
    for category, pattern in STYLE_CATEGORIES:
        if re.search(rf"\b(?:{pattern})(?:s)?\b", style, re.IGNORECASE):
            return category
    return "Other"


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Gallery media and permalink URLs must be absolute HTTPS URLs")
    return value


def collect_gallery(
    graph: dict, media_cache: dict | None = None, style_index: dict | None = None,
) -> list[dict]:
    """Refresh media URLs for exactly the roots in the current topic graph."""
    threads = []
    for node in graph["nodes"]:
        thread = {
            "id": node["id"],
            "topic": node["label"],
            "group": node["group"],
            "text": node.get("text") or node.get("snippet") or node["id"],
            "url": _https_url(node["permalink"]),
            "photos": [],
        }
        replies = threads_client._nest_replies(
            node["id"], threads_client.get_conversation(node["id"])
        )

        def walk(items: list[dict]) -> None:
            for reply in items:
                if topic_graph._is_hidden(reply):
                    continue
                for index, photo in enumerate(threads_client.photo_items(reply), start=1):
                    media_url = photo.get("media_url")
                    if not media_url:
                        print(
                            f"Warning: photo {photo['id']} has no media URL; "
                            "keeping its reply link in the gallery.",
                            flush=True,
                        )
                    thread["photos"].append({
                        "id": photo["id"],
                        "url": _https_url(media_url) if media_url else "",
                        "reply_url": _https_url(reply.get("permalink") or thread["url"]),
                        "username": reply.get("username") or "unknown",
                        "text": reply.get("text") or "",
                        "number": index,
                        "style": (style_index or {}).get(photo["id"]) or photo_style(
                            (media_cache or {}).get(photo["id"])
                        ),
                    })
                walk(reply.get("replies", []))

        walk(replies)
        threads.append(thread)
    return threads


def _button(label: str, text: str, kind: str = "") -> str:
    return (
        f'<button type="button" data-copy="{escape(text, quote=True)}"'
        f' data-kind="{kind}">{label}</button>'
    )


def _photo_markdown(photo: dict) -> str:
    label = escape(f"@{photo['username']} photo {photo['number']}", quote=True)
    reply_url = escape(photo["reply_url"], quote=True)
    if not photo["url"]:
        return f'<a href="{reply_url}">{label} (image unavailable)</a>'
    return (
        f'<a href="{escape(photo["url"], quote=True)}"><img src="{escape(photo["url"], quote=True)}" '
        f'alt="{label}" width="160" /></a>'
    )


def render_gallery(threads: list[dict], generated: str) -> tuple[str, str]:
    count = sum(len(thread["photos"]) for thread in threads)
    style_counts = Counter(
        _style_category(photo.get("style", "Unknown"))
        for thread in threads for photo in thread["photos"]
    )
    style_filters = "\n".join(
        f'<label><input type="checkbox" class="style-filter" '
        f'value="{escape(category.casefold(), quote=True)}" /> '
        f'{escape(category)} ({style_counts[category]})</label>'
        for category in sorted(style_counts)
    )
    note = (
        f"Generated {generated}. {count} reply images. Includes answered and "
        "unanswered replies; hidden branches and root-post images are excluded. "
        "Image URLs can expire; rerun the gallery action to refresh them."
    )
    markdown = ["# Reply image gallery", "", note, ""]
    sections = []
    groups: dict[int, list[dict]] = {}
    for thread in threads:
        groups.setdefault(thread["group"], []).append(thread)
    topic_options = []
    for members in groups.values():
        topic = escape(members[0]["topic"])
        topic_options.append(f'<optgroup label="{topic}">')
        markdown.extend([f"<h2>{topic}</h2>", ""])
        sections.append(f'<section class="gallery-topic"><h2>{topic}</h2>')
        for thread in members:
            text = " ".join(thread["text"].split())
            title = escape(text)
            thread_id = escape(thread["id"], quote=True)
            option_label = escape(text if len(text) <= 100 else text[:97] + "...")
            topic_options.append(
                f'<option value="{thread_id}" title="{title}">{option_label}</option>'
            )
            url = escape(thread["url"], quote=True)
            heading = f'<h3><a href="{url}">{title}</a></h3>'
            markdown.extend([heading, ""])
            sections.append(
                f'<div class="gallery-thread" data-thread-id="{thread_id}">'
                + heading + _button("Copy thread link", thread["url"])
            )
            if not thread["photos"]:
                markdown.extend(["No reply images.", ""])
                sections.append("<p>No reply images.</p></div>")
                continue
            sections.append('<div class="grid">')
            for photo in thread["photos"]:
                style = photo.get("style", "Unknown")
                snippet = _photo_markdown(photo)
                markdown.extend([snippet, ""])
                username = escape(photo["username"])
                caption = escape(photo["text"])
                image = (
                    f'<img src="{escape(photo["url"], quote=True)}" '
                    f'alt="Photo {photo["number"]} by @{username}" loading="lazy" '
                    'referrerpolicy="no-referrer" />'
                    '<span class="unavailable" hidden>Image unavailable or expired. '
                    'Open the reply or rerun the action.</span>'
                    if photo["url"] else '<span>Image unavailable; open the reply.</span>'
                )
                sections.append(
                    f'<article data-photo-id="{escape(photo["id"], quote=True)}" '
                    f'data-photo-style="{escape(_style_category(style).casefold(), quote=True)}" '
                    f'data-image-url="{escape(photo["url"], quote=True)}" '
                    f'data-reply-url="{escape(photo["reply_url"], quote=True)}">'
                    f'<a class="thumbnail" href="{escape(photo["url"] or photo["reply_url"], quote=True)}">'
                    f'{image}</a><p><strong>@{username}</strong> '
                    f'&middot; photo {photo["number"]}</p>'
                    f'<p class="photo-style">{escape(style)}</p><p class="caption">{caption}</p>'
                    f'<p><a href="{escape(photo["reply_url"], quote=True)}">Reply</a>'
                    '</p>'
                    '<button type="button" class="favorite-toggle" '
                    'aria-pressed="false">Favorite</button>'
                    + _button(
                        "Copy image link" if photo["url"] else "Copy reply link",
                        photo["url"] or photo["reply_url"],
                    )
                    + _button("Copy Markdown", snippet)
                    + "</article>"
                )
            sections.append("</div></div>")
        sections.append("</section>")
        topic_options.append("</optgroup>")
    if not threads:
        markdown.extend(["No current topics.", ""])
        sections.append("<p>No current topics.</p>")
    md = "\n".join(markdown)
    links = "\n".join(
        photo["url"] or photo["reply_url"]
        for thread in threads for photo in thread["photos"]
    )
    controls = _button("Copy all Markdown", md, "all-markdown")
    controls += _button("Copy all image links", links, "all-links")
    template = (Path(__file__).with_name("reply_gallery.html")).read_text(encoding="utf-8")
    html = template.replace("{{TOPIC_OPTIONS}}", "\n".join(topic_options)).replace(
        "{{NOTE}}", escape(note)
    ).replace("{{STYLE_FILTERS}}", style_filters).replace(
        "{{CONTENT}}", controls + "\n".join(sections)
    )
    return html, md


def main() -> int:
    graph = json.loads((ROOT_DIR / "docs" / "graph.json").read_text(encoding="utf-8"))
    if graph["nodes"]:
        config.require("THREADS_ACCESS_TOKEN", config.THREADS_ACCESS_TOKEN)
    threads = collect_gallery(graph, load_cache(), load_styles(ROOT_DIR / "data" / "photo-styles.json"))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html, markdown = render_gallery(threads, generated)
    for relative, content in (
        (("docs", "reply-images.html"), html),
        (("docs", "reply-images.txt"), markdown),
        (("data", "reply-images.md"), markdown),
    ):
        path = ROOT_DIR.joinpath(*relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"Wrote reply image gallery for {len(threads)} current thread(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
