from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import aggregator.main as m
from aggregator.models import Article
from aggregator.sinks import PermanentSendError
from aggregator.state import load_state, save_state

FEEDS = "feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n"


def _article(id_, hour=9, tier=1, tag="s"):
    return Article(
        id=id_, title=f"T{id_}", url=f"https://ex.com/{id_}", source="S",
        tag=tag, tier=tier,
        published=datetime(2026, 7, 2, hour, tzinfo=timezone.utc), summary="",
    )


def _setup(tmp_path, targets_yaml, articles, monkeypatch):
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(targets_yaml, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: articles)
    return dict(
        feeds_path=str(tmp_path / "feeds.yaml"),
        targets_path=str(tmp_path / "targets.yaml"),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda _: None,
    )


TWO_TARGETS = (
    "targets:\n"
    "  - name: core\n    type: discord\n    url: https://ex.com/core\n    tiers: [1]\n"
    "  - name: all\n    type: slack\n    url: https://ex.com/all\n"
)


def test_new_target_is_seeded_and_sends_nothing(tmp_path, monkeypatch):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a"), _article("b")], monkeypatch)
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert sent == []
    assert counts == {}
    assert failed == []
    assert set(load_state(kw["state_path"])["core"]) == {"a", "b"}


def test_filters_route_different_articles_to_different_targets(tmp_path, monkeypatch):
    articles = [_article("t1", tier=1), _article("t3", tier=3)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    m.run(**kw)

    assert sorted(sent) == [("all", "t1"), ("all", "t3"), ("core", "t1")]


def test_articles_are_fetched_once_for_all_targets(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_collect(feeds, client):
        calls["n"] += 1
        return [_article("a")]

    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", counting_collect)
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)
    state_path = str(tmp_path / "state.json")
    save_state(state_path, {"core": [], "all": []})

    m.run(feeds_path=str(tmp_path / "feeds.yaml"),
          targets_path=str(tmp_path / "targets.yaml"),
          state_path=state_path, sleep=lambda _: None)

    assert calls["n"] == 1


def test_transient_failure_stops_one_target_and_spares_the_others(tmp_path, monkeypatch):
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []

    def flaky(article, target, client):
        if target.name == "core":
            raise RuntimeError("service down")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", flaky)

    counts, failed = m.run(**kw)

    state = load_state(kw["state_path"])
    assert state["core"] == []                     # nothing recorded: retry next run
    assert set(state["all"]) == {"a", "b"}         # the other target is unaffected
    assert counts["all"] == 2
    assert "core" in failed


def test_permanent_failure_does_not_block_the_queue(tmp_path, monkeypatch):
    """A deleted webhook must not poison every later article for that target."""
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("404")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", picky)

    counts, failed = m.run(**kw)

    assert ("core", "good") in sent                # the queue kept moving
    assert set(load_state(kw["state_path"])["core"]) == {"bad", "good"}
    assert counts["core"] == 1                     # 'bad' is recorded but not counted as sent
    assert "core" in failed


def test_send_cap_leaves_the_remainder_for_the_next_run(tmp_path, monkeypatch):
    articles = [_article(f"a{i}", hour=9) for i in range(m.MAX_SENDS_PER_RUN + 5)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, _ = m.run(**kw)

    assert counts["core"] == m.MAX_SENDS_PER_RUN
    assert len(load_state(kw["state_path"])["core"]) == m.MAX_SENDS_PER_RUN


def test_unset_env_in_one_target_does_not_reduce_the_others(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    targets = (
        "targets:\n"
        "  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n"
        "  - name: alive\n    type: slack\n    url: https://ex.com/alive\n"
    )
    kw = _setup(tmp_path, targets, [_article("a"), _article("b")], monkeypatch)
    save_state(kw["state_path"], {"alive": []})
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert counts["alive"] == 2                    # the working target delivered in full
    assert failed == ["dead"]


def test_state_is_saved_after_each_target(tmp_path, monkeypatch):
    """Process death mid-run must not cost the targets that already finished."""
    articles = [_article("a")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": [], "all": []})

    def send_then_die(article, target, client):
        if target.name == "all":
            raise KeyboardInterrupt("runner evicted")

    monkeypatch.setattr(m.sinks, "send", send_then_die)

    with pytest.raises(KeyboardInterrupt):
        m.run(**kw)

    assert load_state(kw["state_path"])["core"] == ["a"]


def test_dry_run_prints_with_target_prefix_and_persists_nothing(tmp_path, monkeypatch, capsys):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a")], monkeypatch)

    def explode(*a, **k):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(m.sinks, "send", explode)

    m.run(**kw, dry_run=True)

    out = capsys.readouterr().out
    assert "[core]" in out and "[all]" in out
    import os
    assert not os.path.exists(kw["state_path"])


def test_dry_run_validates_the_config(tmp_path, monkeypatch):
    kw = _setup(tmp_path, "targets:\n  - name: x\n    type: nope\n    url: u\n", [], monkeypatch)
    with pytest.raises(ValueError):
        m.run(**kw, dry_run=True)
