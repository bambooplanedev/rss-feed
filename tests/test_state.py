from datetime import datetime, timezone

from aggregator.config import load_feeds
from aggregator.models import Article
from aggregator.parse import MAX_AGE_DAYS
from aggregator.state import load_state, save_state, select_new, MAX_IDS


def _article(id_):
    return Article(
        id=id_, title="t", url="https://ex.com/" + id_, source="s",
        tag="x", tier=1, published=datetime.now(timezone.utc), summary="",
    )


def test_load_state_returns_empty_dict_when_absent(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == {}


def test_roundtrip_per_target(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": {"seen": ["a", "b"], "seeded_tags": ["x"]},
                      "discord": {"seen": ["a"], "seeded_tags": []}})
    assert load_state(path) == {"tg": {"seen": ["a", "b"], "seeded_tags": ["x"]},
                                "discord": {"seen": ["a"], "seeded_tags": []}}


def test_state_file_is_wrapped_in_targets_key(tmp_path):
    import json
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": {"seen": ["a"], "seeded_tags": ["s"]}})
    assert json.loads(open(path, encoding="utf-8").read()) == {
        "targets": {"tg": {"seen": ["a"], "seeded_tags": ["s"]}}
    }


def test_cap_applies_per_target_not_across_targets(tmp_path):
    path = str(tmp_path / "state.json")
    busy = [f"b{i}" for i in range(MAX_IDS + 50)]
    quiet = ["q1", "q2"]

    save_state(path, {"busy": {"seen": busy, "seeded_tags": []},
                      "quiet": {"seen": quiet, "seeded_tags": []}})
    state = load_state(path)

    assert len(state["busy"]["seen"]) == MAX_IDS
    assert state["busy"]["seen"][0] == "b50"       # oldest dropped, newest kept
    assert state["quiet"]["seen"] == quiet         # a busy target cannot evict a quiet one


# Superseded: IDS_PER_FEED = 93 came from the 2026-07-09 seed, which wrote 1308
# ids across 14 feeds that shipped their whole archives. parse.MAX_AGE_DAYS
# ended that regime — a feed can now only contribute what it published inside
# the window, however deep its archive goes.
#
# 5/day/feed is the bound, applied uniformly to every feed. That is already the
# safety margin: the measured worst case in the current list is simonw at
# 3.7/day (reconstructed from this repo's state.json history) and the average is
# 0.6/day. Multiplying a uniform 5 by every feed is why there is no further
# fudge factor here.
MAX_ARTICLES_PER_FEED_PER_DAY = 5

# Sized to let the feed list roughly double before anyone looks at this again.
GROWTH_HEADROOM_FEEDS = 15


def test_max_ids_clears_the_age_window_of_the_current_feeds():
    """Pinned to feeds.yaml, not a frozen count: a cap asserted against a fixed
    number falls silent exactly as feeds are added, which is when it matters."""
    live_feeds = len(load_feeds("feeds.yaml"))

    assert MAX_IDS > live_feeds * MAX_ARTICLES_PER_FEED_PER_DAY * MAX_AGE_DAYS


def test_max_ids_leaves_room_to_grow_the_feed_list():
    """Without headroom the cap evicts ids still inside the age window and the
    channel reposts — silently, and only after the feed is already added."""
    live_feeds = len(load_feeds("feeds.yaml"))

    assert MAX_IDS > (live_feeds + GROWTH_HEADROOM_FEEDS) * MAX_ARTICLES_PER_FEED_PER_DAY * MAX_AGE_DAYS


def test_select_new_filters_seen():
    articles = [_article("1"), _article("2"), _article("3")]
    new = select_new(articles, ["2"])
    assert [a.id for a in new] == ["1", "3"]


def test_save_state_roundtrips_seen_and_seeded_tags(tmp_path):
    path = str(tmp_path / "state.json")
    entry = {"seen": ["a", "b"], "seeded_tags": ["alpha", "beta"]}

    save_state(path, {"t": entry})

    assert load_state(path)["t"] == entry


def test_save_state_dedupes_seen_before_capping(tmp_path):
    """Renaming a feed's tag re-seeds it, re-appending ids already in `seen`.
    Left alone they accumulate against MAX_IDS and evict live ids."""
    path = str(tmp_path / "state.json")

    save_state(path, {"t": {"seen": ["a", "b", "a", "c", "b"], "seeded_tags": []}})

    assert load_state(path)["t"]["seen"] == ["a", "b", "c"]


def test_seeded_tags_are_not_capped(tmp_path):
    path = str(tmp_path / "state.json")
    tags = [f"tag{i}" for i in range(MAX_IDS + 10)]

    save_state(path, {"t": {"seen": [], "seeded_tags": tags}})

    assert load_state(path)["t"]["seeded_tags"] == tags
