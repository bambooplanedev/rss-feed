# AI News RSS Aggregator → Telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggregate AI-news RSS/Atom feeds and post each new article as a structured message to a Telegram channel, on a scheduled GitHub Actions cron, with dedup state committed back to the repo.

**Architecture:** A linear pipeline — fetch feed bytes (httpx) → parse & normalize (feedparser) → dedup against `state.json` → format one Telegram message per article → send (throttled). Orchestrated by `aggregator.main`, run twice daily by a GitHub Actions workflow that commits the updated state file back to the repo. The Telegram channel is the sole output and a machine-readable log a future agent will consume; that agent and its "Bank of Themes" are out of scope.

**Tech Stack:** Python 3.13, feedparser, httpx, PyYAML, stdlib (`zoneinfo`, `html`, `html.parser`, `json`, `argparse`, `logging`); GitHub Actions.

## Global Constraints

- **Python version:** 3.13
- **Pinned runtime deps** (exact, in `requirements.txt`): `feedparser==6.0.12`, `httpx==0.28.1`, `PyYAML==6.0.2`
- **Test dep:** `pytest` (in `requirements-dev.txt`)
- **Package name:** `aggregator` (importable from repo root)
- **Timezone:** UTC everywhere — cron is UTC-native; timestamps formatted in UTC
- **Message format:** Telegram **HTML** parse mode, **one message per article**
- **Telegram single-channel limit ~20 msg/min:** sleep **3.5s between sends**; on HTTP `429` honor `parameters.retry_after` from the response body
- **Secrets via env:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **State:** `state.json` at repo root, committed back each run; first run seeds all-seen and posts nothing
- **No dependencies beyond the pinned set** — use stdlib for HTML stripping (`html.parser`), timezones (`zoneinfo`), HTTP mocking in tests (`httpx.MockTransport`)
- **TDD:** write the failing test first; commit after each green task

## File Structure

```
rss-feed/
├── pyproject.toml                 # pytest config (pythonpath, testpaths)
├── requirements.txt               # pinned runtime deps
├── requirements-dev.txt           # -r requirements.txt + pytest
├── feeds.yaml                     # source list (name, url, tag)
├── state.json                     # dedup state (created on first run, committed back)
├── .github/workflows/aggregate.yml
├── aggregator/
│   ├── __init__.py
│   ├── models.py                  # FeedSource, Article dataclasses
│   ├── config.py                  # load_feeds(path) -> list[FeedSource]
│   ├── fetch.py                   # fetch_feed(url, client) -> bytes
│   ├── parse.py                   # parse_feed, normalize_url, clean_summary
│   ├── state.py                   # load_state, save_state, select_new
│   ├── format.py                  # format_message(article, tz) -> str
│   ├── telegram.py                # send_message(...)
│   └── main.py                    # run(...) pipeline + CLI
└── tests/
    ├── fixtures/
    │   ├── rss_sample.xml
    │   ├── atom_sample.xml
    │   └── malformed.xml
    ├── test_config.py
    ├── test_parse.py
    ├── test_state.py
    ├── test_format.py
    ├── test_telegram.py
    └── test_main.py
```

---

### Task 1: Project scaffolding, models, and config loader

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `feeds.yaml`
- Create: `aggregator/__init__.py`, `aggregator/models.py`, `aggregator/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `FeedSource(name: str, url: str, tag: str)` — frozen dataclass
  - `Article(id: str, title: str, url: str, source: str, tag: str, published: datetime | None, summary: str)` — frozen dataclass
  - `load_feeds(path: str) -> list[FeedSource]`

- [ ] **Step 1: Create dependency and config files**

`requirements.txt`:
```
feedparser==6.0.12
httpx==0.28.1
PyYAML==6.0.2
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest==8.3.4
```

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

`aggregator/__init__.py`: (empty file)

- [ ] **Step 2: Create the source list `feeds.yaml`**

```yaml
feeds:
  - name: MIT Technology Review AI
    url: https://www.technologyreview.com/topic/artificial-intelligence/feed
    tag: mittr
  - name: Ars Technica AI
    url: https://arstechnica.com/ai/feed/
    tag: arstechnica
  - name: The Verge AI
    url: https://www.theverge.com/rss/ai-artificial-intelligence/index.xml
    tag: theverge
  - name: TechCrunch AI
    url: https://techcrunch.com/category/artificial-intelligence/feed/
    tag: techcrunch
  - name: Wired AI
    url: https://www.wired.com/feed/tag/ai/latest/rss
    tag: wired
  - name: AI News
    url: https://www.artificialintelligence-news.com/feed/
    tag: ainews
  - name: Towards Data Science
    url: https://towardsdatascience.com/feed
    tag: tds
  - name: OpenAI
    url: https://openai.com/news/rss.xml
    tag: openai
  - name: Google DeepMind
    url: https://deepmind.google/blog/rss.xml
    tag: deepmind
  - name: Google AI
    url: https://blog.google/technology/ai/rss/
    tag: googleai
  - name: Import AI
    url: https://jack-clark.net/feed/
    tag: importai
  - name: Simon Willison
    url: https://simonwillison.net/atom/everything/
    tag: simonw
  - name: The Gradient
    url: https://thegradient.pub/rss/
    tag: gradient
  - name: Berkeley BAIR
    url: https://bair.berkeley.edu/blog/feed.xml
    tag: bair
