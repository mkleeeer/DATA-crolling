import re
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

# Filenames/paths that are almost never the real content photo we want —
# site logos, UI icons, social-share badges, tracking pixels. Checked
# against the URL path only (not query string), case-insensitively, so a
# real photo whose CDN happens to append "?ref=logo_page" isn't caught.
# Based on the same kind of heuristics newspaper3k's image scorer and
# common logo-scraper filename rules use.
_JUNK_URL_PATTERNS = re.compile(
    r"(icon|logo|sprite|badge|avatar|favicon|creativecommons|copyleft|public.?domain)",
    re.IGNORECASE,
)


def _looks_like_junk(url: str) -> bool:
    path = urlparse(url).path.lower()
    # MediaWiki-style thumbnail URLs embed the *original* filename mid-path
    # (.../thumb/2/22/Some_Badge.svg/250px-Some_Badge.svg.png) — any segment
    # ending in .svg means the source was never a photo, regardless of what
    # that particular badge/diagram happens to be named.
    if any(seg.endswith(".svg") for seg in path.split("/")):
        return True
    return bool(_JUNK_URL_PATTERNS.search(path))


def find_pdf_links(soup: BeautifulSoup, page_url: str, limit: int = 5) -> list:
    """Direct links to a .pdf file on a page — used when a submitted URL
    turns out to be an HTML landing/redirect page rather than the file
    itself (a "click here to download" page), so the download step can
    follow one more hop to reach the actual PDF. Returns candidates in
    document order; the caller tries them until one verifies as a real PDF,
    since a page can have a stale/broken link before a working one."""
    found = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path.lower()
        if not path.endswith(".pdf") or absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found


