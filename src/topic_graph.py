"""Builds a lightweight topic graph from today/yesterday's threads.

Each root thread (with its replies' text and photo descriptions folded in)
becomes a document. Threads are grouped into topics by TF-IDF cosine
similarity (union-find over a similarity threshold, so no fixed topic count
is required at this small daily scale).

Produces:
  - data/needs-reply.md     flat list of replies that still need a reply
                            (photo replies), each a self-contained bullet
                            (own text/photo + one line of immediate parent
                            context + thread backlink) -- no topic grouping,
                            no tree to walk. This is the bulk of the data
                            (full photo descriptions), so it's split out from
                            needs-like.md to keep the like-watch's read small.
  - data/needs-like.md      flat list of replies that still need a like
                            (text-only replies, no photo description to
                            carry) -- a much smaller file, meant for a
                            worker/cron that only decides likes and has no
                            use for the reply-drafting bulk.
  - data/actions.md         combined convenience copy of both of the above,
                            in one file, for a human skimming everything at
                            once. Not meant to be the primary machine read --
                            prefer the two split files above for that.
  - data/topics.md          compact, actionable-first *structural* view for
                            Muse: thread summaries plus ONLY replies that
                            still need a response (config.THREADS_USERNAME
                            hasn't replied to them yet), with real nesting
                            preserved -- already-answered branches collapse
                            to a one-line count instead of being spelled out
                            (see _prune_for_action). Useful when the tree
                            shape/order matters, but actions.md is the
                            smaller day-to-day read.
  - data/threads/<id>.md    full, unpruned per-thread conversation (every
                            reply, answered or not) for on-demand drill-down;
                            not loaded by Muse by default.
  - docs/graph.json         node/edge data for the GitHub Pages visual graph
                            (full archive, each reply flagged needs_response).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import config

SIMILARITY_THRESHOLD = 0.2
TOP_KEYWORDS = 4

ROOT_DIR = Path(__file__).resolve().parent.parent
ACTIONS_MD_PATH = ROOT_DIR / "data" / "actions.md"
NEEDS_REPLY_MD_PATH = ROOT_DIR / "data" / "needs-reply.md"
NEEDS_LIKE_MD_PATH = ROOT_DIR / "data" / "needs-like.md"
TOPICS_MD_PATH = ROOT_DIR / "data" / "topics.md"
THREADS_DIR = ROOT_DIR / "data" / "threads"
HISTORY_DIR = ROOT_DIR / "data" / "history"
GRAPH_JSON_PATH = ROOT_DIR / "docs" / "graph.json"


def _thread_document(thread: dict) -> str:
    parts = [thread.get("text") or ""]
    parts.extend(d for d in (thread.get("photo_descriptions") or []) if d)
    for reply in _iter_replies(thread.get("replies", [])):
        parts.append(reply.get("text") or "")
        parts.extend(d for d in (reply.get("photo_descriptions") or []) if d)
    return "\n".join(p for p in parts if p)


def _iter_replies(replies: list[dict]):
    """Depth-first walk over a nested reply tree -- mirrors
    threads_client.iter_replies, duplicated here to keep this module
    independent of the API client (it also accepts synthetic/test data).
    """
    for reply in replies:
        yield reply
        yield from _iter_replies(reply.get("replies", []))


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _cluster_threads(docs: list[str]):
    vectorizer = TfidfVectorizer(stop_words="english", max_features=2000)
    matrix = vectorizer.fit_transform(docs)
    sim = cosine_similarity(matrix)

    uf = UnionFind(len(docs))
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            if sim[i, j] >= SIMILARITY_THRESHOLD:
                uf.union(i, j)

    roots = [uf.find(i) for i in range(len(docs))]
    unique_roots = sorted(set(roots))
    remap = {r: idx for idx, r in enumerate(unique_roots)}
    cluster_ids = [remap[r] for r in roots]
    return cluster_ids, vectorizer, matrix


def _top_keywords(vectorizer: TfidfVectorizer, matrix, indices: list[int]) -> list[str]:
    sub = matrix[indices]
    scores = sub.sum(axis=0).A1
    feature_names = vectorizer.get_feature_names_out()
    top_idx = scores.argsort()[::-1][:TOP_KEYWORDS]
    return [feature_names[i] for i in top_idx if scores[i] > 0]


def _snippet(text: str, length: int = 80) -> str:
    """Collapses whitespace only. Truncation is reserved for the small
    visual-graph node labels (docs/graph.json) -- topics.md is read by Muse
    instead of photos, so its whole point is to carry full context, not a
    truncated preview.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if length is None:
        return text
    return text if len(text) <= length else text[: length - 1] + "…"


