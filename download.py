#!/usr/bin/env python3
"""Download and verify the Octara PDF search results listed in sources.txt."""

import csv
import hashlib
from pathlib import Path
import time
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "pdfs"
SOURCES = ROOT / "sources.txt"
MANIFEST = ROOT / "PDFS.csv"


def main() -> None:
    urls = [line.strip() for line in SOURCES.read_text().splitlines() if line.strip()]
    OUTPUT.mkdir(exist_ok=True)
    records = []

    for position, url in enumerate(urls, 1):
        filename = Path(unquote(urlparse(url).path)).name
        destination = OUTPUT / filename
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})

        for attempt in range(4):
            try:
                with urlopen(request, timeout=120) as response:
                    data = response.read()
                if not data.startswith(b"%PDF"):
                    raise ValueError("server response is not a PDF")
                if len(data) >= 100_000_000:
                    raise ValueError(
                        f"{filename} exceeds GitHub's 100 MB per-file limit"
                    )
                destination.write_bytes(data)
                records.append(
                    (filename, len(data), hashlib.sha256(data).hexdigest(), url)
                )
                print(f"[{position}/{len(urls)}] {filename}: {len(data):,} bytes")
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))

    with MANIFEST.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("filename", "bytes", "sha256", "source_url"))
        writer.writerows(records)


if __name__ == "__main__":
    main()
