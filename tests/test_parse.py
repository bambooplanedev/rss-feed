from datetime import timezone
from pathlib import Path

from aggregator.models import FeedSource
from aggregator.parse import parse_feed, normalize_url, clean_summary

TESTS_DIR = Path(__file__).parent

SOURCE = FeedSource(name="Test Source", url="https://ex.com/feed", tag="test", tier=1)
FIX = Path("tests/fixtures")


def test_parse_rss_returns_articles():
    articles = parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE)
    assert len(articles) == 2
    a = articles[0]
    assert a.title == "First Post"
    assert a.url == "https://ex.com/first"
    assert a.source == "Test Source"
    assert a.tag == "test"
    assert a.published.tzinfo == timezone.utc
    assert a.published.hour == 9
    assert "summary text" in a.summary


def test_parse_atom_uses_link_href_and_id():
    articles = parse_feed((FIX / "atom_sample.xml").read_bytes(), SOURCE)
    assert len(articles) == 1
    assert articles[0].url == "https://ex.com/atom1"
    assert articles[0].id == "tag:ex.com,2026:atom1"


def test_parse_malformed_feed_still_returns_entries():
    articles = parse_feed((FIX / "malformed.xml").read_bytes(), SOURCE)
    assert len(articles) >= 1
    assert articles[0].title == "Only"


def test_clean_summary_strips_html_and_truncates():
    raw = "<p>Hello <b>world</b> and more text here that keeps going on " + "y " * 200 + "</p>"
    out = clean_summary(raw)
    assert "<" not in out
    assert out.startswith("Hello world")
    assert len(out) <= 301
    assert out.endswith("…")


def test_clean_summary_short_text_unchanged():
    assert clean_summary("<p>Short &amp; sweet.</p>") == "Short & sweet."


def test_normalize_url_strips_tracking_trailing_slash_and_fragment():
    assert (
        normalize_url("https://Ex.com/Path/?utm_source=x&id=5#frag")
        == "https://ex.com/Path?id=5"
    )


def test_parse_feed_copies_tier_from_source():
    source = FeedSource(name="S", url="https://ex.com/feed", tag="s", tier=3)
    content = (TESTS_DIR / "fixtures" / "rss_sample.xml").read_bytes()

    articles = parse_feed(content, source)

    assert articles
    assert all(a.tier == 3 for a in articles)
