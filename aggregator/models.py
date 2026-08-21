from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tag: str
    tier: int


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    url: str
    source: str
    tag: str
    tier: int
    published: datetime | None
    summary: str


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    tz: ZoneInfo
    url: str = ""
    token: str = ""
    chat_id: str = ""
    tiers: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()

    def matches(self, article: Article) -> bool:
        if self.tiers and article.tier not in self.tiers:
            return False
        if self.tags and article.tag not in self.tags:
            return False
        return article.tag not in self.exclude_tags
