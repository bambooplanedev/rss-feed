import httpx
import pytest

from aggregator.fetch import fetch_feed


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
