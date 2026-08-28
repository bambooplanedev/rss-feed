from datetime import datetime, timezone

import pytest

from aggregator.models import Article
from aggregator.state import load_state, save_state, select_new, MAX_IDS_PER_FEED


def _article(id_, tag="x"):
    return Article(
        id=id_, title="t", url="https://ex.com/" + id_, source="s",
        tag=tag, tier=1, published=datetime.now(timezone.utc), summary="",
    )


def test_load_state_returns_empty_dict_when_absent(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == {}


def test_roundtrip_buckets(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": {"seen": {"a": ["1", "2"], "b": ["3"]}}})
    assert load_state(path) == {"tg": {"seen": {"a": ["1", "2"], "b": ["3"]}}}


def test_cap_applies_per_bucket_not_per_target(tmp_path):
    """A busy feed must not evict a quiet feed's ids — the same isolation the
    per-target cap used to give, one level down."""
    path = str(tmp_path / "state.json")
    busy = [f"b{i}" for i in range(MAX_IDS_PER_FEED + 50)]
    save_state(path, {"tg": {"seen": {"busy": busy, "quiet": ["q1", "q2"]}}})

    seen = load_state(path)["tg"]["seen"]

    assert len(seen["busy"]) == MAX_IDS_PER_FEED
    assert seen["busy"][0] == "b50"          # oldest dropped, newest kept
    assert seen["quiet"] == ["q1", "q2"]


def test_the_cap_keeps_the_newest_given_run_writes_buckets_oldest_first(tmp_path):
    """run builds every bucket in publication order ascending, so [-N:] keeps
    the newest. save_state cannot sort — it has ids, never Articles."""
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": {"seen": {"f": [f"id{i}" for i in range(MAX_IDS_PER_FEED + 3)]}}})
    assert load_state(path)["tg"]["seen"]["f"][-1] == f"id{MAX_IDS_PER_FEED + 2}"


def test_save_state_dedupes_within_a_bucket(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": {"seen": {"f": ["a", "b", "a", "c", "b"]}}})
    assert load_state(path)["tg"]["seen"]["f"] == ["a", "b", "c"]


def test_select_new_reads_the_union_of_buckets():
    """Cross-feed dedup: an id already seen under one tag is not new under
    another. Per-tag lookup alone would deliver a syndicated article twice."""
    articles = [_article("1", tag="x"), _article("2", tag="y")]
    assert [a.id for a in select_new(articles, {"x": ["2"], "y": []})] == ["1"]


def test_select_new_with_no_buckets_returns_everything():
    articles = [_article("1", tag="x")]
    assert select_new(articles, {}) == articles


# The cap is per feed now, so it no longer scales with the feed list. Rather
# than assume a rate, measure one: the largest bucket actually committed in
# state.json is real traffic, not a guess, and the cap only needs to clear it
# with room to spare. Skips until state.json is migrated to buckets — on this
# commit it is still the pre-migration flat shape, so there is nothing to
# measure yet.
def test_max_ids_per_feed_clears_the_largest_bucket_committed():
    committed = load_state("state.json")
    buckets = [entry["seen"] for entry in committed.values()
               if isinstance(entry.get("seen"), dict)]
    if not buckets:
        pytest.skip("state.json is not migrated to per-feed buckets yet")
    largest = max(len(ids) for seen in buckets for ids in seen.values())
    assert MAX_IDS_PER_FEED > largest
