import json
from pathlib import Path

from .models import Article

# Per target, not per file: a shared cap would let a busy target evict a quiet
# target's ids and cause silent reposts. The cap must exceed the RSS window with
# headroom: the measured window is 1308 ids across 14 feeds (first production
# seed, 2026-07-09), so ~93 ids per feed. A cap below the window discards ids at
# seed time, so select_new finds hundreds of "new" articles that were really
# already published, and a channel reposts its backlog indefinitely.
#
# Sized for ~30 feeds at 1.5x that rate. Adding a feed appends its entire window
# to `seen` at seed time, so headroom is consumed per feed added, not per article
# delivered; at the old cap of 2000 six added feeds were enough to start evicting
# live ids. test_state.py pins both properties to feeds.yaml so the cap fails
# loudly when the feed list outgrows it, rather than after the reposts land.
#
# This is a ceiling, not an allocation — `seen` is ~1440 ids today. Reaching it
# is not free: state.json is committed twice a day, so a `seen` near this cap is
# a ~300KB single-line blob per run. Fix that before growing the feed list far.
MAX_IDS = 5000


def load_state(path: str) -> dict[str, list[str] | dict]:
    """Returns entries verbatim: {"seen": [...], "seeded_tags": [...]} for
    current files, a bare id list for pre-per-feed-seeding ones. main.run
    migrates the legacy shape, where the feed list is in scope."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("targets", {})


def save_state(path: str, state: dict[str, dict]) -> None:
    # Dedupe before capping, not after: renaming a feed's tag re-seeds it and
    # re-appends ids already in `seen`. Deduping after the slice would let those
    # duplicates push live ids out of the window first.
    out = {
        name: {
            "seen": list(dict.fromkeys(entry["seen"]))[-MAX_IDS:],
            "seeded_tags": entry["seeded_tags"],
        }
        for name, entry in state.items()
    }
    Path(path).write_text(
        json.dumps({"targets": out}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def select_new(articles: list[Article], seen_ids: list[str]) -> list[Article]:
    seen = set(seen_ids)
    return [a for a in articles if a.id not in seen]
