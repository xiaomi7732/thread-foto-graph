"""API-only pilot feed for answers to our replies on other people's posts."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from . import config, threads_auth, threads_client, topic_graph

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ITEM_FIELDS = (
    "id,text,username,permalink,timestamp,has_replies,media_type,"
    "children{id,media_type,media_url},hide_status,is_liked,replied_to{id}"
)
ANSWER_FIELDS = ITEM_FIELDS + ",replies.limit(100){" + ITEM_FIELDS + "}"
ROOT_FIELDS = "id,text,username,permalink,timestamp,has_replies,root_post{id,username,permalink}"


class ObservationError(RuntimeError):
    pass


class RunAborted(ObservationError):
    pass


def _handle(value: str) -> str:
    return value.lstrip("@").casefold()


class Client:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            time.sleep(random.uniform(2, 4))
            try:
                response = self.session.get(
                    f"{threads_client.BASE_URL}/{path}",
                    params=params,
                    timeout=30,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ObservationError("NETWORK_ERROR") from None
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            error = data.get("error") or {}
            code = error.get("code")
            status = response.status_code
            if status == 429 or code in {4, 17, 32, 613}:
                raise RunAborted("RATE_LIMITED")
            if status == 401 or code == 190:
                raise RunAborted("SESSION_EXPIRED")
            if status == 403 or code == 10:
                raise RunAborted("INSUFFICIENT_ACCESS")
            if status >= 500 or error.get("is_transient"):
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
            if status != 200 or error:
                raise ObservationError(f"API_ERROR_HTTP_{status}_CODE_{code}")
            if not data:
                raise ObservationError("INVALID_API_RESPONSE")
            return data
        raise ObservationError("RETRIES_EXHAUSTED")

    def edge(self, media_id: str, node: dict, fields: str) -> list[dict]:
        items = {}
        cursors = set()
        while True:
            edge = node.get("replies")
            if edge is None:
                if node.get("has_replies") is False:
                    break
                raise ObservationError("REPLY_EDGE_UNAVAILABLE")
            if not isinstance(edge, dict) or not isinstance(edge.get("data"), list):
                raise ObservationError("INVALID_REPLY_EDGE")
            for item in edge["data"]:
                if not isinstance(item, dict) or not item.get("id"):
                    raise ObservationError("INVALID_REPLY")
                items[item["id"]] = item
            paging = edge.get("paging") or {}
            if not paging.get("next"):
                break
            cursor = paging.get("cursors", {}).get("after")
            if not cursor or cursor in cursors:
                raise ObservationError("INCOMPLETE_PAGINATION")
            cursors.add(cursor)
            node = self.get(media_id, {
                "fields": f"id,has_replies,replies.after({cursor}).limit(100){{{fields}}}",
            })
        if not items and node.get("has_replies") is not False:
            raise ObservationError("EMPTY_REPLY_EDGE_WITHOUT_NEGATIVE_CONFIRMATION")
        return list(items.values())

    def answers(self, media_id: str) -> tuple[dict, list[dict]]:
        node = self.get(media_id, {
            "fields": ROOT_FIELDS + ",replies.limit(100){" + ANSWER_FIELDS + "}",
        })
        if str(node.get("id")) != media_id:
            raise ObservationError("MEDIA_ID_MISMATCH")
        answers = self.edge(media_id, node, ANSWER_FIELDS)
        for answer in answers:
            if (answer.get("replied_to") or {}).get("id") != media_id:
                raise ObservationError("ANSWER_PARENT_UNAVAILABLE_OR_MISMATCHED")
            if not answer.get("username") or not answer.get("timestamp"):
                raise ObservationError("ANSWER_METADATA_UNAVAILABLE")
            children = self.edge(answer["id"], answer, ITEM_FIELDS)
            if any(
                child.get("replied_to") and child["replied_to"].get("id") != answer["id"]
                for child in children
            ):
                raise ObservationError("CHILD_PARENT_MISMATCH")
            answer["replies"] = children
        return node, answers

    def discover(self, username: str, cutoff: dt.datetime, now: dt.datetime) -> list[dict]:
        params = {
            "fields": ROOT_FIELDS,
            "since": int(cutoff.timestamp()),
            "until": int(now.timestamp()),
            "limit": 100,
        }
        roots = {}
        replies = {}
        cursors = set()
        while True:
            page = self.get("me/replies", params)
            if not isinstance(page.get("data"), list):
                raise RunAborted("INVALID_DISCOVERY_RESPONSE")
            for reply in page["data"]:
                if not all(reply.get(key) for key in ("id", "timestamp", "username")):
                    raise RunAborted("INCOMPLETE_DISCOVERY_METADATA")
                if _handle(reply["username"]) != _handle(username):
                    raise RunAborted("DISCOVERY_ACCOUNT_MISMATCH")
                try:
                    posted = threads_client.parse_timestamp(reply["timestamp"])
                except ValueError:
                    raise RunAborted("INVALID_DISCOVERY_TIMESTAMP") from None
                if not cutoff <= posted <= now:
                    continue
                root = reply.get("root_post") or {}
                if root.get("id") and not root.get("username"):
                    if root["id"] not in roots:
                        try:
                            roots[root["id"]] = self.get(root["id"], {
                                "fields": "id,username,permalink",
                            })
                        except RunAborted:
                            raise
                        except ObservationError as exc:
                            roots[root["id"]] = {
                                "id": root["id"], "lookup_error": str(exc),
                            }
                    root = roots[root["id"]]
                if root.get("username") and _handle(root["username"]) == _handle(username):
                    continue
                reply["root_post"] = root
                replies[reply["id"]] = reply
            paging = page.get("paging") or {}
            if not paging.get("next"):
                break
            cursor = paging.get("cursors", {}).get("after")
            if not cursor or cursor in cursors:
                raise RunAborted("INCOMPLETE_DISCOVERY_PAGINATION")
            cursors.add(cursor)
            params["after"] = cursor
        return sorted(
            replies.values(),
            key=lambda reply: threads_client.parse_timestamp(reply["timestamp"]),
            reverse=True,
        )


def _answer_record(answer: dict, username: str, target: str | None, seen: set[str]) -> dict:
    children = answer["replies"]
    own = _handle(answer["username"]) == _handle(username)
    responded = any(_handle(child.get("username", "")) == _handle(username) for child in children)
    classified = dict(answer)
    if threads_client.photo_items(answer):
        classified["photo_descriptions"] = ["Photo present; open the answer to view it."]
    action = None
    if not own and not responded and not topic_graph._is_hidden(answer):
        category = topic_graph._action_category(classified)
        if category != "like" or answer.get("is_liked") is not True:
            action = category
    return {
        "id": answer["id"],
        "author": answer["username"],
        "is_target_author": _handle(answer["username"]) == _handle(target) if target else None,
        "text": answer.get("text") or "",
        "timestamp": answer["timestamp"],
        "permalink": answer.get("permalink"),
        "media_type": answer.get("media_type"),
        "is_new": not own and answer["id"] not in seen,
        "action": action,
        "is_liked": answer.get("is_liked"),
        "child_reply_count": len(children),
        "child_reply_handles": sorted({child.get("username", "unknown") for child in children}),
    }


def collect(
    client: Client,
    username: str,
    previous: dict,
    now: dt.datetime,
    lookback_days: int,
    max_replies: int,
) -> dict:
    identity = client.get("me", {"fields": "id,username"})
    if _handle(identity.get("username", "")) != _handle(username):
        raise RunAborted("ACCOUNT_MISMATCH")
    discovered = client.discover(username, now - dt.timedelta(days=lookback_days), now)
    selected = discovered[:max_replies]
    old = {item["reply_id"]: item for item in previous.get("replies", [])}
    seen = set(previous.get("seen_answer_ids", []))
    for item in old.values():
        seen.update(answer["id"] for answer in item.get("answers", []))
    observed = set(seen)
    results = []
    for index, reply in enumerate(selected, 1):
        reply_id = reply["id"]
        root = reply.get("root_post") or {}
        target = root.get("username")
        prior = old.get(reply_id, {})
        last_success = prior.get("last_successful")
        if prior and prior.get("state") != "unreachable":
            prior_likes = prior.get("like_count")
            if prior_likes is None and last_success:
                prior_likes = last_success.get("like_count")
            last_success = {
                key: prior.get(key) for key in ("checked_at", "state", "like_count", "answer_count")
            }
            last_success["like_count"] = prior_likes
        result = {
            "reply_id": reply_id,
            "reply_permalink": reply.get("permalink"),
            "text": reply.get("text") or "",
            "posted_at": reply["timestamp"],
            "target_author": target,
            "target_post_url": root.get("permalink"),
            "target_metadata_available": bool(target),
            "checked_at": now.isoformat(),
            "state": "unreachable",
            "like_count": None,
            "like_delta": None,
            "likes_is_new": False,
            "answer_count": None,
            "answers": [],
        }
        if root.get("lookup_error"):
            result["target_metadata_error"] = root["lookup_error"]
        try:
            node, answers = client.answers(reply_id)
            result["text"] = node.get("text") or result["text"]
            result["answers"] = [_answer_record(a, username, target, seen) for a in answers]
            external = [a for a in result["answers"] if _handle(a["author"]) != _handle(username)]
            result["answer_count"] = len(external)
            result["state"] = (
                "answered_by_author" if any(a["is_target_author"] for a in external)
                else "answered_by_other" if external and target
                else "answered" if external
                else "no_answer"
            )
            observed.update(a["id"] for a in result["answers"])
            try:
                insights = client.get(reply_id + "/insights", {"metric": "likes"})
                likes = next(
                    (
                        metric.get("values", [{}])[0].get("value")
                        for metric in insights.get("data", [])
                        if metric.get("name") == "likes" and metric.get("values")
                    ),
                    None,
                )
                if type(likes) is not int or likes < 0:
                    raise ObservationError("LIKE_COUNT_UNAVAILABLE")
                result["like_count"] = likes
                prior_likes = prior.get("like_count")
                if prior_likes is None and last_success:
                    prior_likes = last_success.get("like_count")
                if prior_likes is not None:
                    result["like_delta"] = likes - prior_likes
                    result["likes_is_new"] = likes > prior_likes
            except RunAborted:
                raise
            except ObservationError as exc:
                result["like_error"] = str(exc)
                if last_success:
                    result["last_successful"] = last_success
        except RunAborted:
            raise
        except ObservationError as exc:
            result["error"] = str(exc)
            if last_success:
                result["last_successful"] = last_success
        results.append(result)
        print(f"[{index}/{len(selected)}] {reply_id}: {result['state']}", flush=True)
    if results and all(item["state"] == "unreachable" for item in results):
        raise RunAborted("ALL_ITEMS_UNREACHABLE")
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "run_id": os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex,
        "account": username,
        "status": "partial" if any(
            r.get("error") or r.get("like_error") or r.get("target_metadata_error")
            for r in results
        ) else "ok",
        "lookback_days": lookback_days,
        "max_replies": max_replies,
        "discovered_count": len(discovered),
        "capped": len(discovered) > max_replies,
        "seen_answer_ids": sorted(observed),
        "replies": results,
    }


def _text(value: str) -> str:
    text = html.escape(" ".join((value or "").split()), quote=False)
    for char in "\\`*_{}[]|":
        text = text.replace(char, "\\" + char)
    return text


def _link(label: str, url: str | None) -> str:
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or parsed.hostname not in {"www.threads.com", "www.threads.net", "threads.com", "threads.net"}:
        return label + " (link unavailable)"
    return f"[{label}](<{html.escape(url, quote=True)}>)"


def render(snapshot: dict) -> str:
    replies = snapshot["replies"]
    entries = [(reply, answer) for reply in replies for answer in reply["answers"]]
    entries.sort(key=lambda pair: threads_client.parse_timestamp(pair[1]["timestamp"]), reverse=True)
    generated = threads_client.parse_timestamp(snapshot["generated_at"]).astimezone(ZoneInfo("America/Los_Angeles"))
    lines = [
        "# Needs Outward",
        "",
        f"Generated {generated.isoformat()} for @{_text(snapshot['account'])}. "
        f"{len(replies)} of {snapshot['discovered_count']} outward candidates checked "
        f"(last {snapshot['lookback_days']} days; limit {snapshot['max_replies']}).",
        "",
        "_API-only pilot. Full answer text; no photo descriptions or Threads writes. "
        "Target authors are unknown when the API omits parent metadata; "
        "unknown attribution is never treated as an author answer. "
        "New means first observed since the previous published snapshot, not unread._",
        "",
    ]
    if snapshot["capped"]:
        lines.extend(["**Scope capped: older outward candidates were not checked.**", ""])
    for title, matches in (
        ("New Answers", [(r, a) for r, a in entries if a["is_new"]]),
        ("Needs Reply", [(r, a) for r, a in entries if a["action"] == "reply"]),
        ("Needs Like", [(r, a) for r, a in entries if a["action"] == "like"]),
    ):
        lines.extend([f"## {title} ({len(matches)})", ""])
        if not matches:
            lines.extend(["_Nothing outstanding._", ""])
        for reply, answer in matches:
            lines.append(
                f"- **@{_text(answer['author'])}**: {_text(answer['text']) or '(media-only answer)'} "
                f"({_link('answer', answer['permalink'])})"
            )
            hours = (
                threads_client.parse_timestamp(answer["timestamp"])
                - threads_client.parse_timestamp(reply["posted_at"])
            ).total_seconds() / 3600
            lines.append(
                f"  - Our comment: {_text(reply['text']) or '(media-only comment)'} "
                f"({_link('our reply', reply['reply_permalink'])}); answered after {hours:.1f}h."
            )
            if answer["media_type"] in {"IMAGE", "CAROUSEL_ALBUM"}:
                lines.append("  - Photo present; open the answer to view it.")
            if answer["child_reply_count"]:
                handles = ", ".join("@" + _text(h) for h in answer["child_reply_handles"])
                lines.append(f"  - Follow-ups: {answer['child_reply_count']} ({handles}).")
            lines.append("")
    lines.extend(["## All Tracked Replies", ""])
    if not replies:
        lines.extend(["_No outward candidates in the selected window._", ""])
    for reply in replies:
        likes = reply["like_count"] if reply["like_count"] is not None else "unknown"
        count = reply["answer_count"] if reply["answer_count"] is not None else "unknown"
        lines.append(
            f"- {_link('Our reply', reply['reply_permalink'])} ({reply['posted_at']}): "
            f"**{reply['state']}**; likes: {likes}; answers: {count}."
        )
        lines.append(f"  - {_text(reply['text']) or '(media-only comment)'}")
        if reply.get("error") or reply.get("like_error"):
            lines.append(f"  - **Unavailable:** {reply.get('error') or reply.get('like_error')}.")
        if reply.get("target_metadata_error"):
            lines.append(f"  - **Target metadata unavailable:** {reply['target_metadata_error']}.")
    return "\n".join(lines) + "\n"


def write_outputs(snapshot: dict, output_dir: Path) -> None:
    generated = threads_client.parse_timestamp(snapshot["generated_at"])
    history = output_dir / "history" / (
        "needs-outward-" + generated.strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    serialized = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    files = [
        (output_dir / "needs-outward.md", render(snapshot)),
        (history, serialized),
        (output_dir / "needs-outward.json", serialized),
    ]
    staged = []
    try:
        for path, content in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            staged.append((temporary, path))
            temporary.write_text(content, encoding="utf-8")
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=os.environ.get("OUTWARD_LOOKBACK_DAYS", "14"))
    parser.add_argument("--max-replies", type=int, default=os.environ.get("OUTWARD_MAX_REPLIES", "40"))
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)
    if args.lookback_days < 1 or args.max_replies < 1:
        parser.error("lookback-days and max-replies must be positive")
    client = None
    try:
        username = config.require("THREADS_USERNAME", config.THREADS_USERNAME)
        path = args.output_dir / "needs-outward.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if (
            not isinstance(previous, dict)
            or not isinstance(previous.get("replies", []), list)
            or not isinstance(previous.get("seen_answer_ids", []), list)
        ):
            raise RunAborted("INVALID_BASELINE")
        if previous.get("account") and _handle(previous["account"]) != _handle(username):
            raise RunAborted("BASELINE_ACCOUNT_MISMATCH")
        client = Client(threads_auth.get_valid_access_token())
        snapshot = collect(
            client, username, previous, dt.datetime.now(dt.timezone.utc),
            args.lookback_days, args.max_replies,
        )
        write_outputs(snapshot, args.output_dir)
        summary = (
            f"Outward: {len(snapshot['replies'])} checked; "
            f"{sum(a['is_new'] for r in snapshot['replies'] for a in r['answers'])} new answers; "
            f"{sum(r['state'] == 'unreachable' for r in snapshot['replies'])} unreachable; "
            f"status={snapshot['status']}."
        )
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(summary + "\n")
        return 0
    except (ObservationError, ValueError, OSError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, ObservationError) else type(exc).__name__
        print(f"Outward aborted: {message}. Previous published feed is unchanged.", file=sys.stderr)
        return 1
    finally:
        if client:
            client.session.close()


if __name__ == "__main__":
    sys.exit(main())
