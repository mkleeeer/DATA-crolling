import io
import ipaddress
import socket
import threading
from collections import defaultdict
from contextlib import nullcontext
from urllib.parse import urlparse

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BlockedURLError(Exception):
    """Raised when a URL resolves to a non-public address. The submissions
    sheet is a shared, write-anyone surface — any AI or collaborator can put
    a URL in it — so the fetch layer refusing internal/loopback/link-local
    targets is the app's actual SSRF boundary, not a UI-level nicety."""


_ALLOWED_SCHEMES = {"http", "https"}


def _is_public_address(ip: ipaddress._BaseAddress) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Reject file://, localhost, private/loopback/link-local IPs (incl. the
    169.254.169.254 cloud metadata address, which is link-local) and
    anything that fails to resolve. Checked before the initial request AND
    against the final response.url after redirects, so a public URL that
    redirects to an internal one doesn't get its response used either —
    this doesn't stop a redirect's own network hop mid-flight, but it does
    mean internal content is never surfaced or saved."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise BlockedURLError(f"지원하지 않는 URL 형식입니다: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise BlockedURLError(f"호스트를 확인할 수 없습니다: {parsed.hostname}") from e
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_public_address(ip):
            raise BlockedURLError(f"내부망/사설 IP로의 요청은 차단됩니다: {parsed.hostname} -> {ip}")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def _new_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10, pool_connections=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# requests.Session/urllib3's connection pool is documented as thread-safe,
# but sharing one Session (and its pooled sockets) across Flask's worker
# threads plus the background extractor/download threads was producing
# random "[SSL: WRONG_VERSION_NUMBER]"/"bad record mac" errors on completely
# unrelated domains (whitehouse.gov, 동아일보, 가디언, ...) — the same class
# of cross-thread socket corruption already seen and fixed in sheets.py.
# Giving each thread its own Session removes the shared-socket race.
_local = threading.local()


def _session() -> requests.Session:
    if getattr(_local, "session", None) is None:
        _local.session = _new_session()
    return _local.session


# Cap how many upstream fetches run at the same time so a burst of requests
# (e.g. a batch image download) doesn't itself look like abuse to the CDN,
# and doesn't itself open enough concurrent sockets to start corrupting them.
fetch_limiter = threading.Semaphore(2)

# Some hosts (Wikimedia's thumbnail CDN in particular) rate-limit far more
# aggressively than the general limiter above allows, and keep 429-ing even
# under fetch_limiter's cap — so give known-strict hosts their own tighter,
# per-host limiter on top of the general one.
_STRICT_HOSTS = {"upload.wikimedia.org", "commons.wikimedia.org"}
_host_limiters = defaultdict(lambda: threading.Semaphore(1))


def _limiter_for(url: str):
    host = urlparse(url).netloc.lower()
    return _host_limiters[host] if host in _STRICT_HOSTS else None


def image_headers(image_url: str, page_url: str) -> dict:
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}" if page_url else ""
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = page_url or origin or image_url
    return headers


def fetch_image(image_url: str, page_url: str = "", stream: bool = False, cookies: dict | None = None):
    """cookies is passed through only when a caller explicitly supplies it
    (e.g. the user's own session cookie for a site they're already logged
    into) — nothing here derives, stores, or reuses credentials on its own."""
    assert_public_url(image_url)
    host_limiter = _limiter_for(image_url)
    with fetch_limiter:
        if host_limiter is not None:
            with host_limiter:
                resp = _session().get(image_url, headers=image_headers(image_url, page_url), timeout=15, stream=stream, cookies=cookies)
        else:
            resp = _session().get(image_url, headers=image_headers(image_url, page_url), timeout=15, stream=stream, cookies=cookies)
    assert_public_url(resp.url)
    return resp


def fetch_page(url: str, stream: bool = False):
    """stream=True returns as soon as headers arrive, so a caller that only
    needs to know *what* the URL is (e.g. a 50MB PDF vs. an HTML page) can
    decide without pulling the whole body — the caller must close it."""
    assert_public_url(url)
    with fetch_limiter:
        resp = _session().get(url, headers=BROWSER_HEADERS, timeout=15, stream=stream)
    try:
        assert_public_url(resp.url)
    except BlockedURLError:
        resp.close()
        raise
    return resp


# How much of an image to download just to read its dimensions from the
# header (JPEG/PNG/GIF/WebP all expose width/height within the first few KB)
# — capped well below a real photo's size so probing a bad candidate is
# cheap, unlike downloading it in full only to reject it afterwards.
_DIMENSION_PROBE_CAP = 65536


def probe_image_dimensions(image_url: str, page_url: str = ""):
    """Peek at an image's real pixel size without downloading the whole
    file, so obviously-too-small/oddly-shaped candidates (icons, tracking
    pixels, thin banner strips) can be dropped before they ever cost a full
    download. Returns (width, height), or None if it can't be determined
    (e.g. truncated before Pillow could parse the header) — callers should
    treat None as "unknown" and let the candidate through, not reject it."""
    try:
        assert_public_url(image_url)
    except BlockedURLError:
        return None
    host_limiter = _limiter_for(image_url)
    try:
        with fetch_limiter:
            with (host_limiter or nullcontext()):
                resp = _session().get(
                    image_url, headers=image_headers(image_url, page_url), timeout=8, stream=True,
                )
                try:
                    resp.raise_for_status()
                    buf = io.BytesIO()
                    for chunk in resp.iter_content(chunk_size=4096):
                        buf.write(chunk)
                        if buf.tell() >= _DIMENSION_PROBE_CAP:
                            break
                        buf.seek(0)
                        try:
                            with Image.open(buf) as im:
                                return im.size
                        except Exception:
                            # Not enough bytes yet to parse a header, or an
                            # unrecognized/corrupt stream — either way, keep
                            # buffering until the cap, then give up quietly.
                            buf.seek(0, io.SEEK_END)
                    return None
                finally:
                    resp.close()
    except requests.RequestException:
        return None