```

- [ ] **Step 3: Write the failing test**

`tests/test_config.py`:
```python
from aggregator.models import FeedSource
from aggregator.config import load_feeds


def test_load_feeds_parses_yaml(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(
        "feeds:\n"
        "  - name: Example\n"
        "    url: https://ex.com/feed\n"
        "    tag: ex\n"
    )
    feeds = load_feeds(str(p))
    assert feeds == [FeedSource(name="Example", url="https://ex.com/feed", tag="ex")]


def test_load_real_feeds_file_has_expected_shape():
    feeds = load_feeds("feeds.yaml")
    assert len(feeds) >= 14
    assert all(f.name and f.url and f.tag for f in feeds)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.models'`

- [ ] **Step 5: Write `aggregator/models.py`**

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tag: str


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    url: str
    source: str
    tag: str
    published: datetime | None
    summary: str
```

- [ ] **Step 6: Write `aggregator/config.py`**

```python
import yaml

from .models import FeedSource


def load_feeds(path: str) -> list[FeedSource]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [
        FeedSource(name=s["name"], url=s["url"], tag=s["tag"])
        for s in data["feeds"]
    ]
```

- [ ] **Step 7: Install deps and run tests**

Run: `pip install -r requirements-dev.txt && pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml requirements.txt requirements-dev.txt feeds.yaml aggregator/ tests/test_config.py
git commit -m "feat: scaffold project, models, and feeds config loader"
```

---

### Task 2: Feed parsing and normalization

**Files:**
- Create: `aggregator/parse.py`
- Create: `tests/fixtures/rss_sample.xml`, `tests/fixtures/atom_sample.xml`, `tests/fixtures/malformed.xml`
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: `FeedSource`, `Article` (Task 1)
- Produces:
  - `parse_feed(content: bytes, source: FeedSource) -> list[Article]`
  - `normalize_url(url: str) -> str`
  - `clean_summary(raw: str, limit: int = 300) -> str`

- [ ] **Step 1: Create test fixtures**

`tests/fixtures/rss_sample.xml`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test</title>
<item>
  <title>First Post</title>
  <link>https://ex.com/first</link>
  <guid>https://ex.com/first</guid>
  <pubDate>Wed, 02 Jul 2026 09:00:00 GMT</pubDate>
  <description>This is summary text for first.</description>
</item>
<item>
  <title>Second Post</title>
  <link>https://ex.com/second</link>
  <guid>https://ex.com/second</guid>
  <pubDate>Wed, 02 Jul 2026 10:00:00 GMT</pubDate>
  <description>Second summary.</description>
</item>
</channel></rss>
```

`tests/fixtures/atom_sample.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom Test</title>
<entry>
  <title>Atom Post</title>
  <link href="https://ex.com/atom1"/>
  <id>tag:ex.com,2026:atom1</id>
  <updated>2026-07-02T11:00:00Z</updated>
  <summary>Atom summary.</summary>
</entry>
</feed>
```

`tests/fixtures/malformed.xml` (missing closing `</rss>` — feedparser sets `bozo` but still yields the entry):
```xml
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Bad</title>
<item><title>Only</title><link>https://ex.com/only</link><description>x</description></item>
</channel>
```

- [ ] **Step 2: Write the failing test**

`tests/test_parse.py`:
```python
from datetime import timezone
from pathlib import Path

from aggregator.models import FeedSource
from aggregator.parse import parse_feed, normalize_url, clean_summary

SOURCE = FeedSource(name="Test Source", url="https://ex.com/feed", tag="test")
FIX = Path("tests/fixtures")


def test_parse_rss_returns_articles():
    articles = parse_feed((FIX / "rss_sample.xml").read_bytes(), SOURCE)
    assert len(articles) == 2
    a = articles[0]
    assert a.title == "First Post"
    assert a.url == "https://ex.com/first"
    assert a.source == "Test Source"
    assert a.tag == "test"
    assert a.published.tzinfo == timezone.utc
    assert a.published.hour == 9
    assert "summary text" in a.summary


def test_parse_atom_uses_link_href_and_id():
    articles = parse_feed((FIX / "atom_sample.xml").read_bytes(), SOURCE)
    assert len(articles) == 1
    assert articles[0].url == "https://ex.com/atom1"
    assert articles[0].id == "tag:ex.com,2026:atom1"


def test_parse_malformed_feed_still_returns_entries():
    articles = parse_feed((FIX / "malformed.xml").read_bytes(), SOURCE)
    assert len(articles) >= 1
    assert articles[0].title == "Only"


def test_clean_summary_strips_html_and_truncates():
    raw = "<p>Hello <b>world</b> and more text here that keeps going on " + "y " * 200 + "</p>"
    out = clean_summary(raw)
    assert "<" not in out
    assert out.startswith("Hello world")
    assert len(out) <= 301
    assert out.endswith("…")


def test_clean_summary_short_text_unchanged():
    assert clean_summary("<p>Short &amp; sweet.</p>") == "Short & sweet."


def test_normalize_url_strips_tracking_trailing_slash_and_fragment():
    assert (
        normalize_url("https://Ex.com/Path/?utm_source=x&id=5#frag")
        == "https://ex.com/Path?id=5"
    )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.parse'`

- [ ] **Step 4: Write `aggregator/parse.py`**

```python
import calendar
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser

from .models import Article, FeedSource

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 300
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw or "")
    return " ".join(parser.text().split())


def clean_summary(raw: str, limit: int = SUMMARY_LIMIT) -> str:
    text = strip_html(raw)
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0]
    return truncated + "…"


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def _published(entry) -> datetime | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    # feedparser returns a UTC struct_time; timegm treats it as UTC.
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def parse_feed(content: bytes, source: FeedSource) -> list[Article]:
    parsed = feedparser.parse(content)
    if parsed.bozo:
        log.warning("bozo feed %s: %s", source.url, parsed.get("bozo_exception"))
    articles: list[Article] = []
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not url:
            continue
        articles.append(
            Article(
                id=entry.get("id") or normalize_url(url),
                title=(entry.get("title") or "(untitled)").strip(),
                url=url,
                source=source.name,
                tag=source.tag,
                published=_published(entry),
                summary=clean_summary(entry.get("summary", "")),
            )
        )
    return articles
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_parse.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add aggregator/parse.py tests/test_parse.py tests/fixtures/
git commit -m "feat: feed parsing, url normalization, and summary cleaning"
```

---

### Task 3: Feed fetching

**Files:**
- Create: `aggregator/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: nothing (takes an `httpx.Client`)
- Produces: `fetch_feed(url: str, client: httpx.Client, timeout: float = 20.0) -> bytes`

- [ ] **Step 1: Write the failing test**

`tests/test_fetch.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.fetch'`

- [ ] **Step 3: Write `aggregator/fetch.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fetch.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregator/fetch.py tests/test_fetch.py
git commit -m "feat: httpx feed fetcher with timeout and browser user-agent"
```

---

### Task 4: State and deduplication

**Files:**
- Create: `aggregator/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `Article` (Task 1)
- Produces:
  - `load_state(path: str) -> list[str] | None` — returns `None` if the file does not exist (first run), else the list of seen ids
  - `save_state(path: str, seen_ids: list[str]) -> None` — writes `{"seen": [...]}`, capped to the most recent `MAX_IDS`
  - `select_new(articles: list[Article], seen_ids: list[str]) -> list[Article]`
  - `MAX_IDS = 2000`

- [ ] **Step 1: Write the failing test**

`tests/test_state.py`:
```python
from datetime import datetime, timezone

from aggregator.models import Article
from aggregator.state import load_state, save_state, select_new, MAX_IDS


def _article(id_):
    return Article(
        id=id_, title="t", url="https://ex.com/" + id_, source="s",
        tag="x", published=datetime.now(timezone.utc), summary="",
    )


def test_load_state_missing_file_returns_none(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) is None


def test_save_then_load_round_trips(tmp_path):
    p = str(tmp_path / "state.json")
    save_state(p, ["a", "b", "c"])
    assert load_state(p) == ["a", "b", "c"]


def test_save_state_caps_to_most_recent(tmp_path):
    p = str(tmp_path / "state.json")
    ids = [str(i) for i in range(MAX_IDS + 50)]
    save_state(p, ids)
    loaded = load_state(p)
    assert len(loaded) == MAX_IDS
    assert loaded[-1] == str(MAX_IDS + 49)  # newest retained
    assert loaded[0] == "50"                # oldest dropped


def test_select_new_filters_seen():
    articles = [_article("1"), _article("2"), _article("3")]
    new = select_new(articles, ["2"])
    assert [a.id for a in new] == ["1", "3"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.state'`

- [ ] **Step 3: Write `aggregator/state.py`**

```python
import json
from pathlib import Path

from .models import Article

MAX_IDS = 2000


def load_state(path: str) -> list[str] | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("seen", [])


def save_state(path: str, seen_ids: list[str]) -> None:
    capped = seen_ids[-MAX_IDS:]
    Path(path).write_text(
        json.dumps({"seen": capped}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def select_new(articles: list[Article], seen_ids: list[str]) -> list[Article]:
    seen = set(seen_ids)
    return [a for a in articles if a.id not in seen]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_state.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregator/state.py tests/test_state.py
git commit -m "feat: state persistence, dedup selection, and id capping"
```

---

### Task 5: Message formatting

**Files:**
- Create: `aggregator/format.py`
- Test: `tests/test_format.py`

**Interfaces:**
- Consumes: `Article` (Task 1)
- Produces: `format_message(article: Article, tz: str = "UTC") -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_format.py`:
```python
from datetime import datetime, timezone

from aggregator.models import Article
from aggregator.format import format_message


def _article(**kw):
    base = dict(
        id="1", title="Big <AI> News", url="https://ex.com/a",
        source="TechCrunch", tag="techcrunch",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_format.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.format'`

- [ ] **Step 3: Write `aggregator/format.py`**

```python
import html
from zoneinfo import ZoneInfo

from .models import Article


def format_message(article: Article, tz: str = "UTC") -> str:
    title = html.escape(article.title)
    source = html.escape(article.source)
    if article.published:
        when = article.published.astimezone(ZoneInfo(tz)).strftime("%H:%M, %d %b")
    else:
        when = "—"

    parts = [f"🔹 <b>{title}</b>", f"{source} · {when}"]
    if article.summary:
        parts += ["", html.escape(article.summary)]
    parts += ["", article.url, f"#{article.tag}"]
    return "\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_format.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregator/format.py tests/test_format.py
git commit -m "feat: Telegram HTML message formatter"
```

---

### Task 6: Telegram sender

**Files:**
- Create: `aggregator/telegram.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: nothing (takes an `httpx.Client`)
- Produces: `send_message(text: str, token: str, chat_id: str, client: httpx.Client, *, max_retries: int = 3, sleep=time.sleep) -> None` — raises `RuntimeError` if all retries are exhausted, `httpx.HTTPStatusError` on non-429 HTTP errors

- [ ] **Step 1: Write the failing test**

`tests/test_telegram.py`:
```python
import httpx
import pytest

from aggregator.telegram import send_message


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_message_posts_expected_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        send_message("hello", "TOKEN", "123", client)

    assert "/botTOKEN/sendMessage" in seen["url"]
    assert seen["body"]["chat_id"] == "123"
    assert seen["body"]["text"] == "hello"
    assert seen["body"]["parse_mode"] == "HTML"


def test_send_message_retries_on_429_then_succeeds():
    calls = {"n": 0}
    slept = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        send_message("hi", "T", "1", client, sleep=slept.append)

    assert calls["n"] == 2
    assert slept == [7]


def test_send_message_raises_on_400():
    def handler(request):
        return httpx.Response(400, json={"ok": False})

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            send_message("x", "T", "1", client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.telegram'`

- [ ] **Step 3: Write `aggregator/telegram.py`**

```python
import time

import httpx

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(
    text: str,
    token: str,
    chat_id: str,
    client: httpx.Client,
    *,
    max_retries: int = 3,
    sleep=time.sleep,
) -> None:
    url = _API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for _ in range(max_retries):
        resp = client.post(url, json=payload, timeout=20.0)
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 1)
            sleep(retry_after)
            continue
        resp.raise_for_status()
        return
    raise RuntimeError(f"Telegram send failed after {max_retries} attempts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_telegram.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregator/telegram.py tests/test_telegram.py
git commit -m "feat: Telegram sender with 429 retry_after handling"
```

---

### Task 7: Pipeline orchestration and CLI

**Files:**
- Create: `aggregator/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_feeds` (T1), `fetch_feed` (T3), `parse_feed` (T2), `load_state`/`save_state`/`select_new` (T4), `format_message` (T5), `send_message` (T6)
- Produces:
  - `collect_articles(feeds: list[FeedSource], client: httpx.Client) -> list[Article]` — per-feed try/except isolation
  - `run(*, feeds_path: str, state_path: str, token: str, chat_id: str, tz: str = "UTC", dry_run: bool = False, send_delay: float = 3.5) -> int` — returns number of articles posted
  - `main() -> None` — CLI entry (`--dry-run`, `--feeds`, `--state`, `--tz`)

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:
```python
from datetime import datetime, timezone

import aggregator.main as m
from aggregator.models import Article, FeedSource


def _article(id_, hour):
    return Article(
        id=id_, title=f"T{id_}", url=f"https://ex.com/{id_}", source="S",
        tag="s", published=datetime(2026, 7, 2, hour, tzinfo=timezone.utc), summary="",
    )


def test_first_run_seeds_and_posts_nothing(tmp_path, monkeypatch):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n")

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("a", 9), _article("b", 10)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda *a, **k: sent.append(a))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", send_delay=0)

    assert posted == 0
    assert sent == []
    # state now seeded with both ids
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a", "b"}


def test_second_run_posts_only_new_in_time_order(tmp_path, monkeypatch):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n")

    from aggregator.state import save_state
    save_state(state, ["a"])  # 'a' already seen

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("c", 10), _article("b", 9), _article("a", 8)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda text, token, chat_id, client, **k: sent.append(text))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", send_delay=0)

    assert posted == 2
    # oldest-first: b (09:00) before c (10:00)
    assert "T" + "b" in sent[0]
    assert "T" + "c" in sent[1]
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a", "b", "c"}


def test_dry_run_does_not_send_or_persist(tmp_path, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n")
    from aggregator.state import save_state
    save_state(state, ["a"])

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("b", 9)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda *a, **k: sent.append(a))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", dry_run=True, send_delay=0)

    assert posted == 1
    assert sent == []                       # nothing actually sent
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a"}  # state unchanged
    assert "T" + "b" in capsys.readouterr().out


def test_collect_articles_isolates_failing_feed(monkeypatch):
    good = FeedSource(name="Good", url="https://good/feed", tag="g")
    bad = FeedSource(name="Bad", url="https://bad/feed", tag="b")

    def fake_fetch(url, client):
        if "bad" in url:
            raise RuntimeError("boom")
        return b"<rss></rss>"

    monkeypatch.setattr(m, "fetch_feed", fake_fetch)
    monkeypatch.setattr(m, "parse_feed", lambda content, source: [_article("x", 9)])

    import httpx
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        out = m.collect_articles([good, bad], client)
    assert len(out) == 1  # bad feed skipped, good feed kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.main'`

- [ ] **Step 3: Write `aggregator/main.py`**

```python
import argparse
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from .config import load_feeds
from .fetch import fetch_feed
from .format import format_message
from .models import Article, FeedSource
from .parse import parse_feed
from .state import load_state, save_state, select_new
from .telegram import send_message

log = logging.getLogger("aggregator")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def collect_articles(feeds: list[FeedSource], client: httpx.Client) -> list[Article]:
    articles: list[Article] = []
    for feed in feeds:
        try:
            content = fetch_feed(feed.url, client)
            articles.extend(parse_feed(content, feed))
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            log.warning("feed failed: %s (%s)", feed.url, exc)
    return articles


def run(
    *,
    feeds_path: str,
    state_path: str,
    token: str,
    chat_id: str,
    tz: str = "UTC",
    dry_run: bool = False,
    send_delay: float = 3.5,
) -> int:
    feeds = load_feeds(feeds_path)
    with httpx.Client() as client:
        articles = collect_articles(feeds, client)

    seen = load_state(state_path)
    if seen is None:
        # First run: seed everything as seen, post nothing.
        if not dry_run:
            save_state(state_path, [a.id for a in articles])
        log.info("first run: seeded %d ids, posted nothing", len(articles))
        return 0

    new = select_new(articles, seen)
    new.sort(key=lambda a: a.published or _EPOCH)

    posted_ids = list(seen)
    with httpx.Client() as client:
        for i, article in enumerate(new):
            text = format_message(article, tz)
            if dry_run:
                print(text)
                print("---")
            else:
                send_message(text, token, chat_id, client)
                if i < len(new) - 1:
                    time.sleep(send_delay)
            posted_ids.append(article.id)

    if not dry_run:
        save_state(state_path, posted_ids)
    log.info("posted %d new articles", len(new))
    return len(new)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AI news RSS → Telegram aggregator")
    parser.add_argument("--dry-run", action="store_true", help="print messages, do not send or persist")
    parser.add_argument("--feeds", default="feeds.yaml")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--tz", default="UTC")
    args = parser.parse_args()

    run(
        feeds_path=args.feeds,
        state_path=args.state,
        token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        tz=args.tz,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: PASS (all tests from Tasks 1–7 green)

- [ ] **Step 6: Commit**

```bash
git add aggregator/main.py tests/test_main.py
git commit -m "feat: pipeline orchestration, first-run seeding, dry-run, CLI"
```

---

### Task 8: GitHub Actions workflow and setup docs

**Files:**
- Create: `.github/workflows/aggregate.yml`
- Create: `README.md` (setup instructions) — overwrite the placeholder README
- Modify: `.gitignore` (create if absent)

**Interfaces:**
- Consumes: `python -m aggregator.main` (Task 7), secrets `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- Produces: scheduled + manually-dispatchable workflow that runs the aggregator and commits `state.json`

- [ ] **Step 1: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
.venv/
```

- [ ] **Step 2: Create the workflow `.github/workflows/aggregate.yml`**

```yaml
name: AI News Aggregator

on:
  schedule:
    - cron: "17 7,18 * * *"   # 07:17 and 18:17 UTC (off-peak minute; times are approximate)
  workflow_dispatch: {}         # allow manual runs for testing

permissions:
  contents: write               # required to commit state.json back

concurrency:
  group: aggregator             # serialize runs so they never race on state.json
  cancel-in-progress: false

jobs:
  aggregate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run aggregator
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python -m aggregator.main --tz UTC

      - name: Commit updated state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add state.json
          if git diff --staged --quiet; then
            echo "No state changes to commit."
          else
            git commit -m "chore: update aggregator state"
            git pull --rebase --autostash
            git push
          fi
```

- [ ] **Step 3: Overwrite `README.md` with setup instructions**

````markdown
# AI News RSS Aggregator → Telegram

Fetches AI-news RSS/Atom feeds and posts each new article as a structured
message to a Telegram channel, twice a day, via GitHub Actions. Dedup state
lives in `state.json`, committed back to the repo each run.

## Sources

Edit `feeds.yaml` — one `{ name, url, tag }` entry per feed. No code change needed.

## One-time setup

1. **Create a Telegram bot:** message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow prompts, copy the **bot token**.
2. **Create a channel** (or use an existing one) and **add the bot as an admin**
   with permission to post messages.
3. **Get the channel chat id:** post any message in the channel, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `channel_post.chat.id` (a negative number like `-1001234567890`), or use the
   channel's public `@username`.
4. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. **Keep the repo private** (recommended) — scheduled workflows in *public*
   repos are auto-disabled after 60 days of inactivity.

## Schedule

Runs at **07:17 and 18:17 UTC** (`.github/workflows/aggregate.yml`). Scheduled
runs can be delayed at high load, so treat times as approximate. Use the
**Run workflow** button (workflow_dispatch) to trigger manually.

> **First run** seeds every current feed item as "already seen" and posts
> nothing — this avoids dumping the backlog. Only items published after the
> first run are posted thereafter.

## Local development

```bash
pip install -r requirements-dev.txt
pytest -v                          # run the test suite
python -m aggregator.main --dry-run   # print messages without sending or persisting
```
````

- [ ] **Step 4: Validate the workflow YAML parses**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/aggregate.yml')); print('yaml ok')"`
Expected: `yaml ok`

- [ ] **Step 5: Confirm the full suite still passes**

Run: `pytest -v`
Expected: PASS (all tests green)

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/aggregate.yml README.md .gitignore
git commit -m "feat: GitHub Actions schedule, state commit-back, and setup docs"
```

---

## Post-implementation manual verification

These require real Telegram credentials and cannot be unit-tested:

1. Set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` locally and run
   `python -m aggregator.main` once — confirm it seeds `state.json` and posts nothing.
2. Run it a second time after a feed has a new item — confirm exactly the new
   article(s) post to the channel, oldest first, correctly formatted.
3. Push to GitHub, add the secrets, and trigger the workflow via **Run workflow** —
   confirm the run succeeds and commits an updated `state.json`.
```
