"""Private vision-provenance report; optional loopback-only image previews."""
from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from . import cache as media_cache, config, threads_client, vision


def _permalink(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}
            or url.username or url.password or url.port not in {None, 443}
        ):
            return ""
        return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    except ValueError:
        return ""


def collect_references(threads: list[dict]) -> dict[str, dict]:
    references = {}

    def walk(item: dict, root_link: str) -> None:
        if item.get("hide_status") == "HIDDEN":
            return
        link = _permalink(item.get("permalink") or root_link)
        for photo in threads_client.photo_items(item):
            references[photo["id"]] = {
                "permalink": link,
                "media_url": photo.get("media_url") or "",
            }
        for reply in item.get("replies", []):
            walk(reply, root_link)

    for thread in threads:
        walk(thread, thread.get("permalink") or "")
    return references


def render_report(cache: dict, references: dict[str, dict] | None = None, *, previews: bool = False) -> str:
    references = references or {}
    articles = []
    for media_id in sorted(cache.keys() | references.keys()):
        entry = cache.get(media_id)
        text = media_cache.description_text(entry)
        stale = vision._is_stale_cached_placeholder(entry)
        metadata = entry if isinstance(entry, dict) else {}
        provider = metadata.get("provider") or "legacy/unknown"
        if entry is None:
            status, provider = "pending (no successful cached result)", "unknown"
        elif stale:
            status = "pending (stale cached result)"
        elif text.startswith(vision.DESCRIPTION_UNAVAILABLE_PREFIX):
            status = "moderated" if "blocked by " in text else "unavailable"
        else:
            status = "described"

        def display(value: object) -> str:
            return escape(vision._sanitize(str(value or "unknown")))

        reference = references.get(media_id, {})
        permalink = _permalink(reference.get("permalink") or "")
        link = f'<a href="{escape(permalink, quote=True)}" rel="noreferrer">Open post/reply</a>' if permalink else ""
        image = ""
        if previews and reference.get("media_url"):
            image = (
                f'<img src="/image/{quote(media_id, safe="")}" loading="lazy" '
                'alt="Photo preview" referrerpolicy="no-referrer">'
            )
        articles.append(
            f"<article><h2>{display(media_id)}</h2>{image}"
            f"<p>{link}</p><p><strong>{display(status)}</strong></p>"
            f"<p>Provider: {display(provider)} | Model: {display(metadata.get('model'))}<br>"
            f"Described: {display(metadata.get('described_at'))}<br>"
            f"Fallback: {display(metadata.get('fallback_reason') or 'none')}</p>"
            f"<pre>{display(text) if text else 'No description available.'}</pre></article>"
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; '
        'base-uri \'none\'; form-action \'none\'">'
        "<title>Private vision review</title>"
        "<style>body{font:16px system-ui;max-width:1000px;margin:2em auto;padding:1em}"
        "article{border-top:1px solid #aaa;padding:1em 0}pre{white-space:pre-wrap}"
        "img{max-width:100%;max-height:400px}</style>"
        "<h1>Private vision review</h1>"
        "<p>Snapshot of cached results. Legacy provenance is unknown; cache hits "
        "retain the original attribution. Photo previews require the local server. "
        "Failed uncached attempts appear pending; consult run logs for their cause.</p>"
        + ("".join(articles) or "<p>No cached or current photos.</p>") + "</html>"
    )


def review_server(cache: dict, references: dict[str, dict], port: int = 8091) -> ThreadingHTTPServer:
    html = render_report(cache, references, previews=True).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.headers.get("Host") not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            if self.path == "/":
                content, mime = html, "text/html; charset=utf-8"
            elif self.path.startswith("/image/"):
                media_id = unquote(self.path[len("/image/"):])
                url = references.get(media_id, {}).get("media_url") or ""
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError("Invalid image URL")
                    with requests.get(url, timeout=30, stream=True) as response:
                        response.raise_for_status()
                        mime = response.headers.get("Content-Type", "").split(";")[0]
                        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}:
                            raise ValueError("Unsupported image")
                        chunks, size = [], 0
                        for chunk in response.iter_content(64 * 1024):
                            size += len(chunk)
                            if size > 15 * 1024 * 1024:
                                raise ValueError("Preview too large")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                except (requests.RequestException, ValueError):
                    self.send_error(404, "Preview unavailable; open the post or refresh references")
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(content)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Serve private previews on loopback; implies --refresh")
    parser.add_argument("--refresh", action="store_true", help="Fetch current Threads references (no vision calls)")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args(argv)
    cache = media_cache.load_cache()
    references = {}
    if args.refresh or args.serve:
        config.require("THREADS_ACCESS_TOKEN", config.THREADS_ACCESS_TOKEN)
        references = collect_references(threads_client.collect_threads_with_replies(
            config.LOOKBACK_DAYS, config.MAX_ROOT_THREADS
        ))
    path = media_cache.CACHE_PATH.with_name("vision-review.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(cache, references), encoding="utf-8")
    path.chmod(0o600)
    print(f"Wrote private report: {path}", flush=True)
    if args.serve:
        with review_server(cache, references, args.port) as server:
            print(f"Private review: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
