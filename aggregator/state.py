import json
from pathlib import Path

from .models import Article

MAX_IDS = 2000


def load_state(path: str) -> list[str] | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("seen", [])


def save_state(path: str, seen_ids: list[str]) -> None:
    capped = seen_ids[-MAX_IDS:]
    Path(path).write_text(
        json.dumps({"seen": capped}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def select_new(articles: list[Article], seen_ids: list[str]) -> list[Article]:
    seen = set(seen_ids)
    return [a for a in articles if a.id not in seen]
