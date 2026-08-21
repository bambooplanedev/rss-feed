from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from aggregator.models import FeedSource, Article, Target
from aggregator.config import load_feeds


def test_load_feeds_parses_yaml(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(
        "feeds:\n"
        "  - name: Example\n"
        "    url: https://ex.com/feed\n"
        "    tag: ex\n"
        "    tier: 1\n"
    )
    feeds = load_feeds(str(p))
    assert feeds == [FeedSource(name="Example", url="https://ex.com/feed", tag="ex", tier=1)]


def test_load_real_feeds_file_has_expected_shape():
    feeds = load_feeds("feeds.yaml")
    assert len(feeds) >= 14
    assert all(f.name and f.url and f.tag for f in feeds)
    assert all(f.tier in (1, 2, 3) for f in feeds)


def test_load_feeds_missing_tier_raises_clear_error(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(
        "feeds:\n"
        "  - name: Example\n"
        "    url: https://ex.com/feed\n"
        "    tag: ex\n"
    )
    with pytest.raises(ValueError, match="tier"):
        load_feeds(str(p))


def _article(tier=1, tag="s"):
    return Article(
        id="1", title="T", url="https://ex.com/a", source="S", tag=tag, tier=tier,
        published=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc), summary="",
    )


def _target(**kw):
    base = dict(name="t", type="webhook", tz=ZoneInfo("UTC"), url="https://ex.com/hook")
    base.update(kw)
    return Target(**base)


@pytest.mark.parametrize(
    "filters, article_kw, expected",
    [
        ({}, {}, True),                                            # no filters: everything passes
        ({"tiers": (1, 2)}, {"tier": 1}, True),
        ({"tiers": (1, 2)}, {"tier": 3}, False),
        ({"tags": ("openai",)}, {"tag": "openai"}, True),
        ({"tags": ("openai",)}, {"tag": "wired"}, False),
        ({"exclude_tags": ("tds",)}, {"tag": "tds"}, False),
        ({"exclude_tags": ("tds",)}, {"tag": "wired"}, True),
        ({"tiers": (1,), "exclude_tags": ("tds",)}, {"tier": 1, "tag": "tds"}, False),
    ],
)
def test_target_matches(filters, article_kw, expected):
    assert _target(**filters).matches(_article(**article_kw)) is expected
