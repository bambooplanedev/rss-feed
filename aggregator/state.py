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
