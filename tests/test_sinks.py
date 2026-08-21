import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from aggregator import sinks
from aggregator.models import Article, Target


def _article(**kw):
    base = dict(
        id="1", title="Big <AI> News", url="https://ex.com/a",
        source="TechCrunch", tag="techcrunch", tier=1,
        published=datetime(2026, 7, 2, 9, 5, tzinfo=timezone.utc),
        summary="A & B happened",
    )
    base.update(kw)
    return Article(**base)


def _target(type_, **kw):
    base = dict(name=f"t-{type_}", type=type_, tz=ZoneInfo("UTC"), url="https://ex.com/hook")
    if type_ == "telegram":
        base.update(token="TOKEN", chat_id="123", url="")
    base.update(kw)
    return Target(**base)


def test_telegram_render_is_byte_identical_to_the_old_formatter():
    """Regression guard: the live Telegram output must not change at all."""
    text = sinks.payload(_article(), _target("telegram"))["text"]
    assert text == (
        "🔹 <b>Big &lt;AI&gt; News</b>\n"
        "TechCrunch · 09:05, 02 Jul\n"
        "\n"
        "A &amp; B happened\n"
        "\n"
        "https://ex.com/a\n"
        "#techcrunch"
    )


@pytest.mark.parametrize(
    "type_, key, open_b",
    [("telegram", "text", "<b>"), ("discord", "content", "**"), ("slack", "text", "*")],
)
def test_payload_key_and_bold_marker_per_type(type_, key, open_b):
    body = sinks.payload(_article(), _target(type_))
    assert body[key].startswith(f"🔹 {open_b}")


def test_missing_published_renders_dash():
    text = sinks.payload(_article(published=None), _target("slack"))["text"]
    assert text.split("\n")[1] == "TechCrunch · —"


def test_empty_summary_block_is_omitted():
    text = sinks.payload(_article(summary=""), _target("discord"))["content"]
    assert "\n\n\n" not in text
    assert "https://ex.com/a" in text


def test_target_tz_is_applied():
    target = _target("slack", tz=ZoneInfo("Europe/Kyiv"))
    text = sinks.payload(_article(), target)["text"]
    assert text.split("\n")[1] == "TechCrunch · 12:05, 02 Jul"


def test_slack_escapes_channel_ping():
    text = sinks.payload(_article(title="<!channel> hi"), _target("slack"))["text"]
    assert "&lt;!channel&gt;" in text
    assert "<!channel>" not in text


def test_discord_escapes_markdown_link_in_title():
    text = sinks.payload(
        _article(title="[Free iPhone](https://evil.example)"), _target("discord")
    )["content"]
    assert r"\[Free iPhone\]" in text
    assert "[Free iPhone](https://evil.example)" not in text


def test_discord_escapes_the_blockquote_marker():
    """A summary starting with '>' would render as a blockquote."""
    text = sinks.payload(_article(summary="> quoted"), _target("discord"))["content"]
    assert r"\> quoted" in text


def test_discord_does_not_escape_the_url():
    """Backslashes before _ and ( break Discord autolinking."""
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    text = sinks.payload(_article(url=url), _target("discord"))["content"]
    assert url in text


def test_discord_payload_suppresses_mentions():
    body = sinks.payload(_article(title="@everyone look"), _target("discord"))
    assert body["allowed_mentions"] == {"parse": []}


def test_webhook_payload_carries_every_article_field():
    body = sinks.payload(_article(), _target("webhook"))
    assert body == {
        "id": "1", "title": "Big <AI> News", "url": "https://ex.com/a",
        "source": "TechCrunch", "tag": "techcrunch", "tier": 1,
        "published": "2026-07-02T09:05:00+00:00", "summary": "A & B happened",
    }


def test_webhook_payload_published_null_when_absent():
    assert sinks.payload(_article(published=None), _target("webhook"))["published"] is None


def test_long_title_is_truncated_but_url_and_tag_survive():
    text = sinks.payload(_article(title="x" * 5000), _target("discord"))["content"]
    assert len(text) <= sinks.SPECS["discord"].limit
    assert "https://ex.com/a" in text
    assert text.endswith("#techcrunch")


def test_summary_is_dropped_before_the_title_is_cut():
    long_summary = "s" * 3000
    article = _article(title="Plain AI News", summary=long_summary)
    text = sinks.payload(article, _target("discord"))["content"]
    assert "Plain AI News" in text                    # title intact
    assert long_summary not in text                   # summary dropped
    assert len(text) <= sinks.SPECS["discord"].limit


def test_preview_returns_the_text_for_rendered_types():
    assert sinks.preview(_article(), _target("slack")) == \
        sinks.payload(_article(), _target("slack"))["text"]


def test_preview_returns_pretty_json_for_webhook():
    out = sinks.preview(_article(), _target("webhook"))
    assert json.loads(out)["id"] == "1"
