from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tag: str


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    url: str
    source: str
    tag: str
    published: datetime | None
    summary: str
