# Multi-Messenger Sinks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded Telegram output with a table-driven sink registry that delivers articles to Telegram, Discord, Slack, and generic webhooks, each with its own dedup state and routing filters.

**Architecture:** A single `aggregator/sinks.py` holds a `SPECS` table — one row per messenger type carrying its escaper, bold marker, payload key, text limit, send delay, and 429-backoff reader. `render`/`payload`/`preview`/`send` are module functions that read that table; no per-target objects and no plugin package. Targets are declared in a new `targets.yaml` with `${ENV}` indirection for secrets, and `state.json` becomes `{"targets": {name: [ids]}}` so each destination tracks what it has seen independently.

**Tech Stack:** Python 3.13, `httpx`, `feedparser`, `PyYAML`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-21-multi-messenger-sinks-design.md`

## Global Constraints

- Python 3.13 via the project venv. **Run tests as `.venv/bin/python -m pytest`** — the system `python3` is 3.9 and will fail on `X | Y` type syntax.
- No new runtime dependencies. `requirements.txt` stays at `feedparser`, `httpx`, `PyYAML`.
- Telegram output must not change by a single character. `tests/test_sinks.py` carries a regression test asserting the exact current `format.py` output.
- Error messages about configuration name the **environment variable**, never its value. Secrets must never reach a log line or an exception message.
- `MAX_IDS = 500` per target, `MAX_SENDS_PER_RUN = 20` per target per run.
- **`state.json` at the repo root is live production data** (2000 ids, ~115 KB, rewritten twice daily by the workflow). Never edit it, never delete it, and never point a test or a manual run at it — always use `tmp_path` or an explicit throwaway `--state` path.
- Send delays are fixed per type and are derived from real service limits: telegram `3.5`, discord `2.0`, slack `1.2`, webhook `0.0`. Do not "round them off".
- Every task ends with a commit. Tests must pass before each commit.

---

### Task 1: Article gains `tier`; `Target` model with routing filters

**Files:**
- Modify: `aggregator/models.py`
- Modify: `aggregator/parse.py:66-77` (the `Article(...)` construction in `parse_feed`)
- Modify: `tests/test_parse.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Article` gains `tier: int` (positional field, after `tag`).
  - `Target(name: str, type: str, tz: ZoneInfo, url: str = "", token: str = "", chat_id: str = "", tiers: tuple[int, ...] = (), tags: tuple[str, ...] = (), exclude_tags: tuple[str, ...] = ())`, frozen dataclass, with `matches(self, article: Article) -> bool`.

Adding a field to a frozen dataclass breaks every existing `Article(...)` call site in the tests. That is expected and is part of this task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parse.py`:

```python
def test_parse_feed_copies_tier_from_source():
    source = FeedSource(name="S", url="https://ex.com/feed", tag="s", tier=3)
    content = (TESTS_DIR / "fixtures" / "rss_sample.xml").read_bytes()

    articles = parse_feed(content, source)

    assert articles
    assert all(a.tier == 3 for a in articles)
```

If `tests/test_parse.py` does not already define `TESTS_DIR`, add at the top of the file:

```python
from pathlib import Path

TESTS_DIR = Path(__file__).parent
```

Append to `tests/test_config.py`:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from aggregator.models import Article, Target


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_parse.py::test_parse_feed_copies_tier_from_source tests/test_config.py::test_target_matches -v`
Expected: FAIL — `ImportError: cannot import name 'Target'` and `TypeError: Article.__init__() got an unexpected keyword argument 'tier'`.

- [ ] **Step 3: Add `tier` to `Article` and add `Target`**

In `aggregator/models.py`, add the `tier` field to `Article` and append the `Target` class:

```python
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tag: str
    tier: int


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    url: str
    source: str
    tag: str
    tier: int
    published: datetime | None
    summary: str


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    tz: ZoneInfo
    url: str = ""
    token: str = ""
    chat_id: str = ""
    tiers: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()

    def matches(self, article: Article) -> bool:
        if self.tiers and article.tier not in self.tiers:
            return False
        if self.tags and article.tag not in self.tags:
            return False
        return article.tag not in self.exclude_tags
```

- [ ] **Step 4: Populate `tier` in `parse_feed`**

In `aggregator/parse.py`, inside the `Article(...)` construction, add `tier=source.tier,` immediately after the `tag=source.tag,` line.

- [ ] **Step 5: Fix the existing test fixtures that construct `Article`**

`Article` is frozen and `tier` has no default, so every literal construction must pass it. Update these:

- `tests/test_format.py` — in `_article`, add `tier=1,` to the `base` dict.
- `tests/test_main.py` — in `_article`, add `tier=1,` to the `Article(...)` call.
- `tests/test_state.py` — add `tier=1` to any `Article(...)` construction it contains.

Run `.venv/bin/python -m pytest -v 2>&1 | grep -n "unexpected keyword\|missing 1 required"` to find any construction the list above missed.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git add aggregator/models.py aggregator/parse.py tests/
git commit -m "feat: add Article.tier and the Target model with routing filters"
```

