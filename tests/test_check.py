from datetime import datetime, timezone

import httpx

from aggregator.check import (
    FAIL, OK, WARN, Fetched, exit_code, fetch, judge, main, render, run,
)
from aggregator.models import FeedSource

SRC = FeedSource(name="Test", url="https://ex.com/feed", tag="test", tier=1)
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _rss(*items: str) -> bytes:
    return f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
{''.join(items)}</channel></rss>""".encode()


def _item(date: str | None, link: str = "https://ex.com/a") -> str:
    pub = f"<pubDate>{date}</pubDate>" if date else ""
    return f"<item><title>a</title><link>{link}</link>{pub}</item>"


FRESH = "Mon, 20 Oct 2026 09:00:00 +0000"
STALE = "Mon, 05 Jan 2026 09:00:00 +0000"


def test_a_live_dated_feed_is_ok():
    v = judge(SRC, Fetched(200, _rss(_item(FRESH))), NOW)
    assert (v.level, v.reasons, v.entries) == (OK, (), 1)


def test_any_status_but_200_fails_even_if_the_body_parses():
    """A 203 from a proxy or a 2xx interstitial is not the feed."""
    assert judge(SRC, Fetched(404, b""), NOW).level == FAIL
    v = judge(SRC, Fetched(203, _rss(_item(FRESH))), NOW)
    assert (v.level, v.reasons) == (FAIL, ("HTTP 203",))


def test_no_response_fails_with_the_error_named():
    v = judge(SRC, Fetched(None, b"", "ConnectTimeout"), NOW)
    assert (v.level, v.reasons) == (FAIL, ("no response: ConnectTimeout",))


def test_an_empty_feed_fails():
    assert judge(SRC, Fetched(200, _rss()), NOW).reasons == ("no entries",)
    assert judge(SRC, Fetched(200, b"<html>login</html>"), NOW).level == FAIL


def test_a_feed_with_no_parseable_date_fails_like_it_does_in_a_run():
    v = judge(SRC, Fetched(200, _rss(_item(None), _item("someday"))), NOW)
    assert v.level == FAIL
    assert v.reasons == ("none of 2 entries has a parseable date",)


def test_partly_undated_and_silent_feeds_warn_but_do_not_fail():
    v = judge(SRC, Fetched(200, _rss(_item(STALE), _item(None, "https://ex.com/b"))), NOW)
    assert v.level == WARN
    assert v.reasons == ("1 of 2 entries undated (dropped at parse)", "silent since 2026-01-05")


def test_silence_threshold_is_a_parameter():
    got = Fetched(200, _rss(_item(STALE)))
    assert judge(SRC, got, NOW, silence_days=400).level == OK


def test_same_responses_give_the_same_output_in_file_order():
    feeds = [FeedSource("B", "https://b", "b", 1), FeedSource("A", "https://a", "a", 1)]
    bodies = {"https://a": Fetched(200, _rss(_item(FRESH))), "https://b": Fetched(500, b"")}
    first = [render(v) for v in run(feeds, bodies.__getitem__, NOW)]
    second = [render(v) for v in run(feeds, bodies.__getitem__, NOW)]
    assert first == second
    assert [line.split()[1] for line in first] == ["b", "a"]


def test_exit_code_fails_on_fail_and_on_warn_only_when_strict():
    vs = run([SRC], lambda _: Fetched(200, _rss(_item(STALE))), NOW)
    assert exit_code(vs) == 0
    assert exit_code(vs, strict=True) == 1
    assert exit_code(run([SRC], lambda _: Fetched(404, b""), NOW)) == 1


def _client(*statuses):
    calls = iter(statuses)

    def handler(request):
        status = next(calls)
        if status is None:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(status, content=_rss(_item(FRESH)))
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_retries_a_transient_status_once_and_keeps_the_second_answer():
    slept = []
    got = fetch("https://ex.com/feed", _client(503, 200), sleep=slept.append)
    assert got.status == 200 and slept == [2.0]
    assert fetch("https://ex.com/feed", _client(503, 503), sleep=lambda _: None).status == 503


def test_fetch_does_not_retry_a_404_and_reports_a_dead_connection():
    slept = []
    assert fetch("https://ex.com/feed", _client(404), sleep=slept.append).status == 404
    assert slept == []
    got = fetch("https://ex.com/feed", _client(None, None), sleep=lambda _: None)
    assert (got.status, got.error) == (None, "ConnectError")


def test_main_reads_the_feeds_file_and_prints_a_summary(tmp_path, capsys):
    path = tmp_path / "feeds.yaml"
    path.write_text("feeds:\n  - {name: T, url: 'https://ex.com/feed', tag: test, tier: 1}\n")
    code = main([str(path)], get=lambda _: Fetched(200, _rss(_item(FRESH))), now=NOW)
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[-1] == "1 feeds: 1 ok, 0 warn, 0 fail"
