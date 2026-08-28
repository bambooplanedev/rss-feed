import calendar
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser

from .models import Article, FeedSource

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 300
# The RSS window is not the publication window. OpenAI's feed carried 1155
# entries back to 2015; DeepMind's 100. Before this cutoff, any id evicted from
# `seen` was re-offered by the feed, looked new, and was delivered oldest-first
# at 20 per run — 1343 distinct articles were reposted that way between
# 2026-08-01 and 2026-08-21. Bounding by publication date makes that
# unrepresentable rather than merely unlikely.
MAX_AGE_DAYS = 30
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


# This is the dedup key, not a cosmetic tidy-up. A publisher that changes its
# guid format — Simon Willison's Atom feed did on 2026-08-13 — otherwise
# reposts every article still inside its window under a second id.
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
    if st:
        # feedparser returns a UTC struct_time; timegm treats it as UTC.
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)

    # feedparser reports None for a date it merely could not match — a German
    # day name, an ISO string with a space separator — which is a format change
    # with no intent behind it, not a publisher dropping dates. RSS 2.0 mandates
    # RFC 822 and Atom mandates RFC 3339, so these two cover the compliant world
    # plus the common locale bug. What still fails here is hand-rolled, and
    # collect_articles escalates a feed that produces nothing but those.
    #
    # Tried in turn, not `or`-chained: a broken <pubDate> alongside a good
    # <atom:updated> must not be dropped just because published was non-empty.
    for candidate in (entry.get("published"), entry.get("updated")):
        raw = (candidate or "").strip()
        if not raw:
            continue
        for parse in (parsedate_to_datetime, datetime.fromisoformat):
            try:
                dt = parse(raw)
            except Exception:
                # Deliberately broad: parsedate_to_datetime raises OverflowError
                # (not ValueError) on an absurd year, and datetime.fromisoformat
                # can raise other things on garbage input too. A malformed date
                # must cost one entry, never the whole feed — collect_articles'
                # blanket `except Exception` is what would otherwise discard it.
                continue
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


class ParseResult(NamedTuple):
    articles: list[Article]
    undated: int   # entries dropped because no date could be recovered
    dated: int = 0  # entries that yielded a usable date, before the cutoff is applied


def parse_feed(
    content: bytes, source: FeedSource, *, cutoff: datetime | None = None
) -> ParseResult:
    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    parsed = feedparser.parse(content)
    if parsed.bozo:
        log.warning("bozo feed %s: %s", source.url, parsed.get("bozo_exception"))
    articles: list[Article] = []
    undated = 0
    dated = 0
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not url:
            continue
        published = _published(entry)
        if published is None:
            # Dropped, not kept: an undated entry bypasses the cutoff, and the
            # cutoff is what makes an archive dump unrepresentable. This
            # `continue` is the only thing making Article.published non-optional.
            log.warning("unparseable or missing date in %s: %s (entry dropped)", source.url, url)
            undated += 1
            continue
        dated += 1
        if published < cutoff:
            continue
        articles.append(
            Article(
                id=normalize_url(url),
                title=(entry.get("title") or "(untitled)").strip(),
                url=url,
                source=source.name,
                tag=source.tag,
                tier=source.tier,
                published=published,
                summary=clean_summary(entry.get("summary", "")),
            )
        )
    return ParseResult(articles, undated, dated)
