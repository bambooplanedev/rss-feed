import calendar
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser

from .models import Article, FeedSource

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 300
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw or "")
    return " ".join(parser.text().split())


def clean_summary(raw: str, limit: int = SUMMARY_LIMIT) -> str:
    text = strip_html(raw)
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0]
    return truncated + "…"


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def _published(entry) -> datetime | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    # feedparser returns a UTC struct_time; timegm treats it as UTC.
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def parse_feed(content: bytes, source: FeedSource) -> list[Article]:
    parsed = feedparser.parse(content)
    if parsed.bozo:
        log.warning("bozo feed %s: %s", source.url, parsed.get("bozo_exception"))
    articles: list[Article] = []
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not url:
            continue
        articles.append(
            Article(
                id=entry.get("id") or normalize_url(url),
                title=(entry.get("title") or "(untitled)").strip(),
                url=url,
                source=source.name,
                tag=source.tag,
                tier=source.tier,
                published=_published(entry),
                summary=clean_summary(entry.get("summary", "")),
            )
        )
    return articles