PENDING_PHOTO_TEXT = "(photo pending description - will be described on a future run)"


def _photo_lines(item: dict) -> list[str]:
    """Returns one entry per photo in this thread/reply (a CAROUSEL_ALBUM can
    have several -- see threads_client.photo_items), or [] if it has none.

    Always one entry per actual photo, even if Gemini hasn't described it yet
    (e.g. daily quota hit) -- so an empty list unambiguously means "no
    photos", never "photos not described yet".
    """
    descriptions = item.get("photo_descriptions")
    if not descriptions:
        return []
    return [d if d else PENDING_PHOTO_TEXT for d in descriptions]



def _needs_response(reply: dict) -> bool:
    """A reply needs a response if it's not the owner's own reply, and the
    owner hasn't directly replied to it yet (no direct child authored by
    config.THREADS_USERNAME). This is the actionable signal Muse uses to
    decide what still needs a suggested reply.
    """
    if reply.get("username") == config.THREADS_USERNAME:
        return False
    return not any(
        child.get("username") == config.THREADS_USERNAME
        for child in reply.get("replies", [])
    )


def _action_category(reply: dict) -> str:
    """Categorizes an unanswered reply into what it's actually worth doing:
    a reply with a photo is worth a thoughtful written reply, while a
    plain-text reply usually just warrants a like rather than a full
    suggested response.
    """
    return "reply" if _photo_lines(reply) else "like"


def _prune_for_action(replies: list[dict]) -> tuple[list[dict], int]:
    """Recursively prunes a nested reply tree down to only what's still
    actionable, for the compact data/topics.md view.

    A reply is kept (annotated with '_needs_response' and '_answered_overflow')
    if it itself needs a response, OR if any of its descendants still need a
    response (kept as context so Muse can see what it's replying to/within).
    A reply -- and its entire subtree -- is dropped entirely only when every
    reply in it is already resolved (the owner's own replies, or replies the
    owner has already responded to, with nothing unanswered underneath).

    Returns (kept_replies, dropped_count) where dropped_count is how many
    reply nodes were dropped at this level (for a "+N already answered" note).
    Full detail for dropped replies is never lost -- it's still written out
    in full to data/threads/<id>.md.
    """
    kept = []
    dropped = 0
    for reply in replies:
        child_kept, child_dropped = _prune_for_action(reply.get("replies", []))
        own_needs = _needs_response(reply)
        if own_needs or child_kept:
            pruned_reply = dict(reply)
            pruned_reply["replies"] = child_kept
            pruned_reply["_needs_response"] = own_needs
            pruned_reply["_answered_overflow"] = child_dropped
            kept.append(pruned_reply)
        else:
            dropped += 1 + child_dropped
    return kept, dropped


