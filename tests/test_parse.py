import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aggregator.models import FeedSource
from aggregator.parse import (
    MAX_AGE_DAYS, clean_summary, cutoff_now, normalize_url, parse_feed,
)

TESTS_DIR = Path(__file__).parent

SOURCE = FeedSource(name="Test Source", url="https://ex.com/feed", tag="test", tier=1)
FIX = Path("tests/fixtures")

# The fixtures are dated 2026-07-02. Tests that are not about the cutoff pass
# an explicit early one so they keep asserting on fixed timestamps.
OLD_CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_rss_returns_articles():
    articles = parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF).articles
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
    articles = parse_feed((FIX / "atom_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF).articles
    assert len(articles) == 1
    assert articles[0].url == "https://ex.com/atom1"
    assert articles[0].id == "https://ex.com/atom1"


def test_parse_malformed_feed_still_returns_entries():
    result = parse_feed((FIX / "malformed_dated.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert [a.url for a in result.articles] == ["https://ex.com/only"]


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

    articles = parse_feed(content, source, cutoff=OLD_CUTOFF).articles

    assert articles
    assert all(a.tier == 3 for a in articles)


def test_parse_drops_entries_older_than_the_cutoff():
    articles = parse_feed(
        (FIX / "rss_sample.xml").read_bytes(), SOURCE,
        cutoff=datetime(2026, 7, 3, tzinfo=timezone.utc),
    ).articles
    assert articles == []


def test_parse_keeps_entries_newer_than_the_cutoff():
    """First Post is 09:00, Second Post is 10:00; the cutoff falls between."""
    articles = parse_feed(
        (FIX / "rss_sample.xml").read_bytes(), SOURCE,
        cutoff=datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc),
    ).articles
    assert [a.title for a in articles] == ["Second Post"]


def test_parse_drops_entries_whose_date_cannot_be_parsed(caplog):
    """The 30-day cutoff is what makes an archive dump unrepresentable. An entry
    with no usable date skips that test entirely, so it cannot be kept."""
    with caplog.at_level(logging.WARNING):
        result = parse_feed(_entry_feed("July 2, 2026"), SOURCE, cutoff=OLD_CUTOFF)
    assert result.articles == []
    assert result.undated == 1
    assert "unparseable or missing date" in caplog.text


def test_parse_drops_entries_with_no_date_element_at_all():
    result = parse_feed((FIX / "malformed.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert result.articles == []
    assert result.undated == 1


def test_no_article_from_parse_feed_ever_has_a_null_published():
    """The single guarantee behind Article.published being non-optional."""
    for name in (
        "rss_sample.xml", "atom_sample.xml", "guid_change_sample.xml",
        "malformed_dated.xml", "malformed.xml",
    ):
        result = parse_feed((FIX / name).read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
        assert all(a.published is not None for a in result.articles), name


def test_cutoff_now_is_max_age_days_behind_the_current_moment():
    """parse_feed has no default cutoff any more: one cutoff is computed per
    run and threaded down, so every feed in a run is bounded by the same
    instant rather than by whenever its own fetch happened to finish."""
    before = datetime.now(timezone.utc)
    cutoff = cutoff_now()
    assert before - timedelta(days=MAX_AGE_DAYS) <= cutoff
    assert cutoff <= datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)


def test_a_changed_guid_format_does_not_produce_a_duplicate():
    """On 2026-08-13 Simon Willison's Atom feed switched <id> from
    '<url>/#atom-everything' to '<url>/' and 28 articles were delivered a
    second time. Keying on the normalized URL collapses both forms."""
    articles = parse_feed(
        (FIX / "guid_change_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF
    ).articles
    assert len(articles) == 2
    assert {a.id for a in articles} == {"https://simonwillison.net/2026/Aug/8/auto-mode"}


def _entry_feed(raw_date: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        "<item><title>t</title><link>https://ex.com/a</link>"
        f"<pubDate>{raw_date}</pubDate><description>d</description></item>"
        "</channel></rss>"
    ).encode()


def test_recovers_rfc2822_with_a_non_english_day_name():
    """feedparser's handlers key on English day names; parsedate_to_datetime
    ignores the day name entirely. A WordPress locale change is the realistic
    way a feed lands here."""
    articles = parse_feed(_entry_feed("Mi, 02 Jul 2026 09:00:00 +0200"), SOURCE, cutoff=OLD_CUTOFF).articles
    assert len(articles) == 1
    assert articles[0].published == datetime(2026, 7, 2, 7, 0, tzinfo=timezone.utc)


def test_recovers_iso_variants():
    for raw, expected in [
        ("2026-07-02T09:00:00Z", datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)),
        ("2026-07-02 09:00:00", datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)),
        ("2026-07-02", datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)),
    ]:
        articles = parse_feed(_entry_feed(raw), SOURCE, cutoff=OLD_CUTOFF).articles
        assert articles and articles[0].published == expected, raw


def test_a_naive_recovered_date_is_treated_as_utc():
    articles = parse_feed(_entry_feed("2026-07-02 09:00:00"), SOURCE, cutoff=OLD_CUTOFF).articles
    assert articles[0].published.tzinfo is not None


def _entry_feed_with_updated(published_raw: str, updated_raw: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel><title>T</title>"
        "<item><title>t</title><link>https://ex.com/a</link>"
        f"<pubDate>{published_raw}</pubDate><atom:updated>{updated_raw}</atom:updated>"
        "<description>d</description></item></channel></rss>"
    ).encode()


def test_falls_back_to_updated_when_published_is_unparseable():
    """A broken <pubDate> alongside a good <atom:updated> must be recovered,
    not dropped just because `published` was non-empty garbage."""
    content = _entry_feed_with_updated("not a date", "Mi, 02 Jul 2026 09:00:00 +0200")
    articles = parse_feed(content, SOURCE, cutoff=OLD_CUTOFF).articles
    assert len(articles) == 1
    assert articles[0].published == datetime(2026, 7, 2, 7, 0, tzinfo=timezone.utc)


def test_an_absurd_year_drops_only_that_entry_not_the_feed():
    """parsedate_to_datetime raises OverflowError, not ValueError, on an absurd
    year. That must cost one entry, never the whole feed."""
    result = parse_feed(
        _entry_feed("Wed, 02 Jul 12345678901234567890 09:00:00 GMT"), SOURCE, cutoff=OLD_CUTOFF
    )
    assert result.articles == []
    assert result.undated == 1


def test_parse_reports_bozo_for_a_malformed_document():
    """`bozo` is how collect_articles tells a Cloudflare interstitial from a
    quiet feed — both yield few or no articles without raising."""
    result = parse_feed((FIX / "malformed_dated.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert result.bozo is True
    assert result.articles                      # still tolerant: entries survive


def test_parse_reports_not_bozo_for_a_clean_document():
    result = parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE, cutoff=OLD_CUTOFF)
    assert result.bozo is False


def test_normalize_url_keeps_a_param_that_merely_starts_with_ref():
    """`ref` was matched as a prefix, so `referrer`, `reference` and `refresh`
    were stripped too. Two distinct URLs then collapse onto one id and the
    second article is silently never delivered."""
    assert (
        normalize_url("https://ex.com/a?referrer=x&reference=7")
        == "https://ex.com/a?referrer=x&reference=7"
    )


def test_normalize_url_still_strips_a_bare_ref_param():
    assert normalize_url("https://ex.com/a?ref=hn&id=5") == "https://ex.com/a?id=5"
