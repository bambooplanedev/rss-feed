import json
from pathlib import Path

from .models import Article

# Per feed, not per target. Pruning is the real bound — after each run a bucket
# holds only what its feed still offers, which is the RSS window, tens of ids.
# This cap is the backstop for a feed that offers more than that in one run.
#
# 5 articles/feed/day against parse.MAX_AGE_DAYS is 150; times a 3x rate
# headroom is 450. The measured worst case in the current list is simonw at
# 3.7/day and the list averages 0.6/day.
#
# It must exceed what a feed can OFFER, not what it can publish in the window.
# Those coincide only while the age bound has no holes: a feed whose entries
# stop being datable is dropped entirely rather than admitted undated, which is
# what keeps the two equal.
MAX_IDS_PER_FEED = 500


def load_state(path: str) -> dict[str, dict]:
    """Returns entries verbatim: {"seen": {tag: [ids]}}. main._migrate converts
    older shapes, where the feed list and this run's articles are in scope."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("targets", {})


def save_state(path: str, state: dict[str, dict]) -> None:
    # Dedupe before capping, not after: a re-seed re-appends ids already in the
    # bucket, and deduping after the slice would let those duplicates push live
    # ids out of the window first.
    #
    # The slice keeps the tail, which is the newest ONLY because run builds every
    # bucket in publication order ascending. Do not try to sort here — this
    # function receives ids and has never seen an Article.
    out = {
        name: {
            "seen": {
                tag: list(dict.fromkeys(ids))[-MAX_IDS_PER_FEED:]
                for tag, ids in entry["seen"].items()
            }
        }
        for name, entry in state.items()
    }
    Path(path).write_text(
        json.dumps({"targets": out}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def select_new(articles: list[Article], seen: dict[str, list[str]]) -> list[Article]:
    """Membership against the union of every bucket, so a syndicated article is
    delivered once rather than once per feed carrying it. Delivery writes the id
    to its own article's bucket. Pruning, though, is strictly per tag (main.run
    prunes only `offered[tag]` against `buckets[tag]`): an id is retained only
    while *its own* feed still offers it. An id recorded under tag A that rotates
    out of feed A's window is dropped even while feed B still offers the same
    article — a syndicated article can then be redelivered under feed B. Latent
    today because no two feeds in feeds.yaml carry the same URL."""
    everything = {i for ids in seen.values() for i in ids}
    return [a for a in articles if a.id not in everything]
