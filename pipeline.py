import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from PIL import Image

try:
    import pillow_avif  # noqa: F401  (registers AVIF support with Pillow)
except ImportError:
    pass

import db
import net
import resolver
from scrape import is_generic_link_text

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


def _diagnose_unknown_response(raw: bytes, url: str, resp, decode_error: Exception) -> str:
    """Preserve evidence before reporting an unrecognized response.

    Samples use escaped JSON so control characters cannot corrupt terminal
    output. The .bin contains the entire response.content, without conversion
    (requests may already have decompressed HTTP Content-Encoding).
    """
    sample = raw[:512]
    details = {
        "url": url,
        "final_url": resp.url,
        "status": resp.status_code,
        "headers": {name: resp.headers.get(name, "") for name in (
            "Content-Type", "Content-Length", "Content-Disposition",
            "Content-Encoding", "Transfer-Encoding", "Server",
        )},
        "redirects": [{"url": hop.url, "status": hop.status_code}
                      for hop in resp.history],
        "body_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "decoder_error": type(decode_error).__name__,
        "sample_bytes": len(sample),
        "sample_hex": sample.hex(),
        "sample_utf8": sample.decode("utf-8", errors="replace"),
        "sample_latin1": sample.decode("latin-1"),
    }
    # Print evidence even if the filesystem is full/unwritable.
    print("[pipeline] UNKNOWN_BINARY diagnostics: " + json.dumps(details, ensure_ascii=True), flush=True)
    if os.environ.get("UNKNOWN_BINARY_SAVE_RAW", "1") == "0":
        print("[pipeline] UNKNOWN_BINARY raw saving disabled", flush=True)
        return "원본 임시 저장 꺼짐 (UNKNOWN_BINARY_SAVE_RAW=0)"

    try:
        configured_dir = os.environ.get("UNKNOWN_BINARY_DIAGNOSTICS_DIR")
        diagnostic_dir = (Path(configured_dir) if configured_dir else
                          Path(tempfile.gettempdir()) / "image-crawler-diagnostics")
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        # Exclusive unique names keep concurrent workers/retries from overwriting evidence.
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="unknown-", suffix=".bin", dir=diagnostic_dir, delete=False,
        ) as output:
            output.write(raw)
            saved_path = str(Path(output.name).resolve())
    except OSError as exc:
        print("[pipeline] UNKNOWN_BINARY raw save failed: " +
              json.dumps({"error": str(exc)}, ensure_ascii=True), flush=True)
        return "원본 임시 저장 실패 (진단 로그 확인)"

    print("[pipeline] UNKNOWN_BINARY raw saved: " +
          json.dumps({"path": saved_path, "body_bytes": len(raw)}, ensure_ascii=True), flush=True)
    return f"원본 임시 저장: {saved_path}"


def _fetch_url(url: str, cookies: dict | None = None, page_url: str = "", retry_requests: bool = True):
    """Fetch with error types kept distinguishable (URL/DNS, blocked
    internal target, timeout, specific HTTP status, connection failure)
    instead of collapsing everything into one generic "download failed" —
    matters both for the failure-breakdown view (buckets by these exact
    messages) and for telling a real block apart from a transient blip."""
    try:
        resp = net.fetch_image(url, page_url or url, cookies=cookies, retry_requests=retry_requests)
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


def download_and_process(
    url: str, source_page: str = "", title: str = "", folder: str = "",
    cookies: dict | None = None, expected_md5: str = "",
) -> dict:
    """cookies: only ever what a caller explicitly hands in (e.g. the user's
    own already-logged-in session for a site) — never derived, stored, or
    reused automatically across calls."""
    if not url:
        raise DownloadError("url이 필요합니다.")

    try:
        expected_md5 = resolver.normalize_md5(expected_md5)
        url_checksum = resolver.url_md5(url)
        if expected_md5 and url_checksum and expected_md5 != url_checksum:
            raise resolver.ResolutionError("입력 MD5와 URL의 MD5가 다릅니다.")
    except resolver.ResolutionError as exc:
        raise DownloadError(str(exc)) from exc

    # Exact same URL already saved as a file that still exists on disk: skip
    # the network. When a checksum was supplied, verify the saved bytes first.
    existing = find_saved_by_url(url)
    if existing is not None:
        checksum = expected_md5 or url_checksum
        if checksum:
            actual_md5 = hashlib.md5(
                (DOWNLOADS_DIR / existing["local_path"]).read_bytes(), usedforsecurity=False,
            ).hexdigest()
            if actual_md5 != checksum:
                raise DownloadError(f"MD5_MISMATCH: expected={checksum}, actual={actual_md5}, url={url}")
            return {**_as_duplicate(existing), "requested_url": url, "resolved_url": url,
                    "md5": actual_md5, "md5_verified": True}
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

    requested_url = url
    resolution = {}
    if resolver.is_file(raw) or resolver.is_html(raw, content_type):
        try:
            # Caller cookies belong to the initial request; never forward a
            # caller's cookie dict to a newly discovered mirror host.
            resp, actual_md5, checked_md5 = resolver.resolve(
                resp, url, lambda target, parent: _fetch_url(target, page_url=parent, retry_requests=False),
                _diagnose_unknown_response, expected_md5=expected_md5,
            )
        except resolver.ResolutionError as exc:
            raise DownloadError(str(exc)) from exc
        raw = resp.content
        content_type = resp.headers.get("Content-Type", "")
        url = resp.url
        if url != requested_url:
            source_page = source_page or requested_url
        resolution = {"requested_url": requested_url, "resolved_url": url,
                      "md5": actual_md5, "md5_verified": bool(checked_md5)}

    def resolved_record(record):
        return {**record, **resolution}

    # PDF check comes first and by magic bytes, not Content-Type header — same
    # "never trust the header" reasoning as the image path below (a blocked
    # request can come back as an HTML page with an image/pdf Content-Type).
    if raw[:5] == b"%PDF-":
        return resolved_record(_save_pdf(raw, url, source_page, title, job_id, resp=resp))

    # ZIP-family and DjVu signatures, checked the same way as PDF above —
    # by the actual bytes, never by Content-Type (a generic file server
    # commonly answers with "application/octet-stream" for these too, and
    # trusting that header instead of the signature is exactly what used to
    # send real EPUB/DjVu files into the image decoder and produce a
    # confusing "cannot identify image file" error).
    if raw[:4] == b"PK\x03\x04" or raw[:4] == b"PK\x05\x06":
        if _looks_like_epub(raw):
            return resolved_record(_save_raw(raw, "epub", "application/epub+zip", "epub", url, source_page, title, job_id, resp=resp))
        return resolved_record(_save_raw(raw, "zip", "application/zip", "zip", url, source_page, title, job_id, resp=resp))
    if raw[:4] == b"AT&T":
        return resolved_record(_save_raw(raw, "djvu", "image/vnd.djvu", "djvu", url, source_page, title, job_id, resp=resp))

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        # Unknown may be text, a damaged file, or an unsupported format.
        # Keep the evidence for inspection instead of declaring it unsupported.
        signature = raw[:8].hex()
        diagnostic_result = _diagnose_unknown_response(raw, url, resp, exc)
        raise DownloadError(
            f"UNKNOWN_BINARY (Content-Type: {content_type or 'unknown'}, signature: {signature}): "
            f"응답 형식을 식별하지 못했습니다. 첫 512바이트 진단 로그 확인. {diagnostic_result}"
        ) from exc

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
    return resolved_record(record)
