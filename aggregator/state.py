import json
from pathlib import Path

from .models import Article

# Per target, not per file: a shared cap would let a busy target evict a quiet
# target's ids and cause silent reposts. The cap must exceed what the feed list
# can put in the age window with headroom: parse.MAX_AGE_DAYS caps what any one
# feed contributes to `seen` at 30 days of its own publication rate, not its
# whole archive, so the bound is per-feed-per-day, not per-feed-total. A cap
# below that window discards ids still inside it, so select_new finds "new"
# articles that were really already published, and a channel reposts its
# backlog indefinitely.
#
# Sized at 5 articles/feed/day, uniform across the list, with headroom for the
# feed list to roughly double before anyone needs to revisit this. 5/day is
# already a safety margin above the measured worst case (simonw at 3.7/day; the
# list averages 0.6/day). Past the cutoff, `seen` grows with what's delivered,
# not with what's added — a feed added today only ever seeds its own window, so
# headroom is consumed by accumulation over time, not by the act of adding a
# feed. test_state.py pins both the window and the headroom to feeds.yaml so the
# cap fails loudly when the feed list outgrows it, rather than after the
# reposts land.
#
# This is a ceiling, not an allocation — `seen` is 153 ids today. Reaching it is
# not free: state.json is committed twice a day, so a `seen` near this cap is a
# large single-line blob per run. Fix that before growing the feed list far.
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
