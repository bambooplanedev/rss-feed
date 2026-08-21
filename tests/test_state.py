from datetime import datetime, timezone

from aggregator.models import Article
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
    save_state(path, {"tg": ["a", "b"], "discord": ["a"]})
    assert load_state(path) == {"tg": ["a", "b"], "discord": ["a"]}


def test_state_file_is_wrapped_in_targets_key(tmp_path):
    import json
    path = str(tmp_path / "state.json")
    save_state(path, {"tg": ["a"]})
    assert json.loads(open(path, encoding="utf-8").read()) == {"targets": {"tg": ["a"]}}


def test_cap_applies_per_target_not_across_targets(tmp_path):
    path = str(tmp_path / "state.json")
    busy = [f"b{i}" for i in range(MAX_IDS + 50)]
    quiet = ["q1", "q2"]

    save_state(path, {"busy": busy, "quiet": quiet})
    state = load_state(path)

    assert len(state["busy"]) == MAX_IDS
    assert state["busy"][0] == "b50"          # oldest dropped, newest kept
    assert state["quiet"] == quiet            # a busy target cannot evict a quiet one


def test_max_ids_exceeds_the_measured_rss_window():
    # 1308 is the real window size, reconstructed from this repo's history:
    # the first production seed (2026-07-09, commit c94fa2c) wrote 1308 ids
    # across the 14 live feeds, reproduced the same day after a manual reset.
    # A cap below this number silently discards ids at seed time. Don't lower
    # MAX_IDS without confronting this measurement.
    measured_window = 1308
    assert MAX_IDS > measured_window * 1.3


def test_select_new_filters_seen():
    articles = [_article("1"), _article("2"), _article("3")]
    new = select_new(articles, ["2"])
    assert [a.id for a in new] == ["1", "3"]
