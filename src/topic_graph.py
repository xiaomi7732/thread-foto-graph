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
    for reply in thread.get("replies", []):
        parts.append(reply.get("text") or "")
        if reply.get("description"):
            parts.append(reply["description"])
    return "\n".join(p for p in parts if p)


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
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= length else text[: length - 1] + "…"


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
                f"{_snippet(thread.get('text', ''))}"
            )
            if thread.get("description"):
                lines.append(f"  - Photo: {thread['description']}")
            for reply in thread.get("replies", []):
                lines.append(
                    f"  - Reply by {reply.get('username', 'unknown')}: {_snippet(reply.get('text', ''))}"
                )
                if reply.get("description"):
                    lines.append(f"    - Photo: {reply['description']}")
            nodes.append(
                {
                    "id": thread["id"],
                    "label": topic_label,
                    "group": cid,
                    "snippet": _snippet(thread.get("text", ""), 60),
                    "permalink": thread.get("permalink", ""),
                    "reply_count": len(thread.get("replies", [])),
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
