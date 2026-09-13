import hashlib
import io
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

try:
    import pillow_avif  # noqa: F401  (registers AVIF support with Pillow)
except ImportError:
    pass

import db
import net
from scrape import find_pdf_links, is_generic_link_text

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = Path(r"G:\내 드라이브\[작업공간]\웹이미지 수집")
KEEP_ORIGINAL = os.environ.get("IMAGE_KEEP_ORIGINAL", "1") != "0"

# Formats Pillow can decode. Anything outside this set (or that fails to
# decode) is treated as "not actually an image" — e.g. a site returned an
# HTML error/login page instead of the requested picture.
FORMAT_EXT = {
    "JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif",
    "BMP": "bmp", "TIFF": "tiff", "AVIF": "avif",
}
# JPEG/PNG are saved as-is (byte-for-byte) to avoid a lossy re-encode.
# Everything else gets converted to JPEG, or PNG if it carries transparency.
NO_CONVERT = {"JPEG", "PNG"}


class DownloadError(Exception):
    pass


def _relpath(path: Path) -> str:
    return str(path.relative_to(DOWNLOADS_DIR)).replace("\\", "/")


_WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Looks like a storage ID rather than a name a person gave the file — a long
# run of hex/digits (e.g. the BOK CDN's "d8feb2de24a34fe38ef9db25182c8b05").
_ID_LIKE_STEM = re.compile(r"^[0-9a-fA-F_\-]{16,}$")
_MAX_STEM_LENGTH = 100


def _clean_stem(name: str | None, ext: str) -> str | None:
    """Turn a human-supplied name into a safe Windows filename stem, or None
    if nothing usable is left. Drops a trailing ".<ext>" so a link text like
    "보고서.pdf" doesn't end up saved as "보고서.pdf.pdf"."""
    if not name:
        return None
    stem = unquote(name).strip()
    if stem.lower().endswith(f".{ext}"):
        stem = stem[: -(len(ext) + 1)]
    stem = _WINDOWS_BAD_CHARS.sub("_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:_MAX_STEM_LENGTH].strip(" .")
    # Button words ("다운로드", "PDF") would make every file "다운로드.pdf".
    if not stem or is_generic_link_text(stem):
        return None
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    return stem


def content_disposition_name(resp) -> str | None:
    header = resp.headers.get("Content-Disposition", "") if resp is not None else ""
    if not header:
        return None
    # RFC 5987 form (filename*=UTF-8''%EB%B3%B4...) wins over plain filename=.
    m = re.search(r"filename\*\s*=\s*([\w-]+)''([^;]+)", header, re.IGNORECASE)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=m.group(1))
        except LookupError:
            return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)', header, re.IGNORECASE)
    if not m:
        return None
    value = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
    # HTTP headers reach Python decoded as latin-1, but Korean servers put
    # raw UTF-8 (BOK) or CP949 bytes in them — undo that before unquoting.
    try:
        raw_bytes = value.encode("latin-1")
    except UnicodeEncodeError:
        return unquote(value)
    for encoding in ("utf-8", "cp949"):
        try:
            return unquote(raw_bytes.decode(encoding))
        except UnicodeDecodeError:
            continue
    return unquote(value)


def _url_basename(url: str) -> str | None:
    basename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if not stem or _ID_LIKE_STEM.match(stem):
        return None
    return basename


def _pick_filename_stem(ext: str, title: str, resp, link_text: str | None, url: str) -> str | None:
    """Most deliberate name first: an explicit title (sheet row / API / Link
    Gopher link text), then what the server itself calls the file
    (Content-Disposition), then the link text on the landing page the PDF
    was resolved from, then the URL's own filename if it isn't just an ID."""
    for candidate in (title, content_disposition_name(resp), link_text, _url_basename(url)):
        stem = _clean_stem(candidate, ext)
        if stem:
            return stem
    return None


def _unique_path(directory: Path, stem: str, ext: str) -> Path:
    path = directory / f"{stem}.{ext}"
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}).{ext}"
        n += 1
    return path


def _existing_file(record: dict) -> bool:
    return bool(record.get("local_path")) and (DOWNLOADS_DIR / record["local_path"]).is_file()