---

### Task 2: `sinks.py` — the SPECS table and text rendering

**Files:**
- Create: `aggregator/sinks.py`
- Create: `tests/test_sinks.py`

**Interfaces:**
- Consumes: `Article` and `Target` from Task 1.
- Produces:
  - `SPECS: dict[str, Spec]` — keys `"telegram"`, `"discord"`, `"slack"`, `"webhook"`.
  - `Spec` NamedTuple with fields `esc_text`, `esc_url`, `bold`, `limit`, `key`, `delay`, `retry_after`.
  - `payload(article: Article, target: Target) -> dict`
  - `preview(article: Article, target: Target) -> str`

`format.py` and `telegram.py` stay in place and keep working for this task and the next — `main.py` still imports them. They are deleted in Task 6.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sinks.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sinks.py -v`
Expected: FAIL — `ImportError: cannot import name 'sinks'`.

- [ ] **Step 3: Write `aggregator/sinks.py`**

```python
import html
import json
import re
from dataclasses import asdict
from typing import Any, Callable, NamedTuple

import httpx

from .models import Article, Target

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Discord treats these as formatting; a backslash makes them literal.
_MD_SPECIALS = re.compile(r"([\\`*_~|>\[\]()])")


def _md_escape(text: str) -> str:
    return _MD_SPECIALS.sub(r"\\\1", text)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Spec(NamedTuple):
    esc_text: Callable[[str], str] | None
    esc_url: Callable[[str], str] | None
    bold: tuple[str, str]
    limit: int
    key: str
    delay: float
    retry_after: Callable[[httpx.Response], Any]


# Delays come from real per-destination limits: telegram ~20 msg/min per chat,
# discord 30 msg/min per channel, slack 1 msg/sec per webhook.
SPECS: dict[str, Spec] = {
    "telegram": Spec(html.escape, html.escape, ("<b>", "</b>"), 4096, "text", 3.5,
                     lambda r: r.json()["parameters"]["retry_after"]),
    "discord": Spec(_md_escape, None, ("**", "**"), 2000, "content", 2.0,
                    lambda r: r.json()["retry_after"]),
    "slack": Spec(_slack_escape, _slack_escape, ("*", "*"), 4000, "text", 1.2,
                  lambda r: r.headers["Retry-After"]),
    # webhook sends the article as JSON: esc_*, bold, limit and key are unused.
    "webhook": Spec(None, None, ("", ""), 0, "", 0.0,
                    lambda r: r.headers["Retry-After"]),
}


def _text(article: Article, target: Target) -> str:
    spec = SPECS[target.type]
    esc = spec.esc_text or (lambda s: s)
    esc_url = spec.esc_url or (lambda s: s)
    open_b, close_b = spec.bold
    when = (
        article.published.astimezone(target.tz).strftime("%H:%M, %d %b")
        if article.published
        else "—"
    )

    def build(title: str, summary: str) -> str:
        parts = [f"🔹 {open_b}{esc(title)}{close_b}", f"{esc(article.source)} · {when}"]
        if summary:
            parts += ["", esc(summary)]
        parts += ["", esc_url(article.url), f"#{esc(article.tag)}"]
        return "\n".join(parts)

    text = build(article.title, article.summary)
    if len(text) <= spec.limit:
        return text

    # parse.clean_summary already caps summaries at 300 chars, so this is a
    # guard against a pathological title, not a hot path. Drop the summary
    # first; only then shorten the title. Never cut the rendered string —
    # slicing it can sever an HTML entity or a closing tag and earn a 400.
    text = build(article.title, "")
    if len(text) <= spec.limit:
        return text

    room = max(0, spec.limit - len(build("", "")) - 1)
    title = article.title
    while title and len(esc(title)) > room:
        title = title[:-1]
    return build(title + "…", "")


def payload(article: Article, target: Target) -> dict:
    if target.type == "webhook":
        return {
            **asdict(article),
            "published": article.published.isoformat() if article.published else None,
        }
    text = _text(article, target)
    if target.type == "telegram":
        return {"chat_id": target.chat_id, "text": text, "parse_mode": "HTML"}
    if target.type == "discord":
        # Without this an article titled "@everyone" pings the whole server.
        return {"content": text, "allowed_mentions": {"parse": []}}
    return {"text": text}


