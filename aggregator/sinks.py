import html
import json
import re
import time
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


MAX_RETRIES = 3
MAX_BACKOFF = 60.0


class PermanentSendError(Exception):
    """A non-retryable 4xx. The article will never reach this target."""


class TransientSendError(Exception):
    """A 5xx or other retryable failure, sanitized like PermanentSendError.

    httpx.HTTPStatusError's message embeds the full request URL — for
    Telegram that's the bot token, for Discord/Slack the webhook secret.
    This carries the same target name / status / body-snippet shape without
    the URL, so a routine 503 doesn't leak credentials to the run log. It is
    deliberately NOT a PermanentSendError: main.py must still retry the
    article next run instead of abandoning it.
    """


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
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # exc's own message embeds the full request URL (token/webhook
            # secret and all) — raise a clean one instead of letting it reach
            # main.py's log.warning verbatim.
            raise TransientSendError(
                f"target {target.name!r}: HTTP {resp.status_code} {resp.text[:200]}"
            ) from exc
        return
    raise RuntimeError(f"target {target.name!r}: rate limited after {max_retries} attempts")
