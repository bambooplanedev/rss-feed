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