def preview(article: Article, target: Target) -> str:
    body = payload(article, target)
    key = SPECS[target.type].key
    return body[key] if key else json.dumps(body, ensure_ascii=False, indent=2)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sinks.py -v`
Expected: PASS, 15 tests.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS. `test_format.py` still passes — `format.py` is untouched.

- [ ] **Step 6: Commit**

```bash
git add aggregator/sinks.py tests/test_sinks.py
git commit -m "feat: add sinks SPECS table and per-type rendering"
```

---

### Task 3: `sinks.py` — sending, 429 backoff, permanent-vs-transient errors

**Files:**
- Modify: `aggregator/sinks.py`
- Modify: `tests/test_sinks.py`

**Interfaces:**
- Consumes: `SPECS`, `payload` from Task 2.
- Produces:
  - `class PermanentSendError(Exception)`
  - `send(article: Article, target: Target, client: httpx.Client, *, sleep=time.sleep, max_retries: int = 3) -> None`
  - Module constants `MAX_RETRIES = 3`, `MAX_BACKOFF = 60.0`.

The error split is the load-bearing part of this task. A non-429 4xx is **permanent** — the caller must record the article as handled and move on, or one undeliverable article blocks that target forever. Everything else is **transient** — the caller stops and retries next run.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sinks.py`:

```python
import httpx


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_posts_to_the_telegram_bot_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        sinks.send(_article(), _target("telegram"), client)

    assert seen["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert seen["body"]["chat_id"] == "123"
    assert seen["body"]["parse_mode"] == "HTML"


@pytest.mark.parametrize("type_", ["discord", "slack", "webhook"])
def test_send_posts_to_the_configured_url(type_):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target(type_), client)

    assert seen["url"] == "https://ex.com/hook"


def test_retries_on_telegram_429_reading_the_body():
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        sinks.send(_article(), _target("telegram"), client, sleep=slept.append)

    assert calls["n"] == 2
    assert slept == [7.0]


def test_retries_on_discord_429_reading_the_body():
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 2.5})
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target("discord"), client, sleep=slept.append)

    assert slept == [2.5]


def test_retries_on_slack_429_reading_the_header():
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target("slack"), client, sleep=slept.append)

    assert slept == [3.0]


def test_html_429_body_falls_back_to_one_second():
    """Discord sits behind Cloudflare, which answers 429 with HTML, not JSON."""
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="<html>rate limited</html>")
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target("discord"), client, sleep=slept.append)

    assert slept == [1.0]


def test_absurd_retry_after_is_clamped():
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "86400"})
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target("slack"), client, sleep=slept.append)

    assert slept == [60.0]


def test_http_date_retry_after_falls_back_to_one_second():
    calls, slept = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return httpx.Response(200)

    with _client(handler) as client:
        sinks.send(_article(), _target("slack"), client, sleep=slept.append)

    assert slept == [1.0]


def test_exhausted_retries_raise_runtime_error():
    def handler(request):
        return httpx.Response(429, json={"retry_after": 1})

    with _client(handler) as client:
        with pytest.raises(RuntimeError):
            sinks.send(_article(), _target("discord"), client, sleep=lambda _: None)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_non_429_4xx_is_permanent(status):
    def handler(request):
        return httpx.Response(status, text="nope")

    with _client(handler) as client:
        with pytest.raises(sinks.PermanentSendError):
            sinks.send(_article(), _target("discord"), client)


def test_5xx_is_transient_not_permanent():
    def handler(request):
        return httpx.Response(503, text="down")

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            sinks.send(_article(), _target("slack"), client)


def test_permanent_error_message_does_not_leak_the_url():
    target = _target("discord", url="https://discord.com/api/webhooks/1/SUPERSECRET")

    def handler(request):
        return httpx.Response(404, text="not found")

    with _client(handler) as client:
        with pytest.raises(sinks.PermanentSendError) as exc:
            sinks.send(_article(), target, client)

    assert "SUPERSECRET" not in str(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sinks.py -v -k "send or 429 or permanent or transient or retry or clamp"`
Expected: FAIL — `AttributeError: module 'aggregator.sinks' has no attribute 'send'`.

- [ ] **Step 3: Add sending to `aggregator/sinks.py`**

Add `import time` to the imports, then append:

