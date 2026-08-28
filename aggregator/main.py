import argparse
import logging
import sys
import time
from typing import NamedTuple

import httpx

from . import sinks
from .config import load_feeds, load_targets
from .fetch import fetch_feed
from .models import Article, FeedSource
from .parse import ParseResult, parse_feed
from .state import load_state, save_state, select_new

log = logging.getLogger("aggregator")

# Bounds a genuine backlog: a target that fell behind, or a feed re-seeding
# after an outage. It is no longer what absorbs a filter edit — widening
# `tiers` now seeds the newly matching feeds rather than delivering their
# backlog, since you widen a filter for future news, not for stale articles.
MAX_SENDS_PER_RUN = 20

# Telegram answers `chat not found` with HTTP 400, the same code it uses for a
# malformed message, so the status alone cannot tell a dead target from one bad
# article. What separates them is shape: a bad article is sporadic, a dead
# target fails universally. Three failures with no delivery in between ends the
# target's queue for this run — a stop-hammering rule, not the correctness
# mechanism. The buffering below is the correctness mechanism.
#
# Equal to sinks.MAX_RETRIES by coincidence. Do not unify them; they count
# different things.
PERMANENT_FAILURE_STREAK = 3


class CollectResult(NamedTuple):
    articles: list[Article]
    failed: list[str]   # tags that make the run exit non-zero
    ok: list[str]       # tags safe to seed against and prune against

    # `failed` and `ok` are not complements. A bozo feed is in neither: it
    # parsed something, so it is not worth a red run, but the body may be a
    # truncated page or an interstitial, so its window is not evidence of what
    # the feed holds and nothing may be pruned against it.


def collect_articles(feeds: list[FeedSource], client: httpx.Client) -> CollectResult:
    articles: list[Article] = []
    failed: list[str] = []
    ok: list[str] = []
    for feed in feeds:
        try:
            content = fetch_feed(feed.url, client)
            result = parse_feed(content, feed)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            log.warning("feed failed: %s (%s)", feed.url, exc)
            failed.append(feed.tag)
            continue
        if result.undated and not result.dated:
            log.error(
                "feed %s produced %d entries and no usable dates; nothing from it "
                "can reach a target", feed.url, result.undated,
            )
            failed.append(feed.tag)
        elif not result.bozo:
            ok.append(feed.tag)
        articles.extend(result.articles)
    return CollectResult(articles, failed, ok)


