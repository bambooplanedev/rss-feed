import logging
import os
import re
from zoneinfo import ZoneInfo

import yaml

from .models import FeedSource, Target
from .sinks import SPECS

log = logging.getLogger(__name__)


def load_feeds(path: str) -> list[FeedSource]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("feeds") or []
    if not raw:
        raise ValueError(f"{path} defines no feeds")

    feeds = []
    for i, s in enumerate(raw):
        missing = [k for k in ("name", "url", "tag", "tier") if k not in s]
        if missing:
            raise ValueError(
                f"feed #{i} in {path} is missing required field(s): {', '.join(missing)}"
            )
        # Coerced, not taken as written: `tier: "1"` is a plausible YAML slip
        # and Target.matches compares tiers with `in`, so a string tier makes a
        # `tiers:` filter match nothing, deliver nothing, and stay green.
        try:
            tier = int(s["tier"])
        except (TypeError, ValueError):
            raise ValueError(
                f"feed #{i} in {path}: tier must be a number, got {s['tier']!r}"
            ) from None
        feeds.append(FeedSource(name=s["name"], url=s["url"], tag=s["tag"], tier=tier))

    # Per-target seed state keys on `tag`, so two feeds sharing one are
    # indistinguishable there: seeding either marks both seeded and silently
    # swallows the other's window.
    tags = [f.tag for f in feeds]
    duplicates = sorted({t for t in tags if tags.count(t) > 1})
    if duplicates:
        raise ValueError(f"duplicate feed tag(s) in {path}: {', '.join(duplicates)}")
    return feeds


_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_VALUE_KEYS = ("url", "token", "chat_id", "tz")
_FILTER_KEYS = ("tiers", "tags", "exclude_tags")
_ALLOWED_KEYS = {"name", "type", *_VALUE_KEYS, *_FILTER_KEYS}
_REQUIRED_KEYS = {"telegram": ("token", "chat_id")}


class _MissingEnv(Exception):
    """An environment variable referenced by the config is not set."""


def _resolve(value, key: str):
    if not isinstance(value, str):
        return value
    match = _ENV_RE.match(value.strip())
    if not match:
        return value
    resolved = os.environ.get(match.group(1), "")
    if not resolved:
        # Name the variable, never its value — this line reaches the CI log.
        raise _MissingEnv(f"environment variable {match.group(1)} is not set (key {key!r})")
    return resolved


def load_targets(path: str, default_tz: str = "UTC") -> tuple[list[Target], list[str]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("targets") or []
    if not raw:
        raise ValueError(f"{path} defines no targets")

    targets: list[Target] = []
    skipped: list[str] = []
    seen_names: set[str] = set()

    for i, entry in enumerate(raw):
        name, type_ = entry.get("name"), entry.get("type")
        if not name or not type_:
            raise ValueError(f"target #{i} in {path} is missing 'name' or 'type'")
        if name in seen_names:
            raise ValueError(f"duplicate target name {name!r} in {path}")
        seen_names.add(name)
        if type_ not in SPECS:
            raise ValueError(
                f"target {name!r}: unknown type {type_!r}; known types: {', '.join(sorted(SPECS))}"
            )
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(f"target {name!r}: unknown key(s) {', '.join(sorted(unknown))}")

        try:
            values = {k: _resolve(entry[k], k) for k in _VALUE_KEYS if k in entry}
        except _MissingEnv as exc:
            log.error("target %s skipped: %s", name, exc)
            skipped.append(name)
            continue

        for required in _REQUIRED_KEYS.get(type_, ("url",)):
            if not values.get(required):
                raise ValueError(f"target {name!r} (type {type_}) requires {required!r}")

        tz_name = values.pop("tz", default_tz)
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise ValueError(f"target {name!r}: invalid tz {tz_name!r}") from exc

        targets.append(
            Target(
                name=name,
                type=type_,
                tz=tz,
                tiers=tuple(entry.get("tiers") or ()),
                tags=tuple(entry.get("tags") or ()),
                exclude_tags=tuple(entry.get("exclude_tags") or ()),
                **values,
            )
        )
    return targets, skipped
