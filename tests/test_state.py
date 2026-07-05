from datetime import datetime, timezone

from aggregator.models import Article
from aggregator.state import load_state, save_state, select_new, MAX_IDS


def _article(id_):
    return Article(
        id=id_, title="t", url="https://ex.com/" + id_, source="s",
        tag="x", published=datetime.now(timezone.utc), summary="",
    )


def test_load_state_missing_file_returns_none(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) is None


def test_save_then_load_round_trips(tmp_path):
    p = str(tmp_path / "state.json")
    save_state(p, ["a", "b", "c"])
    assert load_state(p) == ["a", "b", "c"]


def test_save_state_caps_to_most_recent(tmp_path):
    p = str(tmp_path / "state.json")
    ids = [str(i) for i in range(MAX_IDS + 50)]
    save_state(p, ids)
    loaded = load_state(p)
    assert len(loaded) == MAX_IDS
    assert loaded[-1] == str(MAX_IDS + 49)  # newest retained
    assert loaded[0] == "50"                # oldest dropped


def test_select_new_filters_seen():
    articles = [_article("1"), _article("2"), _article("3")]
    new = select_new(articles, ["2"])
    assert [a.id for a in new] == ["1", "3"]