```python
MAX_RETRIES = 3
MAX_BACKOFF = 60.0


class PermanentSendError(Exception):
    """A non-retryable 4xx. The article will never reach this target."""


def _url(target: Target) -> str:
    return _TG_API.format(token=target.token) if target.type == "telegram" else target.url


def _backoff(spec: Spec, resp: httpx.Response) -> float:
    # The extractor itself can throw: Cloudflare answers 429 with HTML, and
    # Retry-After is legally allowed to be an HTTP-date. Either way, wait a
    # second rather than turning a retryable 429 into a hard failure.
    try:
        return min(float(spec.retry_after(resp)), MAX_BACKOFF)
    except Exception:
        return 1.0


def send(
    article: Article,
    target: Target,
    client: httpx.Client,
    *,
    sleep=time.sleep,
    max_retries: int = MAX_RETRIES,
) -> None:
    spec = SPECS[target.type]
    url = _url(target)
    body = payload(article, target)
    for _ in range(max_retries):
        resp = client.post(url, json=body, timeout=20.0)
        if resp.status_code == 429:
            sleep(_backoff(spec, resp))
            continue
        if 400 <= resp.status_code < 500:
            # Deleted webhook, revoked token, unparseable entities: retrying
            # forever would block every later article for this target.
            raise PermanentSendError(
                f"target {target.name!r}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        resp.raise_for_status()
        return
    raise RuntimeError(f"target {target.name!r}: rate limited after {max_retries} attempts")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sinks.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add aggregator/sinks.py tests/test_sinks.py
git commit -m "feat: sink sending with clamped 429 backoff and permanent-error split"
```

---

### Task 4: `load_targets` — config parsing, `${ENV}` resolution, validation

**Files:**
- Modify: `aggregator/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `Target` (Task 1), `SPECS` (Task 2) for the known-types check.
- Produces: `load_targets(path: str, default_tz: str = "UTC") -> tuple[list[Target], list[str]]` — the targets that built, and the names of targets skipped because an environment variable was unset.

Two failure categories, and they behave differently (spec §3.1). A **malformed config** is a human typo that lives in the repo: raise, send nothing. An **unresolved `${VAR}`** is operational — a rotated secret — so that one target is skipped and the others still deliver. Collapsing these would mean a deleted Slack secret silences Telegram.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_targets'`.

- [ ] **Step 3: Implement `load_targets`**

Rewrite `aggregator/config.py`, keeping `load_feeds` exactly as it is and adding below it:

```python
import logging
import os
import re
from zoneinfo import ZoneInfo

import yaml

from .models import FeedSource, Target
from .sinks import SPECS

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_VALUE_KEYS = ("url", "token", "chat_id", "tz")
_FILTER_KEYS = ("tiers", "tags", "exclude_tags")
_ALLOWED_KEYS = {"name", "type", *_VALUE_KEYS, *_FILTER_KEYS}
_REQUIRED_KEYS = {"telegram": ("token", "chat_id")}


class _MissingEnv(Exception):
    """An environment variable referenced by the config is not set."""


def _resolve(value, key: str):
    if not isinstance(value, str):
        return value
    match = _ENV_RE.match(value.strip())
    if not match:
        return value
    resolved = os.environ.get(match.group(1), "")
    if not resolved:
        # Name the variable, never its value — this line reaches the CI log.
        raise _MissingEnv(f"environment variable {match.group(1)} is not set (key {key!r})")
    return resolved


def load_targets(path: str, default_tz: str = "UTC") -> tuple[list[Target], list[str]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("targets") or []
    if not raw:
        raise ValueError(f"{path} defines no targets")

    targets: list[Target] = []
    skipped: list[str] = []
    seen_names: set[str] = set()

    for i, entry in enumerate(raw):
        name, type_ = entry.get("name"), entry.get("type")
        if not name or not type_:
            raise ValueError(f"target #{i} in {path} is missing 'name' or 'type'")
        if name in seen_names:
            raise ValueError(f"duplicate target name {name!r} in {path}")
        seen_names.add(name)
        if type_ not in SPECS:
            raise ValueError(
                f"target {name!r}: unknown type {type_!r}; known types: {', '.join(sorted(SPECS))}"
            )
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(f"target {name!r}: unknown key(s) {', '.join(sorted(unknown))}")

        try:
            values = {k: _resolve(entry[k], k) for k in _VALUE_KEYS if k in entry}
        except _MissingEnv as exc:
            log.error("target %s skipped: %s", name, exc)
            skipped.append(name)
            continue

        for required in _REQUIRED_KEYS.get(type_, ("url",)):
            if not values.get(required):
                raise ValueError(f"target {name!r} (type {type_}) requires {required!r}")

        tz_name = values.pop("tz", default_tz)
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise ValueError(f"target {name!r}: invalid tz {tz_name!r}") from exc

        targets.append(
            Target(
                name=name,
                type=type_,
                tz=tz,
                tiers=tuple(entry.get("tiers") or ()),
                tags=tuple(entry.get("tags") or ()),
                exclude_tags=tuple(entry.get("exclude_tags") or ()),
                **values,
            )
        )
    return targets, skipped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add aggregator/config.py tests/test_config.py
git commit -m "feat: load_targets with env resolution and two-tier config validation"
```

---

### Task 5: Per-target dedup state

