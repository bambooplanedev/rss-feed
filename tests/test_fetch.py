import httpx
import pytest

from aggregator.fetch import MAX_RETRY_AFTER, RATE_LIMIT_DELAY, RETRY_DELAY, fetch_feed


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_feed_returns_content_bytes():
    def handler(request):
        assert "User-Agent" in request.headers
        return httpx.Response(200, content=b"<rss></rss>")

    with _client(handler) as client:
        assert fetch_feed("https://ex.com/feed", client) == b"<rss></rss>"


def test_fetch_feed_raises_on_http_error():
    def handler(request):
        return httpx.Response(404)

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_feed("https://ex.com/missing", client)


def test_fetch_feed_retries_once_on_a_transient_status():
    """One publisher 503 used to make the whole run exit non-zero. At sixteen
    feeds twice a day that turns a red run into noise, and noise is what hides
    a genuinely dead target."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, content=b"<rss/>")

    with _client(handler) as client:
        assert fetch_feed("https://ex.com/feed", client, sleep=lambda _: None) == b"<rss/>"
    assert len(calls) == 2


def test_fetch_feed_retries_once_on_a_transport_error():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, content=b"<rss/>")

    with _client(handler) as client:
        assert fetch_feed("https://ex.com/feed", client, sleep=lambda _: None) == b"<rss/>"
    assert len(calls) == 2


def test_fetch_feed_does_not_retry_a_404():
    """The feed is gone; a second request cannot bring it back, and the red run
    is the point."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_feed("https://ex.com/missing", client, sleep=lambda _: None)
    assert len(calls) == 1


def test_fetch_feed_gives_up_after_the_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_feed("https://ex.com/feed", client, sleep=lambda _: None)
    assert len(calls) == 2


def _sleeps_before_retry(first_response):
    """Run one failed-then-ok fetch and return what fetch_feed slept for."""
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        return first_response if len(calls) == 1 else httpx.Response(200, content=b"<rss/>")

    with _client(handler) as client:
        assert fetch_feed("https://ex.com/feed", client, sleep=slept.append) == b"<rss/>"
    return slept


def test_fetch_feed_backs_off_longer_on_a_429_without_retry_after():
    """A rate limit two seconds later is still a rate limit: the old 2s retry
    hit WordPress.com's 429 twice and turned the run red four times."""
    assert _sleeps_before_retry(httpx.Response(429)) == [RATE_LIMIT_DELAY]


def test_fetch_feed_honours_retry_after_on_a_429():
    assert _sleeps_before_retry(httpx.Response(429, headers={"Retry-After": "7"})) == [7.0]


def test_fetch_feed_caps_retry_after():
    """One publisher must not be able to stall the whole run."""
    resp = httpx.Response(429, headers={"Retry-After": "3600"})
    assert _sleeps_before_retry(resp) == [MAX_RETRY_AFTER]


def test_fetch_feed_ignores_an_unparseable_retry_after():
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _sleeps_before_retry(resp) == [RATE_LIMIT_DELAY]


def test_fetch_feed_keeps_the_short_delay_for_a_5xx():
    assert _sleeps_before_retry(httpx.Response(503)) == [RETRY_DELAY]