def _migrate(state: dict, all_tags: set[str]) -> dict[str, dict]:
    """Normalise every state entry to {"seen": [...], "seeded_tags": [...]}.

    Done here rather than in load_state because this is where the feed list is
    in scope, which means the legacy shape can be filled in directly and the
    bad state (a null seeded_tags reaching save_state) is unrepresentable.

    A bare list predates per-feed seeding. Such a target has been delivering
    against these feeds for months, so they are seeded by definition; assuming
    the opposite would silently swallow their next articles. Tags whose feed has
    left feeds.yaml are pruned, or a feed removed and re-added later stays
    latched while its ids age out of `seen` and dumps its window on return.
    """
    return {
        name: (
            {"seen": entry, "seeded_tags": sorted(all_tags)}
            if isinstance(entry, list)
            else {
                "seen": entry["seen"],
                "seeded_tags": sorted(set(entry.get("seeded_tags", ())) & all_tags),
            }
        )
        for name, entry in state.items()
    }


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
        collected = collect_articles(feeds, client)
    articles, feed_failures, ok_tags = collected

    state = _migrate(load_state(state_path), {f.tag for f in feeds})
    failed: list[str] = list(skipped) + feed_failures
    sent: dict[str, int] = {}

    with httpx.Client() as client:
        for target in targets:
            matched = [a for a in articles if target.matches(a)]
            entry = state.setdefault(target.name, {"seen": [], "seeded_tags": []})

            # Per feed, not per target. A feed that was down during this target's
            # first run is absent from `matched`, stays unseeded, and seeds when
            # it recovers instead of dumping its window 20-per-run. Derived from
            # `matched`, so it is filtered exactly as delivery is: a tag the
            # target's filter excludes must not latch with zero ids recorded.
            newly_seeded = {a.tag for a in matched} - set(entry["seeded_tags"])
            if newly_seeded and not dry_run:
                entry["seen"].extend(a.id for a in matched if a.tag in newly_seeded)
                entry["seeded_tags"] = sorted(set(entry["seeded_tags"]) | newly_seeded)
                log.info(
                    "target %s: seeded %d feed(s): %s",
                    target.name, len(newly_seeded), ", ".join(sorted(newly_seeded)),
                )
            elif newly_seeded:
                log.info(
                    "[dry-run] target %s: a real run would seed %s instead of sending",
                    target.name, ", ".join(sorted(newly_seeded)),
                )

            # `matched` whole, not filtered by newly_seeded: the ids just seeded
            # are already in `seen`, and leaving them in is what lets dry-run
            # keep printing them.
            queue = sorted(
                select_new(matched, entry["seen"]),
                key=lambda a: a.published,
            )
            if len(queue) > MAX_SENDS_PER_RUN:
                log.info(
                    "target %s: %d queued, sending %d, rest next run",
                    target.name, len(queue), MAX_SENDS_PER_RUN,
                )
                queue = queue[:MAX_SENDS_PER_RUN]

            count = 0
            delivered = False
            pending: list[str] = []
            streak = 0
            for i, article in enumerate(queue):
                if dry_run:
                    print(f"[{target.name}] {sinks.preview(article, target)}")
                    print("---")
                    count += 1
                else:
                    try:
                        sinks.send(article, target, client)
                    except sinks.TargetDeadError as exc:
                        # Revoked token, kicked bot, deleted webhook. Falling
                        # through to the transient handler below would work, but
                        # would tell the operator it retries next run — it never
                        # heals on its own. Nothing is recorded: the whole queue
                        # must survive for whoever fixes the credential.
                        log.error(
                            "target %s: unreachable (%s); no article recorded, "
                            "whole queue retries next run",
                            target.name, exc,
                        )
                        failed.append(target.name)
                        break
                    except sinks.PermanentSendError as exc:
                        # Recorded only once this target has proved it can
                        # deliver. Until then the id stays pending: a rotated
                        # credential fails every article, and recording those
                        # destroys the queue an article at a time.
                        log.error(
                            "target %s: permanent failure on %s (%s); %s",
                            target.name, article.url, exc,
                            "skipping article" if delivered else "holding pending a delivery",
                        )
                        failed.append(target.name)
                        streak += 1
                        if delivered:
                            entry["seen"].append(article.id)
                        else:
                            pending.append(article.id)
                        if streak >= PERMANENT_FAILURE_STREAK:
                            log.error(
                                "target %s: %d consecutive permanent failures; treating as "
                                "unreachable, %s retries next run",
                                target.name, streak,
                                "the rest of the queue" if delivered else "the whole queue",
                            )
                            break
                    except Exception as exc:  # noqa: BLE001 - transient; retry next run
                        log.warning(
                            "target %s: transient failure on %s (%s); rest next run",
                            target.name, article.url, exc,
                        )
                        failed.append(target.name)
                        break
                    else:
                        # Pending first, then this id: `seen` then follows the
                        # order articles were attempted, which is what
                        # save_state's [-MAX_IDS:] assumes when it keeps "the
                        # newest".
                        entry["seen"].extend(pending)
                        pending.clear()
                        entry["seen"].append(article.id)
                        delivered = True
                        streak = 0
                        count += 1

                # Loop-scoped so it paces after a permanent failure too, not
                # just a clean send. Guarded so dry-run doesn't idle, and a
                # transient failure already `break`s out before this runs.
                if not dry_run and i < len(queue) - 1:
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