def _render_reply_lines(reply: dict, depth: int, show_action_marker: bool = False) -> list[str]:
    """Renders one reply plus all its nested replies, indented by depth so the
    real reply-to-reply hierarchy is visible (not flattened).

    When show_action_marker is set (compact data/topics.md view), replies
    still needing a response are prefixed **[NEEDS REPLY]** (has a photo --
    worth a thoughtful reply) or **[NEEDS LIKE]** (text-only -- a like is
    usually enough) so Muse doesn't have to re-derive that itself, and a
    kept-but-already-resolved reply with further answered descendants gets a
    one-line overflow note instead of those descendants being spelled out.
    """
    indent = "  " * (depth + 1)
    reply_link = f" ([link]({reply['permalink']}))" if reply.get("permalink") else ""
    marker = ""
    if show_action_marker and reply.get("_needs_response"):
        marker = "**[NEEDS REPLY]** " if _action_category(reply) == "reply" else "**[NEEDS LIKE]** "
    out = [
        f"{indent}- {marker}Reply by {reply.get('username', 'unknown')}: "
        f"{_snippet(reply.get('text', ''), length=None)}{reply_link}"
    ]
    for photo in _photo_lines(reply):
        out.append(f"{indent}  - Photo: {photo}")
    overflow = reply.get("_answered_overflow", 0)
    if overflow:
        out.append(f"{indent}  _(+{overflow} more repl{'y' if overflow == 1 else 'ies'} already answered under this, see archive)_")
    for child in reply.get("replies", []):
        out.extend(_render_reply_lines(child, depth + 1, show_action_marker=show_action_marker))
    return out


def _reply_node(reply: dict) -> dict:
    """Recursively serializes a reply (and its nested replies) for graph.json."""
    needs_response = _needs_response(reply)
    return {
        "username": reply.get("username", "unknown"),
        "text": _snippet(reply.get("text", ""), length=None),
        "permalink": reply.get("permalink", ""),
        "photos": _photo_lines(reply),
        "needs_response": needs_response,
        "action": _action_category(reply) if needs_response else None,
        "replies": [_reply_node(child) for child in reply.get("replies", [])],
    }


def _flatten_actionable(replies: list[dict], parent_text: str | None = None):
    """Depth-first walk yielding (reply, parent_text) for every reply still
    needing a response, anywhere in the tree -- flattened, no indentation/
    ancestor chain kept. parent_text is a short snippet of the reply's
    *immediate* parent (None if the parent is the thread root itself, since
    the thread summary already gives that context) -- just enough for Muse
    to know what a nested reply is responding to without walking the tree.
    """
    for reply in replies:
        if _needs_response(reply):
            yield reply, parent_text
        own_text = _snippet(reply.get("text", ""), length=60) if reply.get("text") else "(photo reply, no text)"
        yield from _flatten_actionable(reply.get("replies", []), own_text)


def _render_action_item(reply: dict, parent_text: str | None) -> list[str]:
    """Renders one actionable reply as a self-contained bullet -- own
    text/photo(s) plus a one-line parent-context note if nested. The thread
    it belongs to is NOT repeated here (that would duplicate the same
    snippet+link on every item under a busy thread) -- see
    _render_action_section, which groups items under one thread header.
    """
    reply_link = f" ([link]({reply['permalink']}))" if reply.get("permalink") else ""
    out = [f"- **{reply.get('username', 'unknown')}**: {_snippet(reply.get('text', ''), length=None)}{reply_link}"]
    for photo in _photo_lines(reply):
        out.append(f"  - Photo: {photo}")
    if parent_text:
        out.append(f"  - _Replying to: {parent_text}_")
    return out


def _group_by_thread(items: list[tuple[dict, dict, str | None]]) -> list[tuple[dict, list[tuple[dict, str | None]]]]:
    """Groups (thread, reply, parent_text) tuples by thread, preserving the
    order threads first appear in, so each thread's header is written once
    instead of once per reply."""
    order: list[dict] = []
    grouped: dict[str, list[tuple[dict, str | None]]] = {}
    for thread, reply, parent_text in items:
        key = thread["id"]
        if key not in grouped:
            grouped[key] = []
            order.append(thread)
        grouped[key].append((reply, parent_text))
    return [(thread, grouped[thread["id"]]) for thread in order]


