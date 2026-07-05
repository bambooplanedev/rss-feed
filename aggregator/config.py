import yaml

from .models import FeedSource


def load_feeds(path: str) -> list[FeedSource]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [
        FeedSource(name=s["name"], url=s["url"], tag=s["tag"])
        for s in data["feeds"]
    ]
