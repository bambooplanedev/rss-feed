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