def extract_links_from_html(html: str, page_url: str) -> list:
    """All <a href> links on a page (not just images) — the "Link Gopher"
    style bulk-link-listing feature, used so a resource/index page's PDF
    (or other file) links can be picked out and queued without opening
    each one individually."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "ftp:", "#")):
            continue
        absolute = urljoin(page_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append({"url": absolute, "text": a.get_text(strip=True)[:150]})
    return links


# Generic site nav/utility labels — checked as an *exact* match on the
# trimmed, lowercased link text, never as a substring, so real content
# whose title happens to contain one of these words isn't caught. Not tied
# to any site's markup, URL scheme, or result count on purpose: a CSS
# selector or "assume N results per page" rule only works for the one page
# it was tuned against.
_NAV_TEXT_EXACT = {
    "login", "log in", "log-in", "logon", "sign in", "sign-in", "signin",
    "register", "sign up", "sign-up", "signup", "logout", "log out",
    "home", "forum", "forums", "news", "rss", "contact", "contact us",
    "about", "about us", "help", "faq", "terms", "privacy", "sitemap",
    "menu", "search",
    "로그인", "로그아웃", "회원가입", "홈", "포럼", "뉴스", "문의", "문의하기",
    "소개", "회사소개", "고객센터", "이용약관", "개인정보", "개인정보처리방침",
    "사이트맵", "검색",
}

# Pagination controls and bare language switchers — also exact-match only.
_PAGINATION_TEXT_RE = re.compile(
    r"^(\d{1,4}|next|prev|previous|first|last|more|»|«|›|‹|\.\.\.|다음|이전|처음|마지막|더보기)$",
    re.IGNORECASE,
)
_LANG_TEXT_RE = re.compile(
    r"^(english|한국어|ko|en|ja|zh|fr|de|es|ru|中文|日本語|español|français|deutsch|русский)$",
    re.IGNORECASE,
)


def _looks_like_nav_link(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    return t in _NAV_TEXT_EXACT or bool(_PAGINATION_TEXT_RE.match(t)) or bool(_LANG_TEXT_RE.match(t))


def filter_navigation_links(links: list) -> list:
    """Drop the obvious site-chrome links (login/register/home/forum/news/
    rss/contact/about, language switchers, pagination) out of a raw
    extract_links_from_html() result, leaving everything else as candidates
    for the resolver/downloader. Text-keyword and scheme based only — no
    domain names, CSS selectors, or "N results per page" assumptions, since
    those only hold for the one page they were tuned against."""
    return [link for link in links if not _looks_like_nav_link(link.get("text", ""))]


# Words that describe the button, not the document — a link text made only
# of these ("다운로드", "PDF파일 다운로드", "첨부파일 다운로드", "Full Text")
# is useless both as a label in the link list and as a filename.
GENERIC_LINK_WORDS = {
    "다운로드", "내려받기", "download", "첨부파일", "첨부", "파일", "file", "files", "pdf", "pdf파일", "뷰어",
    "viewer", "보기", "바로보기", "미리보기", "view", "open", "열기", "원문", "원문보기", "링크", "link", "here",
    "click", "full", "text", "html", "attachment", "첨부파일다운로드", "원문다운로드",
    "자료", "전체", "일괄", "전체다운로드", "일괄다운로드", "all", "zip",
    "문서", "문서보기", "바로가기", "뷰어보기",
}
_ID_LIKE_STEM = re.compile(r"^[0-9a-fA-F_\-]{16,}$")

# Document formats the "파일만" link view lists. Pages (.htm/.do/...) and
# images are never in it — the image crawler covers pictures.
DOCUMENT_EXTS = {"pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "zip", "txt", "epub"}

# Download endpoints whose URL doesn't reveal the file type — the generic
# words board software everywhere uses (FileDown.do, download?atch_no=,
# atchFileId=, fileSn=), not any one site's URL scheme. Only a hint: such a
# link is probed to learn what it really is before it's listed.
_DOWNLOAD_SCRIPT_RE = re.compile(r"(down(load)?|atch_?file|attach|file_?(seq|sn|id|no))", re.IGNORECASE)
_MAX_CONTEXT_LENGTH = 150


def is_generic_link_text(text: str | None) -> bool:
    words = re.findall(r"[^\s\[\]()<>|·:,/\-]+", (text or "").lower())
    return all(w in GENERIC_LINK_WORDS for w in words)


def file_ext_of(name: str | None) -> str:
    """Document extension a filename or link text ends with, or ""."""
    m = re.search(r"\.([a-z0-9]{2,5})\s*$", (name or "").lower())
    return m.group(1) if m and m.group(1) in DOCUMENT_EXTS else ""


def file_name_key(name: str | None) -> str:
    """Comparable form of a file's name, so the same document reached through
    two different URLs ("/files/x.pdf" and "fileDown.do?...") can be merged."""
    return re.sub(r"\s+", "", unquote(name or "")).lower()


def _readable_basename(url: str) -> str | None:
    basename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    stem = basename.rsplit(".", 1)[0]
    if not file_ext_of(basename) or _ID_LIKE_STEM.match(stem):
        return None
    return basename


def _context_label(a) -> str | None:
    """For a bare "PDF"/"다운로드" link whose URL is no help either, the
    nearest enclosing element's own text usually says what the file is.
    Text inside *other* links in that element is skipped — in a row like
    "May: HTML | PDF | Chart Data" the neighbouring links name other things."""
    node = a.parent
    for _ in range(3):
        if node is None or node.name in ("body", "html"):
            return None
        pieces = [s.strip() for s in node.find_all(string=True) if s.strip() and s.find_parent("a") is None]
        text = re.sub(r"\s+", " ", " ".join(p for p in pieces if not is_generic_link_text(p))).strip(" :|·-")
        if text:
            return text if len(text) <= _MAX_CONTEXT_LENGTH else None
        node = node.parent
    return None


def extract_file_links(html: str, page_url: str) -> tuple[list, list]:
    """Just the document links on a page, one row per file — for the PDF
    crawler's "파일만" view, where showing every <a> (menus, SNS, footer)
    buries the one or two files someone actually came for.

    Returns (file_links, unverified_links):
      - file_links: URL path ends in a document extension (a viewer page like
        viewer.html?file=x.pdf does not count), or a download-script link
        whose own text names one
      - unverified_links: download-script links with no type hint — the
        caller should probe these to learn whether they're files at all
    Each item: {"url", "ext", "text" (best label to show), "title" (only a
    label the page itself gave that file — safe as a filename, else "")}.
    Several links to the same file ("보고서.pdf", "다운로드") collapse into one."""
    soup = BeautifulSoup(html, "html.parser")
    entries = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urldefrag(urljoin(page_url, href))[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        labels = [re.sub(r"\s+", " ", t).strip() for t in (a.get_text(" ", strip=True), a.get("title") or "")]
        name = next((t for t in labels if not is_generic_link_text(t)), None)

        ext = file_ext_of(parsed.path)
        if not ext:
            if not _DOWNLOAD_SCRIPT_RE.search(f"{parsed.path}?{parsed.query}"):
                continue
            ext = file_ext_of(name)  # "보고서.hwp" on a FileDown.do link; "" = unknown

        entry = entries.setdefault(absolute, {"url": absolute, "ext": ext, "name": None, "context": None})
        entry["ext"] = entry["ext"] or ext
        if name and not entry["name"]:
            entry["name"] = name
        if not entry["name"] and not entry["context"]:
            entry["context"] = _context_label(a)

    files, unverified = [], []
    for e in entries.values():
        basename = unquote(urlparse(e["url"]).path.rsplit("/", 1)[-1])
        text = e["name"] or _readable_basename(e["url"]) or e["context"] or basename
        item = {"url": e["url"], "ext": e["ext"], "text": text, "title": e["name"] or ""}
        (files if e["ext"] else unverified).append(item)
    return files, unverified


def largest_from_srcset(srcset: str) -> str:
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        candidates.append((width, url))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def find_og_image(soup: BeautifulSoup, page_url: str) -> str:
    """The page's publisher-declared representative photo (what shows up as
    the preview when the page is shared on social media / search results) —
    the closest thing to "already knows which image is the real one",
    since it's a single deliberate choice by the site, not every <img> tag
    on the page treated as equally likely to be the content photo."""
    for meta_name in ("og:image", "twitter:image"):
        tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        content = tag.get("content") if tag else None
        if content and not content.strip().startswith("data:"):
            return urljoin(page_url, content.strip())
    return ""


def extract_images_from_html(html: str, page_url: str):
    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()

    def add(url, alt=""):
        if not url:
            return
        url = url.strip()
        if url.startswith("data:"):
            return
        absolute = urljoin(page_url, url)
        if absolute in seen:
            return
        seen.add(absolute)
        if _looks_like_junk(absolute):
            return
        found.append({"url": absolute, "alt": alt[:120]})

    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
        )
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            best = largest_from_srcset(srcset)
            if best:
                add(best, alt)
                continue
        add(src, alt)

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            add(largest_from_srcset(srcset))

    for tag in soup.find_all(style=True):
        m = re.search(r"background-image\s*:\s*url\((.*?)\)", tag["style"])
        if m:
            add(m.group(1).strip("'\""))

    og_image = find_og_image(soup, page_url)
    if og_image:
        add(og_image)

    return found
