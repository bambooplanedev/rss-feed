import time

import httpx

USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsAggregator/1.0; "
    "+https://github.com/) feedparser/httpx"
)

RETRY_DELAY = 2.0

# Retried once; everything else is not. A 404 feed is gone and a second request
# cannot bring it back — collect_articles marks it failed and the run goes red,
# which is the point. These four are the ones that heal on their own.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _get(url: str, client: httpx.Client, timeout: float) -> bytes:
    resp = client.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def fetch_feed(
    url: str, client: httpx.Client, timeout: float = 20.0, *, sleep=time.sleep
) -> bytes:
    """Fetch once, and on a transient failure fetch once more.

    A feed failure makes the whole run exit non-zero. At sixteen feeds twice a
    day, one publisher's 503 or dropped connection turned that into a routine
    red run — and a routinely red run is what hides a genuinely dead target.
    One retry removes the common case without hiding a feed that is really gone.
    """
    try:
        return _get(url, client, timeout)
    except httpx.TransportError:
        pass
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in _RETRY_STATUSES:
            raise
    sleep(RETRY_DELAY)
    return _get(url, client, timeout)
