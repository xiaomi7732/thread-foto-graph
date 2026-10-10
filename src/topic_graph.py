"""Builds a lightweight topic graph from today/yesterday's threads.

Each root thread (with its replies' text and photo descriptions folded in)
becomes a document. Threads are grouped into topics by TF-IDF cosine
similarity (union-find over a similarity threshold, so no fixed topic count
is required at this small daily scale).

Produces:
  - data/topics.md   compact text tree for Muse to read instead of photos
  - docs/graph.json  node/edge data for the GitHub Pages visual graph
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

SIMILARITY_THRESHOLD = 0.2
TOP_KEYWORDS = 4

ROOT_DIR = Path(__file__).resolve().parent.parent
TOPICS_MD_PATH = ROOT_DIR / "data" / "topics.md"
GRAPH_JSON_PATH = ROOT_DIR / "docs" / "graph.json"


def _thread_document(thread: dict) -> str:
    parts = [thread.get("text") or ""]
    if thread.get("description"):
        parts.append(thread["description"])
    for reply in _iter_replies(thread.get("replies", [])):
        parts.append(reply.get("text") or "")
        if reply.get("description"):
            parts.append(reply["description"])
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


HAS_PHOTO_TYPES = ("IMAGE", "CAROUSEL_ALBUM")


def _photo_line(item: dict) -> str | None:
    """Returns the Photo line for this thread/reply, or None if it has no image.

    Always emitted when media_type indicates a photo, even if Gemini hasn't
    described it yet (e.g. daily quota hit) -- so "no Photo line" unambiguously
    means "no photo", never "photo not described yet".
    """
    if item.get("media_type") not in HAS_PHOTO_TYPES:
        return None
    return item.get("description") or "(photo pending description - will be described on a future run)"



def _render_reply_lines(reply: dict, depth: int) -> list[str]:
    """Renders one reply plus all its nested replies, indented by depth so the
    real reply-to-reply hierarchy is visible (not flattened)."""
    indent = "  " * (depth + 1)
    reply_link = f" ([link]({reply['permalink']}))" if reply.get("permalink") else ""
    out = [
        f"{indent}- Reply by {reply.get('username', 'unknown')}: "
        f"{_snippet(reply.get('text', ''), length=None)}{reply_link}"
    ]
    reply_photo = _photo_line(reply)
    if reply_photo:
        out.append(f"{indent}  - Photo: {reply_photo}")
    for child in reply.get("replies", []):
        out.extend(_render_reply_lines(child, depth + 1))
    return out


def _reply_node(reply: dict) -> dict:
    """Recursively serializes a reply (and its nested replies) for graph.json."""
    return {
        "username": reply.get("username", "unknown"),
        "text": _snippet(reply.get("text", ""), length=None),
        "permalink": reply.get("permalink", ""),
        "photo": _photo_line(reply),
        "replies": [_reply_node(child) for child in reply.get("replies", [])],
    }


def build_topic_graph(threads: list[dict]) -> dict:
    """Returns the full graph structure and writes topics.md + graph.json."""
    TOPICS_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not threads:
        TOPICS_MD_PATH.write_text(
            "# Thread Topics\n\n_No threads found in the lookback window._\n", encoding="utf-8"
        )
        GRAPH_JSON_PATH.write_text(json.dumps({"nodes": [], "edges": []}, indent=2))
        return {"nodes": [], "edges": []}

    docs = [_thread_document(t) for t in threads]
    cluster_ids, vectorizer, matrix = _cluster_threads(docs)

    topics: dict[int, list[int]] = {}
    for idx, cid in enumerate(cluster_ids):
        topics.setdefault(cid, []).append(idx)

    lines = ["# Thread Topics", ""]
    nodes, edges = [], []
    for cid, member_indices in sorted(topics.items()):
        keywords = _top_keywords(vectorizer, matrix, member_indices)
        topic_label = ", ".join(keywords) if keywords else f"topic-{cid}"
        lines.append(f"## Topic: {topic_label}")
        lines.append("")
        for idx in member_indices:
            thread = threads[idx]
            lines.append(
                f"- **Thread** [{thread['id']}]({thread.get('permalink', '')}): "
                f"{_snippet(thread.get('text', ''), length=None)}"
            )
            thread_photo = _photo_line(thread)
            if thread_photo:
                lines.append(f"  - Photo: {thread_photo}")
            for reply in thread.get("replies", []):
                lines.extend(_render_reply_lines(reply, depth=0))
            nodes.append(
                {
                    "id": thread["id"],
                    "label": topic_label,
                    "group": cid,
                    "snippet": _snippet(thread.get("text", ""), length=60),
                    "text": _snippet(thread.get("text", ""), length=None),
                    "permalink": thread.get("permalink", ""),
                    "photo": thread_photo,
                    "reply_count": len(list(_iter_replies(thread.get("replies", [])))),
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