def _as_duplicate(record: dict) -> dict:
    print(f"[pipeline] duplicate: already saved as {record['local_path']} (id={record['id']})")
    return {**record, "duplicate": True}


def find_saved_by_url(url: str) -> dict | None:
    for record in db.find_raw_by_source_url(url):
        if _existing_file(record):
            return record
    return None


def _find_duplicate_by_hash(sha256: str) -> dict | None:
    # Files saved before the sha256 column existed get fingerprinted the
    # first time a duplicate check runs, so they're covered too.
    for record in db.list_raw_missing_sha256():
        if _existing_file(record):
            db.set_sha256(record["id"], hashlib.sha256((DOWNLOADS_DIR / record["local_path"]).read_bytes()).hexdigest())
    for record in db.find_raw_by_sha256(sha256):
        if _existing_file(record):
            return record
    return None


def _save_raw(
    raw: bytes, ext: str, mime_type: str, id_prefix: str, url: str, source_page: str, title: str, job_id: str,
    resp=None, link_text: str | None = None,
) -> dict:
    """Save a non-image file whose real type was determined by signature
    (magic bytes), not by trusting the URL extension or Content-Type —
    shared by PDF/EPUB-ZIP/DjVu, which all just need "write the bytes,
    record it" with no format-specific processing the way images do.

    The same bytes already on disk (by content hash, whatever URL they came
    from) are not written again — the existing record comes back instead,
    marked duplicate=True."""
    sha256 = hashlib.sha256(raw).hexdigest()
    existing = _find_duplicate_by_hash(sha256)
    if existing is not None:
        return _as_duplicate(existing)

    job_dir = DOWNLOADS_DIR / job_id
    converted_dir = job_dir / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)

    job_seq = db.next_job_seq(job_id)
    daily_seq = db.next_daily_seq()
    stem = _pick_filename_stem(ext, title, resp, link_text, url) or f"{job_seq:02d}"
    out_path = _unique_path(converted_dir, stem, ext)
    out_path.write_bytes(raw)

    record = {
        "id": f"{id_prefix}_{datetime.now():%Y%m%d}_{daily_seq:03d}",
        "job_id": job_id,
        "seq": job_seq,
        "filename": out_path.name,
        "local_path": _relpath(out_path),
        "original_path": None,
        "source_url": url,
        "source_page": source_page or None,
        "title": title or None,
        "caption": None,
        "mime_type": mime_type,
        "width": None,
        "height": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "drive_file_id": None,
        "drive_url": None,
        "sha256": sha256,
    }
    db.insert_image(record)
    return record


def _save_pdf(raw: bytes, url: str, source_page: str, title: str, job_id: str, resp=None, link_text: str | None = None) -> dict:
    return _save_raw(raw, "pdf", "application/pdf", "pdf", url, source_page, title, job_id, resp=resp, link_text=link_text)


def _looks_like_epub(raw: bytes) -> bool:
    # EPUB is a ZIP whose very first entry is an uncompressed "mimetype"
    # file containing exactly "application/epub+zip" — cheap to spot
    # without a real zip parse, since that entry sits right after the
    # local file header at the start of the archive.
    return b"application/epub+zip" in raw[:100]


def _log_fetch_failure(url: str, reason: str, resp=None) -> None:
    """One line with everything needed to tell *why* a fetch failed without
    re-running it — final URL (redirects may have changed it), status code,
    and Content-Type, alongside the original URL and the failure reason."""
    final_url = resp.url if resp is not None else "-"
    status = resp.status_code if resp is not None else "-"
    ctype = resp.headers.get("Content-Type", "-") if resp is not None else "-"
    print(f"[pipeline] fetch failed: {reason} | url={url} final_url={final_url} status={status} content-type={ctype}")


