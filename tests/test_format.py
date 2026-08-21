from datetime import datetime, timezone

from aggregator.models import Article
from aggregator.format import format_message


def _article(**kw):
    base = dict(
        id="1", title="Big <AI> News", url="https://ex.com/a",
        source="TechCrunch", tag="techcrunch", tier=1,
        published=datetime(2026, 7, 2, 9, 5, tzinfo=timezone.utc),
        summary="A & B happened",
    )
    base.update(kw)
    return Article(**base)


def test_format_message_structure_and_escaping():
    msg = format_message(_article())
    lines = msg.split("\n")
    assert lines[0] == "🔹 <b>Big &lt;AI&gt; News</b>"
    assert lines[1] == "TechCrunch · 09:05, 02 Jul"
    assert "A &amp; B happened" in msg
    assert "https://ex.com/a" in msg
    assert lines[-1] == "#techcrunch"


def test_format_message_missing_published_uses_dash():
    msg = format_message(_article(published=None))
    assert "· —" in msg.split("\n")[1]


def test_format_message_omits_empty_summary_block():
    msg = format_message(_article(summary=""))
    # url line directly reachable; no double-blank summary body
    assert "https://ex.com/a" in msg
    assert "\n\n\n" not in msg


def test_format_message_escapes_url_ampersand():
    msg = format_message(_article(url="https://ex.com/a?x=1&y=2"))
    assert "https://ex.com/a?x=1&amp;y=2" in msg
    assert "x=1&y=2" not in msg
