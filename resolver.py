"""Bounded traversal of download links actually advertised by a page."""
import hashlib
import io
import json
import re
from urllib.parse import parse_qsl, unquote, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from PIL import Image
from scrape import http_link, _looks_like_nav_link


class ResolutionError(Exception):
    pass


def normalize_md5(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value.strip()):
        raise ResolutionError("MD5는 32자리 16진수여야 합니다.")
    return value.strip().lower()


def url_md5(url):
    """Only explicit MD5 markers, never an arbitrary 32-character ID."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ResolutionError("올바르지 않은 URL입니다.") from exc
    values = [normalize_md5(v) for k, v in parse_qsl(parsed.query)
              if k.lower() == "md5"]
    match = re.search(r"/md5/([0-9a-fA-F]{32})(?:/|$)", unquote(parsed.path), re.I)
    if match:
        values.append(match.group(1).lower())
    values = set(filter(None, values))
    if len(values) > 1:
        raise ResolutionError("URL에 서로 다른 MD5가 포함되어 있습니다.")
    return next(iter(values), "")


def is_document(raw):
    return raw.startswith((b"%PDF-", b"PK\x03\x04", b"PK\x05\x06", b"AT&T"))


def is_file(raw):
    if is_document(raw):
        return True
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        return True
    except Exception:
        return False


def is_html(raw, content_type=""):
    if is_document(raw):
        return False
    return ("html" in content_type.lower() or
            bool(re.search(br"<(?:!doctype\s+html|html|head|body|a\s|meta\s)", raw[:65536], re.I)))


_LABEL = re.compile(r"\b(download|mirrors?\s*\d*|full[ -]?text)\b|다운로드|미러|원문", re.I)
_PATH = re.compile(r"/(?:download|get|mirror)(?:[/.]|$)", re.I)
_EXT = re.compile(r"\.(pdf|epub|djvu|djv|zip)$", re.I)


def download_links(raw, page_url, limit=8):
    """Select declared file/download/mirror links, plus meta refresh targets.

    No guessed hosts, search-result-wide crawling, forms, or JavaScript execution.
    """
    soup = BeautifulSoup(raw[:2_000_000], "html.parser")
    base = soup.find("base", href=True)
    base_url = (http_link(page_url, base["href"]) if base else "") or page_url
    found = {}

    def add(href, priority):
        absolute = http_link(base_url, href)
        if not absolute:
            return
        absolute = urldefrag(absolute)[0]
        if absolute == urldefrag(page_url)[0]:
            return
        found[absolute] = min(found.get(absolute, priority), priority)

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        absolute = http_link(base_url, href)
        if not absolute:
            continue
        parsed = urlparse(absolute)
        label = " ".join((tag.get_text(" ", strip=True), tag.get("title", ""), tag.get("aria-label", "")))
        marked_md5 = any(k.lower() == "md5" for k, _ in parse_qsl(parsed.query)) or "/md5/" in parsed.path.lower()
        if (_looks_like_nav_link(label) or tag.find_parent(["nav", "header", "footer"])
                or any(k.lower() in {"lang", "language"} for k, _ in parse_qsl(parsed.query))):
            continue
        direct = tag.has_attr("download") or _EXT.search(parsed.path) or _PATH.search(parsed.path) or label.strip().lower() in {"get", "[get]"}
        # A site's global "Mirrors" directory is not a mirror of this file.
        if label.strip().lower() == "mirrors" and not marked_md5 and not direct:
            continue
        if direct or marked_md5 or _LABEL.search(label):
            add(href, 0 if direct else 10 if marked_md5 else 20)
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)}):
        match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)$", meta.get("content", ""), re.I)
        if match:
            add(match.group(1).strip(" '\""), 5)
    return sorted(found, key=found.get)[:limit]


def resolve(initial, requested_url, fetch, diagnose, expected_md5="", max_depth=4, max_requests=12):
    """Return (response, actual MD5, expected MD5), or raise after all mirrors.

    fetch(url, referring_page) retains the normal network checks. diagnose is
    called for unrecognized terminal bodies so failed mirrors leave evidence.
    """
    expected = normalize_md5(expected_md5)
    initial_id = url_md5(requested_url)
    if expected and initial_id and expected != initial_id:
        raise ResolutionError("입력 MD5와 URL의 MD5가 다릅니다.")
    expected = expected or initial_id
    seen = set()
    remaining = max_requests
    failures = []

    def log(event, **fields):
        print("[resolver] " + json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)

    def visit(resp, depth, checksum):
        nonlocal remaining
        final = urldefrag(resp.url)[0]
        final_id = url_md5(final)
        if checksum and final_id and checksum != final_id:
            raise ResolutionError("MD5_ID_MISMATCH: 다른 파일의 링크입니다.")
        checksum = checksum or final_id
        raw = resp.content
        if is_file(raw):
            actual = hashlib.md5(raw, usedforsecurity=False).hexdigest()
            if checksum and actual != checksum:
                raise ResolutionError(f"MD5_MISMATCH: expected={checksum}, actual={actual}, url={final}")
            log("resolved", url=final, depth=depth, md5=actual, md5_verified=bool(checksum))
            return resp, actual, checksum
        if final in seen:
            raise ResolutionError("LOOP: 이미 방문한 중간 페이지입니다.")
        seen.add(final)
        candidates = download_links(raw, final) if is_html(raw, resp.headers.get("Content-Type", "")) else []
        if not candidates:
            evidence = diagnose(raw, final, resp, ValueError("No downloadable links or recognized file"))
            raise ResolutionError(f"파일 또는 다운로드 링크를 찾지 못했습니다. {evidence}")
        if depth >= max_depth:
            raise ResolutionError(f"DEPTH_LIMIT: 최대 {max_depth}단계에 도달했습니다.")
        for candidate in candidates:
            if candidate in seen:
                continue
            if remaining <= 0:
                failures.append(f"REQUEST_LIMIT: 최대 {max_requests}개 후보 요청에 도달했습니다.")
                break
            try:
                candidate_id = url_md5(candidate)
                if checksum and candidate_id and checksum != candidate_id:
                    raise ResolutionError("MD5_ID_MISMATCH: 다른 파일의 링크입니다.")
                remaining -= 1
                log("try", url=candidate, from_url=final, depth=depth + 1)
                child = fetch(candidate, final)
                return visit(child, depth + 1, checksum or candidate_id)
            except Exception as exc:
                failures.append(str(exc))
                log("candidate_failed", url=candidate, reason=str(exc))
        raise ResolutionError("미러 후보를 모두 확인했지만 파일을 찾지 못했습니다.")

    try:
        return visit(initial, 0, expected)
    except ResolutionError as exc:
        summary = " | ".join(failures[-3:])
        raise ResolutionError(f"RESOLUTION_FAILED: {exc}" + (f" 최근 원인: {summary}" if summary else "")) from exc
