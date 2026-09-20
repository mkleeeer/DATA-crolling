import time
import uuid
from datetime import datetime

import net
import resolver
import settings
import sheets
from queue_config import POLL_SECONDS, SPREADSHEET_ID
from scrape import extract_images_from_html, find_og_image
from bs4 import BeautifulSoup

# Real-pixel-size thresholds for dropping obviously-junk candidates before
# they're ever queued for download (icons, tracking pixels, thin banner
# strips) — same shape of heuristic as newspaper3k's image scorer
# (minimal_area / min width / max aspect ratio), tuned looser on the ratio
# than newspaper3k's 16:9 since that would reject plenty of legitimate
# portrait/landscape photos.
MIN_CANDIDATE_AREA = 5000
MIN_CANDIDATE_WIDTH = 80
MAX_CANDIDATE_ASPECT_RATIO = 3.0

# Each submission costs 2-3 Sheets write calls (status=processing, then
# done/error, plus an images append on success). Clearing a large backlog
# in one run_once() fires those back-to-back, which is what pushed the
# write quota into sustained 429s even with retries. A small pause between
# rows spreads writes out instead of bursting them.
_ROW_PACING_SECONDS = 0.4


# File signatures pipeline.download_and_process() knows how to save — checked
# on the first bytes only, so a file served with a generic Content-Type
# (application/octet-stream, binary/octet-stream, missing) is still handed to
# the download step instead of being parsed as an HTML page.
_FILE_SIGNATURES = (
    b"%PDF-",                        # PDF
    b"PK\x03\x04", b"PK\x05\x06",    # ZIP / EPUB
    b"AT&T",                         # DjVu
    b"\xff\xd8\xff",                 # JPEG
    b"\x89PNG\r\n\x1a\n",            # PNG
    b"GIF87a", b"GIF89a",            # GIF
)


def _looks_like_file(head: bytes) -> bool:
    if head.startswith(_FILE_SIGNATURES):
        return True
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _is_direct_file(resp, content_type: str) -> bool:
    """Decide "file to download" vs. "page to scrape" while reading as little
    of the body as possible. The download worker fetches a file in full
    anyway, so pulling the whole thing here too (just to look at a header)
    meant every PDF crossed the network twice.

    - image/* or application/pdf: trusted as a file, body never read
    - text/html: a page, body read normally for scraping
    - anything else: peek at the first chunk's signature; if it's not a
      known file, the rest is read so the caller can still use resp.text
    """
    ctype = content_type.lower()
    if ctype.startswith("image/") or ctype.startswith("application/pdf"):
        return True
    if ctype.startswith("text/html") or ctype.startswith("application/xhtml"):
        return False
    chunks = resp.iter_content(chunk_size=8192)
    head = next(chunks, b"")
    if _looks_like_file(head):
        return True
    # Not a file after all — reassemble the body so resp.text (and its
    # charset detection) behaves exactly as it would on a non-streamed
    # response. requests has no public API to "un-peek" a stream.
    resp._content = head + b"".join(chunks)
    resp._content_consumed = True
    return False


def _passes_size_filter(dimensions) -> bool:
    if dimensions is None:
        return True  # couldn't determine size — don't punish it for that
    width, height = dimensions
    if width * height < MIN_CANDIDATE_AREA:
        return False
    if width < MIN_CANDIDATE_WIDTH:
        return False
    if max(width, height) / max(min(width, height), 1) > MAX_CANDIDATE_ASPECT_RATIO:
        return False
    return True


def _filter_candidates_by_size(candidates: list, page_url: str) -> list:
    kept = []
    for cand in candidates:
        dimensions = net.probe_image_dimensions(cand["url"], page_url)
        if _passes_size_filter(dimensions):
            kept.append(cand)
        else:
            print(f"[extractor] dropping small/odd-shaped candidate ({dimensions}): {cand['url']}")
    return kept