def _fetch_url(url: str, cookies: dict | None = None):
    """Fetch with error types kept distinguishable (URL/DNS, blocked
    internal target, timeout, specific HTTP status, connection failure)
    instead of collapsing everything into one generic "download failed" —
    matters both for the failure-breakdown view (buckets by these exact
    messages) and for telling a real block apart from a transient blip."""
    try:
        resp = net.fetch_image(url, url, cookies=cookies)
    except net.BlockedURLError as e:
        _log_fetch_failure(url, "blocked (SSRF guard)")
        raise DownloadError(str(e)) from e
    except requests.exceptions.Timeout as e:
        _log_fetch_failure(url, "timeout")
        raise DownloadError(f"연결 시간 초과: {e}") from e
    except requests.exceptions.ConnectionError as e:
        _log_fetch_failure(url, "connection error")
        raise DownloadError(f"연결 실패: {e}") from e
    except requests.exceptions.RequestException as e:
        _log_fetch_failure(url, "request error")
        raise DownloadError(f"다운로드 실패: {e}") from e

    if resp.status_code == 403:
        _log_fetch_failure(url, "403 forbidden", resp)
        raise DownloadError(f"접근이 거부되었습니다 (403): {resp.url}")
    if resp.status_code == 404:
        _log_fetch_failure(url, "404 not found", resp)
        raise DownloadError(f"파일을 찾을 수 없습니다 (404): {resp.url}")
    if resp.status_code == 429:
        _log_fetch_failure(url, "429 rate limited", resp)
        raise DownloadError(f"요청이 너무 잦습니다 (429): {resp.url}")
    if resp.status_code >= 500:
        _log_fetch_failure(url, f"{resp.status_code} server error", resp)
        raise DownloadError(f"서버 오류 ({resp.status_code}): {resp.url}")
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        _log_fetch_failure(url, f"{resp.status_code} http error", resp)
        raise DownloadError(f"다운로드 실패: {e}") from e
    return resp


def _resolve_pdf_from_html(raw: bytes, page_url: str):
    """A URL can turn out to be an HTML landing/redirect page instead of the
    file itself (a "click here to download" page) — same idea as a download
    manager resolving a link before fetching it. Look for a direct .pdf link
    on that page and follow it, trying candidates in order until one
    actually verifies as a PDF by magic bytes (not just by extension).
    Returns (raw_bytes, resolved_url, response, link_text) or all None if
    nothing panned out — link_text is what the page called that file, kept
    so it can become the saved filename."""
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception:
        return None, None, None, None
    for candidate in find_pdf_links(soup, page_url):
        try:
            resp = _fetch_url(candidate)
        except DownloadError:
            continue
        if resp.content[:5] == b"%PDF-":
            return resp.content, candidate, resp, _link_text_for(soup, page_url, candidate)
    return None, None, None, None


def _link_text_for(soup: BeautifulSoup, page_url: str, target_url: str) -> str | None:
    """A page often links the same file several times ("보고서.pdf",
    "다운로드", "뷰어") — take the first label that actually names it."""
    for a in soup.find_all("a", href=True):
        if urljoin(page_url, a["href"].strip()) != target_url:
            continue
        for label in (a.get("title"), a.get_text(strip=True)):
            if _clean_stem(label, "pdf"):
                return label
    return None


