"""게시물 목록 페이지 → 글 목록.

A board's list page ("이슈노트 목록", "보도자료 목록") is where someone wants
to start a bulk download, but the files sit one level down, on each post's
own page. This finds the post links on a list page so the PDF crawler can
open each post and gather its files, instead of making the user paste every
post URL by hand.
"""
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

import net
from scrape import file_ext_of, is_generic_link_text

_MIN_POSTS = 5
_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
# Post rows: the smallest usual "one item" container around a link.
_ROW_TAGS = ["li", "tr", "article", "dl"]
# How many posts a list page asks for at once when the site lets us choose.
_LIST_PAGE_SIZE = 1000


def _script_list_url(page_url: str) -> str | None:
    """Some list pages are an empty shell whose post list is filled in by a
    script from a second URL — reading the shell finds nothing but menus.
    For those, return the URL the list really comes from.

    Bank of Korea: /portal/singl/newsData/list.do loads its rows from
    listCont.do, paged by pageIndex/pageUnit. The board filter (depth2 =
    parent menu, depth3 = this board) is often missing from the address bar
    (list.do?menuNo=200066) — the page fetches it from depthInitLoad.json,
    and without it listCont.do returns every board mixed together."""
    p = urlparse(page_url)
    if not (p.netloc.endswith("bok.or.kr") and p.path.endswith("/newsData/list.do")):
        return None
    base = p.path[: -len("list.do")]
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    if not query.get("depth2"):
        init_url = urlunparse(p._replace(path=base + "depthInitLoad.json", query=urlencode(query)))
        resp = net.fetch_page(init_url)
        resp.raise_for_status()
        depth_query = dict(parse_qsl(resp.json().get("pageQueryString", ""), keep_blank_values=True))
        query.update({k: v for k, v in depth_query.items() if k in ("depth2", "depth3", "depth") and v})
    query.update(pageIndex="1", pageUnit=str(_LIST_PAGE_SIZE))
    return urlunparse(p._replace(path=base + "listCont.do", query=urlencode(query)))


def _link_pattern(url: str):
    """Post links on one board share a shape and differ only in values —
    view.do?nttId=11064677&... vs view.do?nttId=11064490&... — while menu,
    footer and SNS links each look different. Digits in the path are
    wildcarded so /speech/waller20260903a.htm and /speech/bowman20260828a.htm
    also count as one shape."""
    p = urlparse(url)
    keys = tuple(sorted({k for k, _ in parse_qsl(p.query, keep_blank_values=True)}))
    return p.netloc, re.sub(r"\d+", "#", p.path), keys


def _row_of(a):
    return a.find_parent(_ROW_TAGS) or a.parent


def _date_in(text: str) -> str:
    m = _DATE_RE.search(text or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def detect_posts(html: str, page_url: str) -> list:
    """The repeated post links on a list page, in page order:
    [{"url", "title", "date"}]. Picks the group of same-shaped links that
    looks most like a post list — at least _MIN_POSTS distinct links, rows
    that carry a date and titles longer than menu labels. Returns [] when
    the page has no such group (a single article, a home page)."""
    soup = BeautifulSoup(html, "html.parser")
    groups = defaultdict(dict)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        if urlparse(url).scheme not in ("http", "https") or file_ext_of(urlparse(url).path):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if is_generic_link_text(title):  # "첨부파일 전체다운로드" buttons repeat per row too
            continue
        post = groups[_link_pattern(url)].setdefault(url, {"url": url, "title": "", "date": ""})
        if len(title) > len(post["title"]):
            post["title"] = title
        post["date"] = post["date"] or _date_in(_row_of(a).get_text(" ", strip=True))

    best, best_score = [], 0.0
    for posts in groups.values():
        posts = [p for p in posts.values() if p["title"]]
        if len(posts) < _MIN_POSTS:
            continue
        dated = sum(1 for p in posts if p["date"]) / len(posts)
        avg_title = sum(len(p["title"]) for p in posts) / len(posts)
        # A board's rows carry dates; a menu's don't. Undated groups only
        # count when they're long lists of sentence-length titles.
        if dated < 0.5 and not (len(posts) >= 10 and avg_title >= 25):
            continue
        score = len(posts) * (1 + dated) * min(avg_title, 40)
        if score > best_score:
            best, best_score = posts, score
    return best


def find_posts(html: str, page_url: str) -> tuple[list, str]:
    """(posts, the URL the list was read from). Follows a script-filled list
    page to its real list URL first; falls back to the page as given."""
    try:
        list_url = _script_list_url(page_url)
    except Exception as e:
        print(f"[listing] could not resolve script list URL for {page_url}: {e}")
        list_url = None
    if list_url:
        try:
            resp = net.fetch_page(list_url)
            resp.raise_for_status()
            posts = detect_posts(resp.text, resp.url)
            if posts:
                return posts, list_url
        except Exception as e:
            print(f"[listing] script list fetch failed, using page as-is: {list_url} ({e})")
    return detect_posts(html, page_url), page_url