def _render_action_section(items: list[tuple[dict, dict, str | None]]) -> list[str]:
    """Renders one '## Needs Reply'/'## Needs Like' section: items grouped
    under a single '### <thread>' header per thread (written once, not once
    per reply) to avoid repeating the same thread snippet+link dozens of
    times for a busy thread."""
    lines = []
    for thread, entries in _group_by_thread(items):
        thread_link = f" ([thread]({thread.get('permalink', '')}))" if thread.get("permalink") else ""
        lines.append(f"### {_snippet(thread.get('text', ''), length=60)}{thread_link}")
        for reply, parent_text in entries:
            lines.extend(_render_action_item(reply, parent_text))
        lines.append("")
    return lines


def _split_action_items(
    threads: list[dict],
) -> tuple[list[tuple[dict, dict, str | None]], list[tuple[dict, dict, str | None]]]:
    """Walks every thread's actionable replies and splits them into
    (reply_items, like_items) -- shared by all three action-list renderers
    so the categorization logic lives in exactly one place."""
    reply_items: list[tuple[dict, dict, str | None]] = []
    like_items: list[tuple[dict, dict, str | None]] = []
    for thread in threads:
        for reply, parent_text in _flatten_actionable(thread.get("replies", [])):
            entry = (thread, reply, parent_text)
            if _action_category(reply) == "reply":
                reply_items.append(entry)
            else:
                like_items.append(entry)
    return reply_items, like_items


BROWSER_ONLY_NOTE = (
    "_This needs a browser session to actually post -- the Threads API has "
    "no endpoint to like a reply (returns HTTP 400), so drafting here "
    "doesn't mean \"API-actionable\": every item below still ends with a "
    "human/browser posting it._"
)


def _render_needs_reply_md(reply_items: list[tuple[dict, dict, str | None]]) -> str:
    """Builds data/needs-reply.md -- just the photo replies still awaiting a
    reply. This carries the bulk of the data (full photo descriptions), so
    it's kept separate from needs-like.md: a worker that only decides likes
    shouldn't have to load all this to get to its much smaller list."""
    lines = [
        "# Needs Reply",
        "",
        "_Flat list of replies still awaiting a reply -- no topic grouping, "
        "no already-answered branches, no reply tree to walk, and each "
        "thread's link shown only once. See data/topics.md for fuller "
        "structural/thread context, or data/threads/<id>.md for the full "
        "conversation. See data/needs-like.md for the (much smaller) "
        "text-only replies that just need a like._",
        "",
        BROWSER_ONLY_NOTE,
        "",
        f"## Needs Reply ({len(reply_items)})",
        "",
    ]
    if not reply_items:
        lines.append("_Nothing -- all photo replies answered._")
    else:
        lines.extend(_render_action_section(reply_items))
    return "\n".join(lines) + "\n"