def process_submission(row: dict) -> None:
    url = (row.get("url") or "").strip()
    folder = row.get("folder") or ""
    source_page = row.get("source_page") or ""
    title = row.get("title") or ""
    row_number = row["_row_number"]

    print(f"[extractor] processing row {row_number}: {url}")

    try:
        sheets.update_row(
            SPREADSHEET_ID, "submissions", row_number,
            {"status": "processing"}, sheets.SUBMISSIONS_HEADERS,
        )
        resp = net.fetch_page(url, stream=True)
        try:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            is_direct_file = _is_direct_file(resp, content_type)
            if not is_direct_file:
                page_text = resp.text
        finally:
            resp.close()

        if row.get("kind") == "file" or resolver.url_md5(url) or is_direct_file:
            # File submissions keep ONE original URL so the downloader can
            # resolve mirrors/checksums rather than queueing a book cover.
            candidates = [{"url": url, "alt": title}]
        elif settings.get_only_og_image():
            if not source_page:
                source_page = resp.url
            og_url = find_og_image(BeautifulSoup(page_text, "html.parser"), resp.url)
            candidates = [{"url": og_url, "alt": title}] if og_url else []
        else:
            candidates = extract_images_from_html(page_text, resp.url)
            if not source_page:
                source_page = resp.url
            candidates = _filter_candidates_by_size(candidates, resp.url)

        if not candidates:
            no_image_reason = (
                "대표 이미지(og:image)가 없는 페이지입니다."
                if settings.get_only_og_image() else "이미지를 찾지 못했습니다."
            )
            sheets.update_row(
                SPREADSHEET_ID, "submissions", row_number,
                {"status": "error", "error": no_image_reason}, sheets.SUBMISSIONS_HEADERS,
            )
            return

        now = datetime.now().isoformat(timespec="seconds")
        image_rows = [
            {
                "id": f"cand_{uuid.uuid4().hex[:12]}",
                "submission_id": row.get("id", ""),
                "folder": folder,
                "seq": i,
                "source_url": cand["url"],
                "source_page": source_page or url,
                "title": title or cand.get("alt", ""),
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }
            for i, cand in enumerate(candidates, start=1)
        ]
        sheets.append_rows(SPREADSHEET_ID, "images", image_rows, sheets.IMAGES_HEADERS)

        sheets.update_row(
            SPREADSHEET_ID, "submissions", row_number,
            {"status": "done", "error": ""}, sheets.SUBMISSIONS_HEADERS,
        )
        print(f"[extractor] row {row_number}: found {len(candidates)} image(s)")

    except Exception as e:
        sheets.update_row(
            SPREADSHEET_ID, "submissions", row_number,
            {"status": "error", "error": str(e)[:300]}, sheets.SUBMISSIONS_HEADERS,
        )
        print(f"[extractor] row {row_number} failed: {e}")


def process_rows(row_numbers: set) -> int:
    """Process exactly these rows (by sheet row number), regardless of their
    current status — used by the web UI's selective/per-folder processing."""
    rows = sheets.read_rows(SPREADSHEET_ID, "submissions", sheets.SUBMISSIONS_HEADERS)
    targets = [r for r in rows if r["_row_number"] in row_numbers]
    handled = 0
    for row in targets:
        try:
            process_submission(row)
        except Exception as e:
            print(f"[extractor] row {row['_row_number']} unrecoverable: {e}")
        handled += 1
        time.sleep(_ROW_PACING_SECONDS)
    return handled


def run_once(stop_event=None) -> int:
    rows = sheets.read_rows(SPREADSHEET_ID, "submissions", sheets.SUBMISSIONS_HEADERS)
    pending = [r for r in rows if (r.get("status") or "").strip() in ("", "pending")]
    handled = 0
    for row in pending:
        if stop_event is not None and stop_event.is_set():
            break
        # One row's failure (including a failed error-status write) must not
        # stop the rest of the batch from being attempted.
        try:
            process_submission(row)
        except Exception as e:
            print(f"[extractor] row {row['_row_number']} unrecoverable: {e}")
        handled += 1
        time.sleep(_ROW_PACING_SECONDS)
    return handled


def main():
    print(f"[extractor] watching {SPREADSHEET_ID} every {POLL_SECONDS}s")
    while True:
        try:
            n = run_once()
            if n:
                print(f"[extractor] handled {n} submission(s)")
        except Exception as e:
            print(f"[extractor] loop error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
