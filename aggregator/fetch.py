import httpx

USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsAggregator/1.0; "
    "+https://github.com/) feedparser/httpx"
)


def fetch_feed(url: str, client: httpx.Client, timeout: float = 20.0) -> bytes:
    resp = client.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content