def download_and_process(
    url: str, source_page: str = "", title: str = "", folder: str = "",
    cookies: dict | None = None,
) -> dict:
    """cookies: only ever what a caller explicitly hands in (e.g. the user's
    own already-logged-in session for a site) — never derived, stored, or
    reused automatically across calls."""
    if not url:
        raise DownloadError("url이 필요합니다.")

    # Exact same URL already saved as a file (PDF/EPUB/ZIP/DjVu) that still
    # exists on disk — skip the network entirely. Different URLs pointing at
    # the same file are caught later by content hash in _save_raw().
    existing = find_saved_by_url(url)
    if existing is not None:
        return _as_duplicate(existing)

    job_id = db.get_or_create_job(folder)
    resp = _fetch_url(url, cookies=cookies)
    raw = resp.content
    content_type = resp.headers.get("Content-Type", "")
    print(
        f"[pipeline] fetched: url={url} final_url={resp.url} status={resp.status_code} "
        f"content-type={content_type} content-length={resp.headers.get('Content-Length', '-')} "
        f"content-disposition={resp.headers.get('Content-Disposition', '-')}"
    )

    # PDF check comes first and by magic bytes, not Content-Type header — same
    # "never trust the header" reasoning as the image path below (a blocked
    # request can come back as an HTML page with an image/pdf Content-Type).
    if raw[:5] == b"%PDF-":
        return _save_pdf(raw, url, source_page, title, job_id, resp=resp)

    looks_like_html = content_type.startswith("text/html") or raw.lstrip()[:15].lower().startswith(b"<!doctype html") or raw.lstrip()[:5].lower() == b"<html"
    if looks_like_html:
        resolved_raw, resolved_url, resolved_resp, link_text = _resolve_pdf_from_html(raw, resp.url)
        if resolved_raw is not None:
            return _save_pdf(
                resolved_raw, resolved_url, source_page or url, title, job_id,
                resp=resolved_resp, link_text=link_text,
            )
        # A real image URL never comes back as an HTML document, so there's
        # no point handing this to Pillow — it's an HTML page (login wall,
        # error page, a landing page with no findable PDF link), not a file.
        _log_fetch_failure(url, "html response, no downloadable file found", resp)
        raise DownloadError(f"HTML 응답입니다 (다운로드 대상 파일 아님, Content-Type: {content_type or 'text/html'}): {resp.url}")

    # ZIP-family and DjVu signatures, checked the same way as PDF above —
    # by the actual bytes, never by Content-Type (a generic file server
    # commonly answers with "application/octet-stream" for these too, and
    # trusting that header instead of the signature is exactly what used to
    # send real EPUB/DjVu files into the image decoder and produce a
    # confusing "cannot identify image file" error).
    if raw[:4] == b"PK\x03\x04" or raw[:4] == b"PK\x05\x06":
        if _looks_like_epub(raw):
            return _save_raw(raw, "epub", "application/epub+zip", "epub", url, source_page, title, job_id, resp=resp)
        return _save_raw(raw, "zip", "application/zip", "zip", url, source_page, title, job_id, resp=resp)
    if raw[:4] == b"AT&T":
        return _save_raw(raw, "djvu", "image/vnd.djvu", "djvu", url, source_page, title, job_id, resp=resp)

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        # Not PDF, not HTML, not ZIP/EPUB, not DjVu, and Pillow — which
        # does its own signature-based format sniffing, not extension or
        # Content-Type — doesn't recognize it either. Genuinely unknown,
        # not a bug to chase: report it as exactly that instead of leaking
        # Pillow's raw "cannot identify image file <...>" exception text.
        signature = raw[:8].hex()
        _log_fetch_failure(url, "unknown binary signature", resp)
        raise DownloadError(
            f"UNKNOWN_BINARY (Content-Type: {content_type or 'unknown'}, signature: {signature}): "
            f"지원하지 않는 파일 형식입니다."
        )

    fmt = im.format or "JPEG"
    orig_ext = FORMAT_EXT.get(fmt, "bin")

    job_dir = DOWNLOADS_DIR / job_id
    converted_dir = job_dir / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)

    job_seq = db.next_job_seq(job_id)
    daily_seq = db.next_daily_seq()
    seq_name = f"{job_seq:02d}"
    file_id = f"img_{datetime.now():%Y%m%d}_{daily_seq:03d}"

    if fmt in NO_CONVERT:
        target_fmt, target_ext = fmt, orig_ext
        out_path = converted_dir / f"{seq_name}.{target_ext}"
        out_path.write_bytes(raw)
    else:
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if has_alpha:
            target_fmt, target_ext = "PNG", "png"
            out_im = im.convert("RGBA")
        else:
            target_fmt, target_ext = "JPEG", "jpg"
            out_im = im.convert("RGB")
        out_path = converted_dir / f"{seq_name}.{target_ext}"
        save_kwargs = {"quality": 90, "optimize": True} if target_fmt == "JPEG" else {}
        out_im.save(out_path, format=target_fmt, **save_kwargs)

    original_path = None
    if KEEP_ORIGINAL:
        original_dir = job_dir / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        original_path = original_dir / f"{seq_name}.{orig_ext}"
        original_path.write_bytes(raw)

    width, height = im.size
    record = {
        "id": file_id,
        "job_id": job_id,
        "seq": job_seq,
        "filename": out_path.name,
        "local_path": _relpath(out_path),
        "original_path": _relpath(original_path) if original_path else None,
        "source_url": url,
        "source_page": source_page or None,
        "title": title or None,
        "caption": None,
        "mime_type": f"image/{target_fmt.lower()}",
        "width": width,
        "height": height,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "drive_file_id": None,
        "drive_url": None,
    }
    db.insert_image(record)
    return record
