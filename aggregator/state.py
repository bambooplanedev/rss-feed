import json
from pathlib import Path

from .models import Article

# Per target, not per file: a shared cap would let a busy target evict a quiet
# target's ids and cause silent reposts. The cap must exceed the RSS window
# with headroom: the measured window is 1308 ids across the current 14 feeds
# (first production seed, 2026-07-09). A cap below the window discards ids at
# seed time, so select_new finds hundreds of "new" articles that were really
# already published, and a channel reposts its backlog indefinitely.
MAX_IDS = 2000


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
