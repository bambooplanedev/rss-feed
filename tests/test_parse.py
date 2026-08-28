import logging
from datetime import datetime, timezone
from pathlib import Path

from aggregator.models import FeedSource
from aggregator.parse import parse_feed, normalize_url, clean_summary

TESTS_DIR = Path(__file__).parent

SOURCE = FeedSource(name="Test Source", url="https://ex.com/feed", tag="test", tier=1)
FIX = Path("tests/fixtures")

# The fixtures are dated 2026-07-02. Tests that are not about the cutoff pass
# an explicit early one so they keep asserting on fixed timestamps.
OLD_CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_rss_returns_articles():
    articles = parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert len(articles) == 2
    a = articles[0]
    assert a.title == "First Post"
    assert a.url == "https://ex.com/first"
    assert a.source == "Test Source"
    assert a.tag == "test"
    assert a.published.tzinfo == timezone.utc
    assert a.published.hour == 9
    assert "summary text" in a.summary


def test_parse_atom_ignores_the_entry_id_in_favour_of_the_link():
    """The feed's own <id> is deliberately not the dedup key — see
    test_a_changed_guid_format_does_not_produce_a_duplicate."""
    articles = parse_feed((FIX / "atom_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert len(articles) == 1
    assert articles[0].url == "https://ex.com/atom1"
    assert articles[0].id == "https://ex.com/atom1"


def test_parse_malformed_feed_still_returns_entries():
    articles = parse_feed((FIX / "malformed.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
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

    articles = parse_feed(content, source, cutoff=OLD_CUTOFF)

    assert articles
    assert all(a.tier == 3 for a in articles)


def test_parse_drops_entries_older_than_the_cutoff():
    articles = parse_feed(
        (FIX / "rss_sample.xml").read_bytes(), SOURCE,
        cutoff=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    assert articles == []


def test_parse_keeps_entries_newer_than_the_cutoff():
    """First Post is 09:00, Second Post is 10:00; the cutoff falls between."""
    articles = parse_feed(
        (FIX / "rss_sample.xml").read_bytes(), SOURCE,
        cutoff=datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc),
    )
    assert [a.title for a in articles] == ["Second Post"]


def test_parse_keeps_undated_entries_and_warns(caplog):
    """Dropping them would be silent data loss, and an undated entry is the one
    case the cutoff cannot judge. The warning makes it visible instead."""
    with caplog.at_level(logging.WARNING):
        articles = parse_feed(
            (FIX / "malformed.xml").read_bytes(), SOURCE,
            cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    assert [a.url for a in articles] == ["https://ex.com/only"]
    assert "undated entry" in caplog.text


def test_parse_applies_the_default_cutoff_when_none_is_given():
    """Pins that the default is actually wired up. The fixtures are dated
    2026-07-02 and only recede further past MAX_AGE_DAYS as time passes."""
    assert parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE) == []


def test_a_changed_guid_format_does_not_produce_a_duplicate():
    """On 2026-08-13 Simon Willison's Atom feed switched <id> from
    '<url>/#atom-everything' to '<url>/' and 28 articles were delivered a
    second time. Keying on the normalized URL collapses both forms."""
    articles = parse_feed(
        (FIX / "guid_change_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF
    )
    assert len(articles) == 2
    assert {a.id for a in articles} == {"https://simonwillison.net/2026/Aug/8/auto-mode"}
