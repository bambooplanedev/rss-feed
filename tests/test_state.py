from datetime import datetime, timezone

from aggregator.models import Article
from aggregator.parse import MAX_AGE_DAYS
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


# The cap is per feed now, so it no longer scales with the feed list and the
# headroom is a rate multiple rather than a feed count. 5/day is already above
# the measured worst case (simonw at 3.7/day; the list averages 0.6/day), and
# pruning means a bucket normally settles at the feed's RSS window, well under
# this. The cap is a backstop, not the primary bound.
MAX_ARTICLES_PER_FEED_PER_DAY = 5
RATE_HEADROOM = 3


def test_max_ids_per_feed_clears_a_feed_publishing_far_above_the_measured_rate():
    assert MAX_IDS_PER_FEED > MAX_ARTICLES_PER_FEED_PER_DAY * MAX_AGE_DAYS * RATE_HEADROOM
