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
    assert len(feeds) >= 13
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


from aggregator.config import load_targets


def _write(tmp_path, body):
    p = tmp_path / "targets.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_load_targets_resolves_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://hooks.example/abc")
    path = _write(tmp_path, "targets:\n  - name: d\n    type: discord\n    url: ${HOOK}\n")

    targets, skipped = load_targets(path)

    assert skipped == []
    assert targets[0].url == "https://hooks.example/abc"


def test_load_targets_keeps_literal_values(tmp_path):
    path = _write(tmp_path, "targets:\n  - name: d\n    type: discord\n    url: https://literal.example\n")
    targets, _ = load_targets(path)
    assert targets[0].url == "https://literal.example"


def test_load_targets_does_not_interpolate_inside_a_string(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "abc")
    path = _write(tmp_path, "targets:\n  - name: d\n    type: discord\n    url: https://x/${HOOK}\n")
    targets, _ = load_targets(path)
    assert targets[0].url == "https://x/${HOOK}"


def test_unset_env_skips_only_that_target(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_HOOK", raising=False)
    path = _write(tmp_path, (
        "targets:\n"
        "  - name: tg\n    type: telegram\n    token: T\n    chat_id: '1'\n"
        "  - name: d\n    type: discord\n    url: ${MISSING_HOOK}\n"
    ))

    targets, skipped = load_targets(path)

    assert [t.name for t in targets] == ["tg"]
    assert skipped == ["d"]


def test_unset_env_error_names_the_variable_not_the_value(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PRESENT", "s3cret")
    monkeypatch.delenv("ABSENT_HOOK", raising=False)
    path = _write(tmp_path, "targets:\n  - name: d\n    type: discord\n    url: ${ABSENT_HOOK}\n")

    with caplog.at_level("ERROR"):
        load_targets(path)

    assert "ABSENT_HOOK" in caplog.text
    assert "s3cret" not in caplog.text


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("targets:\n  - name: d\n    type: carrier_pigeon\n    url: u\n", "carrier_pigeon"),
        ("targets:\n  - name: d\n    type: discord\n    url: u\n  - name: d\n    type: slack\n    url: u\n", "duplicate"),
        ("targets:\n  - name: d\n    type: discord\n", "url"),
        ("targets:\n  - name: t\n    type: telegram\n    token: T\n", "chat_id"),
        ("targets:\n  - name: d\n    type: discord\n    url: u\n    chat_ids: '1'\n", "chat_ids"),
        ("targets:\n  - name: d\n    type: discord\n    url: u\n    tz: Europe/Kiyv\n", "Kiyv"),
        ("targets:\n  - type: discord\n    url: u\n", "name"),
        ("targets: []\n", "no targets"),
    ],
)
def test_malformed_config_raises(tmp_path, body, fragment):
    path = _write(tmp_path, body)
    with pytest.raises(ValueError) as exc:
        load_targets(path)
    assert fragment in str(exc.value)


def test_tz_defaults_and_overrides(tmp_path):
    path = _write(tmp_path, (
        "targets:\n"
        "  - name: a\n    type: discord\n    url: u\n"
        "  - name: b\n    type: discord\n    url: u2\n    tz: Europe/Kyiv\n"
    ))
    targets, _ = load_targets(path, default_tz="America/New_York")
    assert str(targets[0].tz) == "America/New_York"
    assert str(targets[1].tz) == "Europe/Kyiv"


def test_load_shipped_targets_yaml(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    targets, skipped = load_targets("targets.yaml")

    assert [t.name for t in targets] == ["tg-main"]
    assert skipped == []


def test_filters_default_to_empty_and_become_tuples(tmp_path):
    path = _write(tmp_path, (
        "targets:\n"
        "  - name: a\n    type: discord\n    url: u\n"
        "  - name: b\n    type: discord\n    url: u2\n    tiers: [1, 2]\n    exclude_tags: [tds]\n"
    ))
    targets, _ = load_targets(path)
    assert targets[0].tiers == () and targets[0].tags == () and targets[0].exclude_tags == ()
    assert targets[1].tiers == (1, 2)
    assert targets[1].exclude_tags == ("tds",)


def test_load_feeds_rejects_duplicate_tags(tmp_path):
    """seeded_tags keys on `tag`, so two feeds sharing one are indistinguishable:
    seeding either would mark both seeded and swallow the other's window."""
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "feeds:\n"
        "  - {name: A, url: https://ex.com/a, tag: dup, tier: 1}\n"
        "  - {name: B, url: https://ex.com/b, tag: dup, tier: 2}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dup"):
        load_feeds(str(path))