def _render_needs_like_md(like_items: list[tuple[dict, dict, str | None]]) -> str:
    """Builds data/needs-like.md -- just the text-only replies still
    awaiting a like. Deliberately tiny (no photo descriptions to carry) so a
    like-only worker/cron can load this instead of the much bigger
    needs-reply.md."""
    lines = [
        "# Needs Like",
        "",
        "_Flat list of text-only replies still awaiting a like -- no topic "
        "grouping, each thread's link shown only once. See "
        "data/needs-reply.md for the (bigger) photo replies that need an "
        "actual reply._",
        "",
        BROWSER_ONLY_NOTE,
        "",
        f"## Needs Like ({len(like_items)})",
        "",
    ]
    if not like_items:
        lines.append("_Nothing -- all text replies answered._")
    else:
        for thread, entries in _group_by_thread(like_items):
            thread_link = f" ([thread]({thread.get('permalink', '')}))" if thread.get("permalink") else ""
            lines.append(f"### {_snippet(thread.get('text', ''), length=60)}{thread_link}")
            for reply, _parent_text in entries:
                reply_link = f" ([link]({reply['permalink']}))" if reply.get("permalink") else ""
                lines.append(
                    f"- {reply.get('username', 'unknown')}: {_snippet(reply.get('text', ''), length=None)}{reply_link}"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


def _render_action_list(needs_reply_md: str, needs_like_md: str) -> str:
    """Builds data/actions.md -- a combined convenience copy of
    needs-reply.md + needs-like.md in one file, for a human skimming
    everything at once. Prefer the two split files for machine reads: this
    one re-bundles the full (large) reply section even for a consumer that
    only wants the like list."""
    lines = [
        "# Action List",
        "",
        "_Combined convenience copy of data/needs-reply.md + "
        "data/needs-like.md, for skimming everything in one place. If "
        "you're building a worker that only needs one of the two (e.g. a "
        "like-only cron), read that file directly instead -- it's much "
        "smaller._",
        "",
    ]
    # Re-use each file's own body, dropping its top-level title line so the
    # combined file reads as one document with '##' section headers.
    for md in (needs_reply_md, needs_like_md):
        body_lines = md.splitlines()[2:]  # drop "# Needs X" + blank line
        lines.extend(body_lines)
        lines.append("")
    return "\n".join(lines)


def _thread_header_lines(thread: dict) -> tuple[list[str], list[str]]:
    """Returns (header_lines, thread_photo_lines) shared by both the compact
    and archive renderings of a thread -- the root post is always shown in
    full either way, it's only the reply tree that differs."""
    header = [
        f"- **Thread** [{thread['id']}]({thread.get('permalink', '')}): "
        f"{_snippet(thread.get('text', ''), length=None)}"
    ]
    photos = _photo_lines(thread)
    return header, photos


def _render_archive_thread(thread: dict) -> str:
    """Renders the full, unpruned conversation for one thread -- every reply,
    answered or not -- for data/threads/<id>.md (on-demand drill-down)."""
    header, photos = _thread_header_lines(thread)
    lines = [f"# Thread {thread['id']}", ""]
    lines.extend(header)
    for photo in photos:
        lines.append(f"  - Photo: {photo}")
    for reply in thread.get("replies", []):
        lines.extend(_render_reply_lines(reply, depth=0, show_action_marker=False))
    return "\n".join(lines) + "\n"


def _write_dated_history(needs_reply_md: str, needs_like_md: str) -> None:
    """Writes a dated snapshot of each of today's needs-reply/needs-like
    content to data/history/ (e.g. needs-reply-2026-01-15.md), in addition
    to the live needs-reply.md/needs-like.md files. Overwritten every run
    for the current UTC date -- since the pipeline runs hourly, each day's
    file simply reflects its latest state until the date rolls over, at
    which point it becomes a frozen historical record of that day. This is
    purely additive: it never changes what the live files contain or how
    Muse should read them day-to-day."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    (HISTORY_DIR / f"needs-reply-{today}.md").write_text(needs_reply_md, encoding="utf-8")
    (HISTORY_DIR / f"needs-like-{today}.md").write_text(needs_like_md, encoding="utf-8")


def build_topic_graph(threads: list[dict]) -> dict:
    """Returns the full graph structure and writes:
      - data/needs-reply.md     flat list of photo replies needing a reply
      - data/needs-like.md      flat list of text-only replies needing a like
      - data/actions.md         combined convenience copy of the above two
      - data/topics.md          compact, actionable-first structural view
      - data/threads/<id>.md    full per-thread archive
      - data/history/needs-reply-<date>.md / needs-like-<date>.md
                                 dated daily snapshots of the two above,
                                 for historical drill-down (what did the
                                 action list look like on a given day)
      - docs/graph.json         full visual-graph archive
    """
    TOPICS_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not threads:
        empty_reply_md = "# Needs Reply\n\n_No threads found in the lookback window._\n"
        empty_like_md = "# Needs Like\n\n_No threads found in the lookback window._\n"
        NEEDS_REPLY_MD_PATH.write_text(empty_reply_md, encoding="utf-8")
        NEEDS_LIKE_MD_PATH.write_text(empty_like_md, encoding="utf-8")
        ACTIONS_MD_PATH.write_text(
            "# Action List\n\n_No threads found in the lookback window._\n", encoding="utf-8"
        )
        TOPICS_MD_PATH.write_text(
            "# Thread Topics\n\n_No threads found in the lookback window._\n", encoding="utf-8"
        )
        _write_dated_history(empty_reply_md, empty_like_md)
        GRAPH_JSON_PATH.write_text(json.dumps({"nodes": [], "edges": []}, indent=2))
        return {"nodes": [], "edges": []}

    reply_items, like_items = _split_action_items(threads)
    needs_reply_md = _render_needs_reply_md(reply_items)
    needs_like_md = _render_needs_like_md(like_items)
    NEEDS_REPLY_MD_PATH.write_text(needs_reply_md, encoding="utf-8")
    NEEDS_LIKE_MD_PATH.write_text(needs_like_md, encoding="utf-8")
    _write_dated_history(needs_reply_md, needs_like_md)
    ACTIONS_MD_PATH.write_text(_render_action_list(needs_reply_md, needs_like_md), encoding="utf-8")

    docs = [_thread_document(t) for t in threads]
    cluster_ids, vectorizer, matrix = _cluster_threads(docs)

    topics: dict[int, list[int]] = {}
    for idx, cid in enumerate(cluster_ids):
        topics.setdefault(cid, []).append(idx)

    lines = [
        "# Thread Topics",
        "",
        "_Compact, actionable-first view: only replies still awaiting a "
        "response are shown in full, marked **[NEEDS REPLY]** (has a photo -- "
        "worth a thoughtful reply) or **[NEEDS LIKE]** (text-only -- a like "
        "is usually enough). Already-answered replies collapse to a count -- "
        "see the linked archive for the complete conversation._",
        "",
    ]
    nodes, edges = [], []
    for cid, member_indices in sorted(topics.items()):
        keywords = _top_keywords(vectorizer, matrix, member_indices)
        topic_label = ", ".join(keywords) if keywords else f"topic-{cid}"
        lines.append(f"## Topic: {topic_label}")
        lines.append("")
        for idx in member_indices:
            thread = threads[idx]
            archive_link = f"threads/{thread['id']}.md"

            # Write the full, unpruned archive for this thread regardless of
            # the topic-cluster loop structure (one file per thread).
            (THREADS_DIR / f"{thread['id']}.md").write_text(
                _render_archive_thread(thread), encoding="utf-8"
            )

            header, thread_photos = _thread_header_lines(thread)
            lines.extend(header)
            for photo in thread_photos:
                lines.append(f"  - Photo: {photo}")

            total_replies = len(list(_iter_replies(thread.get("replies", []))))
            pruned_replies, dropped = _prune_for_action(thread.get("replies", []))
            if total_replies == 0:
                pass  # no replies at all -- nothing more to render
            elif not pruned_replies:
                lines.append(
                    f"  - All {total_replies} repl{'y' if total_replies == 1 else 'ies'} "
                    f"already answered. ✅ ([full thread]({archive_link}))"
                )
            else:
                for reply in pruned_replies:
                    lines.extend(_render_reply_lines(reply, depth=0, show_action_marker=True))
                if dropped:
                    lines.append(
                        f"  _(+{dropped} more repl{'y' if dropped == 1 else 'ies'} elsewhere in "
                        f"this thread already answered, see [full thread]({archive_link}))_"
                    )
                else:
                    lines.append(f"  ([full thread]({archive_link}))")

            nodes.append(
                {
                    "id": thread["id"],
                    "label": topic_label,
                    "group": cid,
                    "snippet": _snippet(thread.get("text", ""), length=60),
                    "text": _snippet(thread.get("text", ""), length=None),
                    "permalink": thread.get("permalink", ""),
                    "photos": thread_photos,
                    "reply_count": total_replies,
                    "replies": [_reply_node(reply) for reply in thread.get("replies", [])],
                }
            )
        lines.append("")
        if len(member_indices) > 1:
            anchor = threads[member_indices[0]]["id"]
            for idx in member_indices[1:]:
                edges.append({"from": anchor, "to": threads[idx]["id"]})

    TOPICS_MD_PATH.write_text("\n".join(lines), encoding="utf-8")

    graph = {"nodes": nodes, "edges": edges}
    GRAPH_JSON_PATH.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return graph