**Files:**
- Modify: `aggregator/state.py`
- Modify: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `load_state(path: str) -> dict[str, list[str]]` — the inner mapping; `{}` when the file is absent.
  - `save_state(path: str, seen_by_target: dict[str, list[str]]) -> None` — writes `{"targets": {...}}`.
  - `select_new(articles, seen_ids)` — unchanged.
  - `MAX_IDS = 500`.

There is deliberately **no migration**, and this is a decision rather than an absence of data. The aggregator has been running in production since 2026-07-09; `state.json` is tracked and holds 2000 ids in the old flat `{"seen": [...]}` shape. We are not carrying them over: Telegram takes the same seed path as any new target, at the cost of one skipped 12-hour news window, and we avoid ~10 lines plus tests that would run exactly once in the project's life. `save_state` rewrites the whole document, so the `seen` key disappears after the first run.

**Do not add a `migrate_state` function.** If a reviewer flags the missing migration, the answer is this paragraph.

The cap is per target on purpose: a shared cap would let a busy target evict a quiet target's ids and cause silent reposts. 500 is chosen because `state.json` is committed back to the repo twice a day and dedup only has to outlive the RSS window (~700 ids in flight across 14 feeds).

- [ ] **Step 1: Write the failing tests**

Replace the state-shape tests in `tests/test_state.py` with:

```python
from aggregator.state import MAX_IDS, load_state, save_state, select_new


def test_load_state_returns_empty_dict_when_absent(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == {}


def test_roundtrip_per_target(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": ["a", "b"], "discord": ["a"]})
    assert load_state(path) == {"tg": ["a", "b"], "discord": ["a"]}


def test_state_file_is_wrapped_in_targets_key(tmp_path):
    import json
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": ["a"]})
    assert json.loads(open(path, encoding="utf-8").read()) == {"targets": {"tg": ["a"]}}


def test_cap_applies_per_target_not_across_targets(tmp_path):
    path = str(tmp_path / "state.json")
    busy = [f"b{i}" for i in range(MAX_IDS + 50)]
    quiet = ["q1", "q2"]

    save_state(path, {"busy": busy, "quiet": quiet})
    state = load_state(path)

    assert len(state["busy"]) == MAX_IDS
    assert state["busy"][0] == "b50"          # oldest dropped, newest kept
    assert state["quiet"] == quiet            # a busy target cannot evict a quiet one
```

Keep any existing `select_new` tests as they are — that function does not change.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_state.py -v`
Expected: FAIL — `load_state` returns a list, not a dict.

- [ ] **Step 3: Rewrite `aggregator/state.py`**

```python
import json
from pathlib import Path

from .models import Article

# Per target, not per file: a shared cap would let a busy target evict a quiet
# target's ids and cause silent reposts. state.json is committed back to the
# repo twice a day, so the number stays small — dedup only has to outlive the
# RSS window (~700 ids in flight across 14 feeds).
MAX_IDS = 500


def load_state(path: str) -> dict[str, list[str]]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("targets", {})


