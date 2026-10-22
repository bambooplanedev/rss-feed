"""Check every feed in feeds.yaml before it is trusted with a run.

    python -m aggregator.check [feeds.yaml] [--silence-days 90] [--strict]

The same verification feeds.yaml's header promises was done by hand on
2026-08-28: HTTP 200, a non-empty body, and every entry carrying a parseable
publication date. Done by hand, it was done once. This makes it a command.

Deterministic on purpose: the verdict is a pure function of what each server
returned and of `now`, the rules apply in a fixed order, and feeds are
reported in file order. Two runs against the same responses print the same
lines. The network is the only thing that varies, and it is fetched once per
feed, with the aggregator's own single retry on a transient status.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

import feedparser
import httpx

from .config import load_feeds
from .fetch import _RETRY_STATUSES, RETRY_DELAY, USER_AGENT
from .models import FeedSource
from .parse import _published

#: A feed that has not published for this long is reported, not failed.
#: Eugene Yan and Lilian Weng write a few long posts a year; silence is their
#: normal, and a red check for it would teach the reader to ignore red.
SILENCE_DAYS = 90

OK, WARN, FAIL = "ok", "warn", "fail"


class Fetched(NamedTuple):
    status: int | None   # None: no HTTP answer at all
    content: bytes
    error: str = ""


class Verdict(NamedTuple):
    tag: str
    level: str
    reasons: tuple[str, ...]
    entries: int = 0
    newest: datetime | None = None


def fetch(url: str, client: httpx.Client, timeout: float = 20.0,
          *, sleep: Callable[[float], None] = time.sleep) -> Fetched:
    """One GET, and one more after a transient failure — never more.

    Unlike fetch.fetch_feed this keeps the status: the check is about the
    status, so "it raised" is not enough to report. Retry-After is ignored on
    purpose — a fixed delay keeps the run's length independent of the server.
    """
    def once() -> Fetched:
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT},
                              timeout=timeout, follow_redirects=True)
        except httpx.TransportError as exc:
            return Fetched(None, b"", type(exc).__name__)
        return Fetched(resp.status_code, resp.content)

    first = once()
    if first.status is not None and first.status not in _RETRY_STATUSES:
        return first
    sleep(RETRY_DELAY)
    return once()


def judge(source: FeedSource, got: Fetched, now: datetime,
          silence_days: int = SILENCE_DAYS) -> Verdict:
    """The rules, in the order they are checked. A failing rule stops the rest:
    an empty body has no dates worth reporting on."""
    if got.status is None:
        return Verdict(source.tag, FAIL, (f"no response: {got.error}",))
    if got.status != 200:
        return Verdict(source.tag, FAIL, (f"HTTP {got.status}",))

    parsed = feedparser.parse(got.content)
    entries = parsed.entries
    if not entries:
        return Verdict(source.tag, FAIL, ("no entries",))

    dates = [d for d in (_published(e) for e in entries) if d is not None]
    if not dates:
        return Verdict(source.tag, FAIL, (f"none of {len(entries)} entries has a parseable date",),
                       len(entries))

    reasons = []
    if parsed.bozo:
        reasons.append(f"malformed body: {type(parsed.get('bozo_exception')).__name__}")
    undated = len(entries) - len(dates)
    if undated:
        reasons.append(f"{undated} of {len(entries)} entries undated (dropped at parse)")
    newest = max(dates)
    if newest < now - timedelta(days=silence_days):
        reasons.append(f"silent since {newest.date().isoformat()}")
    return Verdict(source.tag, WARN if reasons else OK, tuple(reasons), len(entries), newest)


def render(v: Verdict) -> str:
    newest = v.newest.date().isoformat() if v.newest else "-"
    tail = "; ".join(v.reasons)
    return f"{v.level.upper():4}  {v.tag:16} entries={v.entries:<4} newest={newest}  {tail}".rstrip()


def run(feeds: list[FeedSource], get: Callable[[str], Fetched], now: datetime,
        silence_days: int = SILENCE_DAYS) -> list[Verdict]:
    return [judge(f, get(f.url), now, silence_days) for f in feeds]


def exit_code(verdicts: list[Verdict], strict: bool = False) -> int:
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(v.level in bad for v in verdicts) else 0


def main(argv: list[str] | None = None, *, get=None, now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m aggregator.check", description=__doc__.splitlines()[0])
    p.add_argument("feeds", nargs="?", default="feeds.yaml")
    p.add_argument("--silence-days", type=int, default=SILENCE_DAYS)
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = p.parse_args(argv)

    feeds = load_feeds(args.feeds)
    now = now or datetime.now(timezone.utc)
    if get is None:
        client = httpx.Client()
        get = lambda url: fetch(url, client)  # noqa: E731
    verdicts = run(feeds, get, now, args.silence_days)

    for v in verdicts:
        print(render(v))
    counts = {lvl: sum(v.level == lvl for v in verdicts) for lvl in (OK, WARN, FAIL)}
    print(f"{len(verdicts)} feeds: {counts[OK]} ok, {counts[WARN]} warn, {counts[FAIL]} fail")
    return exit_code(verdicts, args.strict)


if __name__ == "__main__":
    sys.exit(main())
