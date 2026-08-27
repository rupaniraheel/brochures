#!/usr/bin/env python3
"""Discover and archive Octara PDFs from paginated search results and the site."""

from __future__ import annotations

import csv
import hashlib
import html
from html.parser import HTMLParser
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdfs"
MANIFEST = ROOT / "PDFS.csv"
DISCOVERED = ROOT / "sources.txt"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
OCTARA_HOSTS = {"octara.com", "www.octara.com"}


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.lower() in {"href", "src"} and value:
                self.links.append(value)


def request_bytes(url: str, timeout: int = 90) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", "")


def retry_request(url: str, attempts: int = 4) -> tuple[bytes, str]:
    for attempt in range(attempts):
        try:
            return request_bytes(url)
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def canonical(url: str, base: str = "https://octara.com/") -> str | None:
    value = html.unescape(urljoin(base, url.strip()))
    parsed = urlparse(value)
    if parsed.hostname not in OCTARA_HOSTS or parsed.scheme not in {"http", "https"}:
        return None
    # Normalize host/scheme while retaining encoded paths used by the server.
    return urlunparse(("https", "www.octara.com", parsed.path, "", parsed.query, ""))


def is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def extract_links(document: bytes, base: str) -> set[str]:
    parser = Links()
    parser.feed(document.decode("utf-8", "replace"))
    result = set()
    for link in parser.links:
        normalized = canonical(link, base)
        if normalized:
            result.add(normalized)
    return result


def sitemap_pages() -> set[str]:
    pending = ["https://www.octara.com/wp-sitemap.xml"]
    visited: set[str] = set()
    pages: set[str] = set()
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        try:
            document, _ = retry_request(url)
        except Exception as exc:
            print(f"Sitemap warning: {url}: {exc}")
            continue
        locations = re.findall(rb"<loc>\s*(.*?)\s*</loc>", document, re.I | re.S)
        for raw in locations:
            location = canonical(html.unescape(raw.decode("utf-8", "replace")))
            if not location:
                continue
            if urlparse(location).path.lower().endswith(".xml"):
                pending.append(location)
            elif is_pdf(location):
                pages.add(location)
            else:
                pages.add(location)
    return pages


def search_result_urls() -> set[str]:
    found: set[str] = set()
    query = quote_plus("site:octara.com filetype:pdf")
    search_pages = []
    # Paginate beyond the first result page on both engines.
    for offset in range(0, 200, 10):
        search_pages.append(f"https://www.google.com/search?q={query}&start={offset}&filter=0")
    for first in range(1, 202, 10):
        search_pages.append(f"https://www.bing.com/search?q={query}&first={first}&count=10")

    for position, search_url in enumerate(search_pages, 1):
        try:
            document, _ = retry_request(search_url, attempts=2)
        except Exception as exc:
            print(f"Search warning: {search_url}: {exc}")
            continue
        text = html.unescape(document.decode("utf-8", "replace"))
        candidates = re.findall(r"https?://[^\s\"'<>]+", text, re.I)
        for candidate in candidates:
            candidate = candidate.rstrip(".,);]&")
            parsed = urlparse(candidate)
            # Unwrap common Google redirect links.
            if parsed.hostname in {"google.com", "www.google.com"} and parsed.path == "/url":
                candidate = parse_qs(parsed.query).get("q", [""])[0]
            normalized = canonical(candidate)
            if normalized and is_pdf(normalized):
                found.add(normalized)
        print(f"Searched page {position}/{len(search_pages)}; PDFs found: {len(found)}")
    return found


def crawl_site(seeds: set[str]) -> set[str]:
    pending = list(seeds | {
        "https://www.octara.com/",
        "https://www.octara.com/e-newsletter/",
        "https://www.octara.com/publication/",
    })
    visited: set[str] = set()
    pdfs: set[str] = set()

    while pending and len(visited) < 600:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        if is_pdf(url):
            pdfs.add(url)
            continue
        try:
            document, content_type = retry_request(url, attempts=2)
        except Exception as exc:
            print(f"Crawl warning: {url}: {exc}")
            continue
        if "html" not in content_type.lower() and b"<html" not in document[:1000].lower():
            continue
        for link in extract_links(document, url):
            if is_pdf(link):
                pdfs.add(link)
            elif link not in visited:
                path = urlparse(link).path.lower()
                if not path.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx", ".mp4", ".mp3")):
                    pending.append(link)
        if len(visited) % 25 == 0:
            print(f"Crawled {len(visited)} pages; linked PDFs found: {len(pdfs)}")
    return pdfs


def output_path(url: str, used: dict[str, str]) -> Path:
    name = Path(unquote(urlparse(url).path)).name
    safe = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip() or "document.pdf"
    key = safe.casefold()
    if key in used and used[key] != url:
        stem, suffix = Path(safe).stem, Path(safe).suffix
        safe = f"{stem}-{hashlib.sha256(url.encode()).hexdigest()[:10]}{suffix}"
    used[safe.casefold()] = url
    return PDF_DIR / safe


def main() -> None:
    known = {
        canonical(line)
        for line in DISCOVERED.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    known.discard(None)

    sitemap = sitemap_pages()
    search = search_result_urls()
    linked = crawl_site({url for url in sitemap if not is_pdf(url)})
    urls = {url for url in known | search | linked | sitemap if url and is_pdf(url)}
    print(f"Discovered {len(urls)} unique PDF URLs")

    PDF_DIR.mkdir(exist_ok=True)
    used: dict[str, str] = {}
    records = []
    failures = []
    for position, url in enumerate(sorted(urls), 1):
        destination = output_path(url, used)
        try:
            # Reuse valid files from the initial archive when names match.
            data = destination.read_bytes() if destination.exists() else b""
            if not data.startswith(b"%PDF"):
                data, _ = retry_request(url)
            if not data.startswith(b"%PDF"):
                raise ValueError("response is not a PDF")
            if len(data) >= 100_000_000:
                raise ValueError("file exceeds GitHub's 100 MB limit")
            destination.write_bytes(data)
            records.append((destination.name, len(data), hashlib.sha256(data).hexdigest(), url))
            print(f"Downloaded {position}/{len(urls)}: {destination.name} ({len(data):,} bytes)")
        except Exception as exc:
            failures.append((url, str(exc)))
            print(f"FAILED {url}: {exc}")

    with MANIFEST.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("filename", "bytes", "sha256", "source_url"))
        writer.writerows(records)
    DISCOVERED.write_text("".join(f"{url}\n" for url in sorted(urls)), encoding="utf-8")

    if failures:
        failure_file = ROOT / "FAILED.csv"
        with failure_file.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("source_url", "error"))
            writer.writerows(failures)
        print(f"Warning: {len(failures)} PDF downloads failed; see FAILED.csv")
    else:
        failure_file = ROOT / "FAILED.csv"
        failure_file.unlink(missing_ok=True)

    print(f"Archived and validated {len(records)} PDFs")


if __name__ == "__main__":
    main()