def save_state(path: str, seen_by_target: dict[str, list[str]]) -> None:
    capped = {name: ids[-MAX_IDS:] for name, ids in seen_by_target.items()}
    Path(path).write_text(
        json.dumps({"targets": capped}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def select_new(articles: list[Article], seen_ids: list[str]) -> list[Article]:
    seen = set(seen_ids)
    return [a for a in articles if a.id not in seen]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_state.py -v`
Expected: PASS.

- [ ] **Step 5: Confirm the expected breakage**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: FAIL. `main.py` still uses the old list-shaped state; Task 6 rewrites it. Do not patch `main.py` here.

- [ ] **Step 6: Commit**

```bash
git add aggregator/state.py tests/test_state.py
git commit -m "feat: per-target dedup state, MAX_IDS capped per target"
```

---

### Task 6: Rewrite `main.py` for multi-target delivery

**Files:**
- Modify: `aggregator/main.py`
- Delete: `aggregator/format.py`, `aggregator/telegram.py`
- Delete: `tests/test_format.py`, `tests/test_telegram.py`
- Rewrite: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_targets` (Task 4), `sinks.send`/`sinks.preview`/`sinks.PermanentSendError`/`SPECS` (Tasks 2-3), `load_state`/`save_state`/`select_new` (Task 5), `Target.matches` (Task 1).
- Produces: `run(*, feeds_path, targets_path, state_path, tz="UTC", dry_run=False, sleep=time.sleep) -> tuple[dict[str, int], list[str]]` — sent counts per target name, and the names of targets that had a failure.

`format.py` and `telegram.py` are deleted here because this is the commit where `main.py` stops importing them. Their coverage moved to `tests/test_sinks.py` in Tasks 2-3 — including the byte-identical Telegram regression test.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_main.py` entirely:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import aggregator.main as m
from aggregator.models import Article
from aggregator.sinks import PermanentSendError
from aggregator.state import load_state, save_state

FEEDS = "feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n"


def _article(id_, hour=9, tier=1, tag="s"):
    return Article(
        id=id_, title=f"T{id_}", url=f"https://ex.com/{id_}", source="S",
        tag=tag, tier=tier,
        published=datetime(2026, 7, 2, hour, tzinfo=timezone.utc), summary="",
    )


def _setup(tmp_path, targets_yaml, articles, monkeypatch):
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(targets_yaml, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: articles)
    return dict(
        feeds_path=str(tmp_path / "feeds.yaml"),
        targets_path=str(tmp_path / "targets.yaml"),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda _: None,
    )


TWO_TARGETS = (
    "targets:\n"
    "  - name: core\n    type: discord\n    url: https://ex.com/core\n    tiers: [1]\n"
    "  - name: all\n    type: slack\n    url: https://ex.com/all\n"
)


def test_new_target_is_seeded_and_sends_nothing(tmp_path, monkeypatch):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a"), _article("b")], monkeypatch)
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert sent == []
    assert counts == {}
    assert failed == []
    assert set(load_state(kw["state_path"])["core"]) == {"a", "b"}


def test_filters_route_different_articles_to_different_targets(tmp_path, monkeypatch):
    articles = [_article("t1", tier=1), _article("t3", tier=3)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    m.run(**kw)

    assert sorted(sent) == [("all", "t1"), ("all", "t3"), ("core", "t1")]


def test_articles_are_fetched_once_for_all_targets(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_collect(feeds, client):
        calls["n"] += 1
        return [_article("a")]

    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", counting_collect)
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)
    state_path = str(tmp_path / "state.json")
    save_state(state_path, {"core": [], "all": []})

    m.run(feeds_path=str(tmp_path / "feeds.yaml"),
          targets_path=str(tmp_path / "targets.yaml"),
          state_path=state_path, sleep=lambda _: None)

    assert calls["n"] == 1


def test_transient_failure_stops_one_target_and_spares_the_others(tmp_path, monkeypatch):
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []

    def flaky(article, target, client):
        if target.name == "core":
            raise RuntimeError("service down")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", flaky)

    counts, failed = m.run(**kw)

    state = load_state(kw["state_path"])
    assert state["core"] == []                     # nothing recorded: retry next run
    assert set(state["all"]) == {"a", "b"}         # the other target is unaffected
    assert counts["all"] == 2
    assert "core" in failed


def test_permanent_failure_does_not_block_the_queue(tmp_path, monkeypatch):
    """A deleted webhook must not poison every later article for that target."""
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("404")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", picky)

    counts, failed = m.run(**kw)

    assert ("core", "good") in sent                # the queue kept moving
    assert set(load_state(kw["state_path"])["core"]) == {"bad", "good"}
    assert counts["core"] == 1                     # 'bad' is recorded but not counted as sent
    assert "core" in failed


def test_send_cap_leaves_the_remainder_for_the_next_run(tmp_path, monkeypatch):
    articles = [_article(f"a{i}", hour=9) for i in range(m.MAX_SENDS_PER_RUN + 5)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, _ = m.run(**kw)

    assert counts["core"] == m.MAX_SENDS_PER_RUN
    assert len(load_state(kw["state_path"])["core"]) == m.MAX_SENDS_PER_RUN


def test_unset_env_in_one_target_does_not_reduce_the_others(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    targets = (
        "targets:\n"
        "  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n"
        "  - name: alive\n    type: slack\n    url: https://ex.com/alive\n"
    )
    kw = _setup(tmp_path, targets, [_article("a"), _article("b")], monkeypatch)
    save_state(kw["state_path"], {"alive": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert counts["alive"] == 2                    # the working target delivered in full
    assert failed == ["dead"]


def test_state_is_saved_after_each_target(tmp_path, monkeypatch):
    """Process death mid-run must not cost the targets that already finished."""
    articles = [_article("a")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})

    def send_then_die(article, target, client):
        if target.name == "all":
            raise KeyboardInterrupt("runner evicted")

    monkeypatch.setattr(m.sinks, "send", send_then_die)

    with pytest.raises(KeyboardInterrupt):
        m.run(**kw)

    assert load_state(kw["state_path"])["core"] == ["a"]


def test_dry_run_prints_with_target_prefix_and_persists_nothing(tmp_path, monkeypatch, capsys):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a")], monkeypatch)

    def explode(*a, **k):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(m.sinks, "send", explode)

    m.run(**kw, dry_run=True)

    out = capsys.readouterr().out
    assert "[core]" in out and "[all]" in out
    import os
    assert not os.path.exists(kw["state_path"])


def test_dry_run_validates_the_config(tmp_path, monkeypatch):
    kw = _setup(tmp_path, "targets:\n  - name: x\n    type: nope\n    url: u\n", [], monkeypatch)
    with pytest.raises(ValueError):
        m.run(**kw, dry_run=True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: FAIL — `run()` does not accept `targets_path`.

- [ ] **Step 3: Rewrite `aggregator/main.py`**

```python
import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import httpx

from . import sinks
from .config import load_feeds, load_targets
from .fetch import fetch_feed
from .models import Article, FeedSource
from .parse import parse_feed
from .state import load_state, save_state, select_new

log = logging.getLogger("aggregator")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Widening a filter (tiers: [1] -> [1, 2]) can make a hundred old articles
# "new" at once. The remainder arrives on the next run 12 hours later, which
# is the behaviour we want anyway.
MAX_SENDS_PER_RUN = 20


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
    targets_path: str,
    state_path: str,
    tz: str = "UTC",
    dry_run: bool = False,
    sleep=time.sleep,
) -> tuple[dict[str, int], list[str]]:
    # A malformed config raises here, before any feed is fetched. A target
    # whose ${VAR} is unset is skipped instead, so a rotated Slack secret
    # cannot silence Telegram.
    targets, skipped = load_targets(targets_path, tz)
    feeds = load_feeds(feeds_path)

    with httpx.Client() as client:
        articles = collect_articles(feeds, client)

    state = load_state(state_path)
    failed: list[str] = list(skipped)
    sent: dict[str, int] = {}

    with httpx.Client() as client:
        for target in targets:
            matched = [a for a in articles if target.matches(a)]

            if target.name not in state and not dry_run:
                state[target.name] = [a.id for a in matched]
                save_state(state_path, state)
                log.info("target %s: first run, seeded %d id(s)", target.name, len(matched))
                continue

            queue = sorted(
                select_new(matched, state.get(target.name, [])),
                key=lambda a: a.published or _EPOCH,
            )
            if len(queue) > MAX_SENDS_PER_RUN:
                log.info(
                    "target %s: %d queued, sending %d, rest next run",
                    target.name, len(queue), MAX_SENDS_PER_RUN,
                )
                queue = queue[:MAX_SENDS_PER_RUN]

            count = 0
            for i, article in enumerate(queue):
                if dry_run:
                    print(f"[{target.name}] {sinks.preview(article, target)}")
                    print("---")
                    count += 1
                else:
                    try:
                        sinks.send(article, target, client)
                    except sinks.PermanentSendError as exc:
                        # Recording the id is the point: leaving it in the queue
                        # would block every later article for this target forever.
                        log.error(
                            "target %s: permanent failure on %s (%s); skipping article",
                            target.name, article.url, exc,
                        )
                        failed.append(target.name)
                        state[target.name].append(article.id)
                    except Exception as exc:  # noqa: BLE001 - transient; retry next run
                        log.warning(
                            "target %s: transient failure on %s (%s); rest next run",
                            target.name, article.url, exc,
                        )
                        failed.append(target.name)
                        break
                    else:
                        state[target.name].append(article.id)
                        count += 1
                if i < len(queue) - 1:
                    sleep(sinks.SPECS[target.type].delay)

            sent[target.name] = count
            if not dry_run:
                save_state(state_path, state)

    return sent, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AI news RSS → messenger aggregator")
    parser.add_argument("--dry-run", action="store_true", help="print messages, do not send or persist")
    parser.add_argument("--feeds", default="feeds.yaml")
    parser.add_argument("--targets", default="targets.yaml")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--tz", default="UTC")
    args = parser.parse_args()

    sent, failed = run(
        feeds_path=args.feeds,
        targets_path=args.targets,
        state_path=args.state,
        tz=args.tz,
        dry_run=args.dry_run,
    )

    prefix = "[dry-run] would have " if args.dry_run else ""
    for name, count in sent.items():
        log.info("%starget %s: %d message(s)", prefix, name, count)
    if failed:
        # Exit non-zero so a dead target shows up as a red workflow run
        # instead of a green one that quietly delivers nothing.
        log.error("target(s) with failures: %s", ", ".join(sorted(set(failed))))
        sys.exit(1)


if __name__ == "__main__":
    main()
```


- [ ] **Step 4: Delete the superseded modules and their tests**

```bash
git rm aggregator/format.py aggregator/telegram.py tests/test_format.py tests/test_telegram.py
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS, no collection errors. Confirm nothing still imports the deleted modules:

Run: `grep -rn "format_message\|from .telegram\|aggregator.telegram" aggregator/ tests/`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add -A aggregator tests
git commit -m "feat: multi-target delivery loop with per-target state and error isolation"
```

---

### Task 7: `targets.yaml`, workflow wiring, README

**Files:**
- Create: `targets.yaml`
- Modify: `.github/workflows/aggregate.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: a runnable deployment.

The shipped `targets.yaml` contains **only the Telegram target**, so the live behaviour after this change is identical to before it. Discord, Slack, and webhook examples ship commented out — an uncommented target whose secret is unset would log an `ERROR` and turn every workflow run red for no reason.

- [ ] **Step 1: Create `targets.yaml`**

```yaml
# Delivery targets. Each entry gets its own dedup state under its `name`,
# so adding a target later starts it from "now" instead of dumping the backlog.
#
# ${VAR} is read from the environment at load time. Webhook URLs are secrets:
# anyone holding one can post to your channel. Keep them in GitHub Secrets.
#
# Optional filters on any target:
#   tiers: [1, 2]           only these feed tiers
#   tags: [openai]          only these feed tags
#   exclude_tags: [tds]     everything except these tags

targets:
  - name: tg-main
    type: telegram
    token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}

  # - name: discord-ai
  #   type: discord
  #   url: ${DISCORD_WEBHOOK_URL}
  #   tiers: [1, 2]

  # - name: slack-team
  #   type: slack
  #   url: ${SLACK_WEBHOOK_URL}

  # - name: n8n
  #   type: webhook
  #   url: ${N8N_WEBHOOK_URL}
```

- [ ] **Step 2: Verify the shipped config parses and dry-run works end to end**

```bash
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=1 .venv/bin/python -m aggregator.main --dry-run --state /tmp/nonexistent-state.json 2>&1 | head -30
```

Expected: real article previews, each line prefixed `[tg-main]`, no network sends, and no state file written at the throwaway path. This exercises the fresh-checkout dry-run path from spec §9.

Then confirm the repo's live `state.json` was not touched:

```bash
git status --porcelain state.json
```

Expected: no output.

Then confirm a missing secret degrades rather than crashes:

```bash
env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID .venv/bin/python -m aggregator.main --dry-run 2>&1 | head -5
```

Expected: `ERROR target tg-main skipped: environment variable TELEGRAM_BOT_TOKEN is not set` and a non-zero exit — not a traceback.

- [ ] **Step 3: Add the webhook secrets to the workflow**

In `.github/workflows/aggregate.yml`, replace the `env:` block of the "Run aggregator" step with:

```yaml
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          # Uncomment alongside the matching entry in targets.yaml:
          # DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          # SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          # N8N_WEBHOOK_URL: ${{ secrets.N8N_WEBHOOK_URL }}
```

Leave the rest of the workflow alone. The `concurrency` group and the state commit-back step already do the right thing.

- [ ] **Step 4: Update the README**

Change the title line to `# AI News RSS Aggregator → Telegram, Discord, Slack, webhooks` and the opening paragraph to say messages go to every configured target rather than to a Telegram channel.

Add this section after "Sources":

````markdown
## Targets

Edit `targets.yaml` — one entry per destination. Four types:

| type | required keys | notes |
|---|---|---|
| `telegram` | `token`, `chat_id` | HTML formatting |
| `discord` | `url` | incoming webhook; mentions are suppressed |
| `slack` | `url` | incoming webhook |
| `webhook` | `url` | posts the raw article as JSON |

Optional on any target: `tz` (defaults to `--tz`), and the filters `tiers`,
`tags`, `exclude_tags`. Filters combine with AND; an absent filter does not filter.

`${VAR}` is resolved from the environment at load time.

**Getting a webhook URL:** Discord — channel Settings → Integrations → Webhooks →
New Webhook → Copy Webhook URL. Slack — create a Slack app, enable Incoming
Webhooks, Add New Webhook to Workspace, copy the URL.

> **Webhook URLs are secrets.** Anyone who has one can post to your channel.
> Keep them in GitHub Secrets and reference them as `${VAR}` — never commit the
> literal URL.

Three things worth knowing:

- **Each target dedups independently.** A target that is absent from
  `state.json` seeds on its first run and posts nothing, so adding a target
  later starts it from "now" instead of dumping the backlog.
- **Renaming a target makes it a new target** — it re-seeds, and the old key
  stays in `state.json` as an orphan. Delete the old key by hand if it bothers you.
- **Two targets with the same URL deliver everything twice**, and the state
  will look perfectly healthy. The dedup key is the target `name`, not the URL.
- **The state format is one-way.** Rolling back to a single-target version of
  this code requires deleting `state.json` first, or it will repost the entire
  backlog.
````

In "Local development", change the dry-run line to:

```bash
python -m aggregator.main --dry-run   # print messages for every target, send nothing
```

- [ ] **Step 5: Run the full suite one final time**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add targets.yaml .github/workflows/aggregate.yml README.md
git commit -m "feat: ship targets.yaml, wire workflow secrets, document targets"
```
