"""Extract HTTP(S) links from pasted notes, Markdown, HTML or table text."""
import re
from scrape import http_link

_URL = re.compile(r'https?://[^\s<>"\'`|\u200b\u3000“”‘’]+', re.I)


def extract_urls(text):
    # Decode ampersand separators without decoding percent-encoded URL data.
    text = re.sub(r'&(?:amp|#0*38|#x0*26);', '&', text, flags=re.I)
    result = []
    seen = set()
    for match in _URL.finditer(text):
        url = match.group().rstrip('.,;!。，；！')
        # Markdown/prose may wrap links in parentheses; balanced parentheses
        # inside filenames remain part of the URL.
        while url:
            previous = url
            for opening, closing in [('(', ')'), ('[', ']'), ('{', '}')]:
                if url.endswith(closing) and url.count(closing) > url.count(opening):
                    url = url[:-1].rstrip('.,;!。，；！')
            if previous == url:
                break
        if http_link('', url) and url not in seen:
            seen.add(url)
            result.append(url)
    return result
