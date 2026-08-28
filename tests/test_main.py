import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import aggregator.main as m
from aggregator.models import Article, FeedSource
from aggregator.sinks import PermanentSendError, TargetDeadError
from aggregator.state import load_state, save_state

FEEDS = "feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n"


def _article(id_, hour=9, tier=1, tag="s"):
    return Article(
        id=id_, title=f"T{id_}", url=f"https://ex.com/{id_}", source="S",
        tag=tag, tier=tier,
        published=datetime(2026, 7, 2, hour, tzinfo=timezone.utc), summary="",
    )


def _feeds(*tags):
    return "feeds:\n" + "".join(
        f"  - name: {t}\n    url: https://ex.com/{t}\n    tag: {t}\n    tier: {i + 1}\n"
        for i, t in enumerate(tags)
    )


def _setup(tmp_path, targets_yaml, articles, monkeypatch, feeds_yaml=FEEDS):
    (tmp_path / "feeds.yaml").write_text(feeds_yaml, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(targets_yaml, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: (articles, [], []))
    return dict(
        feeds_path=str(tmp_path / "feeds.yaml"),
        targets_path=str(tmp_path / "targets.yaml"),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda _: None,
    )


def _seeded(*names, tags=("s",)):
    """Targets already past the seed, so a test can exercise delivery."""
    return {n: {"seen": [], "seeded_tags": list(tags)} for n in names}


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
    assert counts == {"core": 0, "all": 0}   # ran and delivered nothing, not absent
    assert failed == []
    assert set(load_state(kw["state_path"])["core"]["seen"]) == {"a", "b"}


def test_filters_route_different_articles_to_different_targets(tmp_path, monkeypatch):
    articles = [_article("t1", tier=1), _article("t3", tier=3)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    m.run(**kw)

    assert sorted(sent) == [("all", "t1"), ("all", "t3"), ("core", "t1")]


def test_articles_are_fetched_once_for_all_targets(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_collect(feeds, client):
        calls["n"] += 1
        return [_article("a")], [], []

    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", counting_collect)
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)
    state_path = str(tmp_path / "state.json")
    save_state(state_path, _seeded("core", "all"))

    m.run(feeds_path=str(tmp_path / "feeds.yaml"),
          targets_path=str(tmp_path / "targets.yaml"),
          state_path=state_path, sleep=lambda _: None)

    assert calls["n"] == 1


def test_transient_failure_stops_one_target_and_spares_the_others(tmp_path, monkeypatch):
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    sent = []

    def flaky(article, target, client):
        if target.name == "core":
            raise RuntimeError("service down")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", flaky)

    counts, failed = m.run(**kw)

    state = load_state(kw["state_path"])
    assert state["core"]["seen"] == []             # nothing recorded: retry next run
    assert set(state["all"]["seen"]) == {"a", "b"}  # the other target is unaffected
    assert counts["all"] == 2
    assert "core" in failed


# Asserts set equality on `seen`, so it passes unchanged under the buffering
# flush order added below. If it starts failing, the flush order is wrong.
def test_permanent_failure_does_not_block_the_queue(tmp_path, monkeypatch):
    """A deleted webhook must not poison every later article for that target."""
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    sent = []

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("404")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", picky)

    counts, failed = m.run(**kw)

    assert ("core", "good") in sent                # the queue kept moving
    assert set(load_state(kw["state_path"])["core"]["seen"]) == {"bad", "good"}
    assert counts["core"] == 1                     # 'bad' is recorded but not counted as sent
    assert "core" in failed


def test_dead_target_with_a_short_queue_records_nothing(tmp_path, monkeypatch):
    """The case a per-run counter misses: two articles, both fail, the streak
    never reaches three. Under a counter both ids were burned unrecoverably."""
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    def dead_for_core(article, target, client):
        if target.name == "core":
            raise PermanentSendError("400 Bad Request: chat not found")

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == []
    assert counts["core"] == 0
    assert "core" in failed


def test_dead_target_with_a_long_queue_stops_after_the_streak(tmp_path, monkeypatch):
    articles = [_article(str(i), 9) for i in range(10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    attempts = []

    def dead_for_core(article, target, client):
        if target.name == "core":
            attempts.append(article.id)
            raise PermanentSendError("400 Bad Request: chat not found")

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert len(attempts) == m.PERMANENT_FAILURE_STREAK      # stopped hammering
    assert load_state(kw["state_path"])["core"]["seen"] == []   # and burned nothing
    assert "core" in failed


def test_a_bad_article_after_a_success_is_recorded(tmp_path, monkeypatch):
    articles = [_article("good1", 9), _article("bad", 10), _article("good2", 11)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("400 can't parse entities")

    monkeypatch.setattr(m.sinks, "send", picky)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == ["good1", "bad", "good2"]


def test_a_bad_article_before_any_success_is_committed_once_one_lands(tmp_path, monkeypatch):
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("400 can't parse entities")

    monkeypatch.setattr(m.sinks, "send", picky)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == ["bad", "good"]


def test_streak_log_after_a_delivery_does_not_claim_nothing_was_sent(tmp_path, monkeypatch, caplog):
    """`streak` counts consecutive failures since the last success, not since
    the run started. A target that delivers once and then dies must not log
    a message claiming nothing was delivered, or that the whole queue retries
    when the delivered id is already recorded."""
    articles = [_article(str(i), 9) for i in range(5)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    def deliver_one_then_die(article, target, client):
        if target.name == "core" and article.id != "0":
            raise PermanentSendError("400 Bad Request: chat not found")

    monkeypatch.setattr(m.sinks, "send", deliver_one_then_die)

    with caplog.at_level("ERROR", logger="aggregator"):
        m.run(**kw)

    unreachable = [r for r in caplog.records if "treating as unreachable" in r.getMessage()]
    assert unreachable, "operator is never told the target was cut off"
    message = unreachable[0].getMessage()
    assert "nothing delivered" not in message
    assert "whole queue" not in message


def test_target_dead_stops_the_target_without_recording(tmp_path, monkeypatch):
    """A revoked token must not mark the whole queue delivered."""
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    sent = []

    def dead_for_core(article, target, client):
        if target.name == "core":
            raise TargetDeadError("target 'core': HTTP 401 unauthorized")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == []   # nothing burned
    assert counts["core"] == 0
    assert "core" in failed
    assert sorted(a for t, a in sent if t == "all") == ["a", "b"]  # other target unharmed


def test_target_dead_is_retried_in_full_next_run(tmp_path, monkeypatch):
    """The whole queue must survive, not just the article that hit the 401."""
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    monkeypatch.setattr(
        m.sinks, "send",
        lambda a, t, c: (_ for _ in ()).throw(TargetDeadError("HTTP 401")),
    )
    m.run(**kw)

    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))
    m.run(**kw)

    assert sorted(a for t, a in sent if t == "core") == ["a", "b"]


def test_target_dead_is_logged_as_unreachable_not_as_transient(tmp_path, monkeypatch, caplog):
    """A revoked token never heals on its own; telling the operator it retries
    next run buys a week of silent non-delivery."""
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a")], monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    monkeypatch.setattr(
        m.sinks, "send",
        lambda a, t, c: (_ for _ in ()).throw(TargetDeadError("target 'core': HTTP 401 nope")),
    )

    with caplog.at_level("WARNING", logger="aggregator"):
        m.run(**kw)

    dead = [r for r in caplog.records if "unreachable" in r.getMessage()]
    assert dead, "operator is never told the target is dead"
    assert dead[0].levelname == "ERROR"
    assert not [r for r in caplog.records if "rest next run" in r.getMessage()]


def test_send_cap_leaves_the_remainder_for_the_next_run(tmp_path, monkeypatch):
    articles = [_article(f"a{i}", hour=9) for i in range(m.MAX_SENDS_PER_RUN + 5)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, _ = m.run(**kw)

    assert counts["core"] == m.MAX_SENDS_PER_RUN
    assert len(load_state(kw["state_path"])["core"]["seen"]) == m.MAX_SENDS_PER_RUN


def test_unset_env_in_one_target_does_not_reduce_the_others(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    targets = (
        "targets:\n"
        "  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n"
        "  - name: alive\n    type: slack\n    url: https://ex.com/alive\n"
    )
    kw = _setup(tmp_path, targets, [_article("a"), _article("b")], monkeypatch)
    save_state(kw["state_path"], _seeded("alive"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert counts["alive"] == 2                    # the working target delivered in full
    assert failed == ["dead"]


def test_state_is_saved_after_each_target(tmp_path, monkeypatch):
    """Process death mid-run must not cost the targets that already finished."""
    articles = [_article("a")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _seeded("core", "all"))

    def send_then_die(article, target, client):
        if target.name == "all":
            raise KeyboardInterrupt("runner evicted")

    monkeypatch.setattr(m.sinks, "send", send_then_die)

    with pytest.raises(KeyboardInterrupt):
        m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == ["a"]


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


def test_a_feed_with_entries_but_no_dated_ones_fails_the_run(monkeypatch):
    """A feed that silently vanishes from the channel must be a red run, not a
    log line — this repo had a feed dead for 29 days that nobody noticed."""
    feeds = [FeedSource(name="undated", url="https://ex.com/f", tag="undated", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed", lambda content, source: m.ParseResult([], undated=3, dated=0, bozo=False))

    articles, failed, _ = m.collect_articles(feeds, client=None)

    assert articles == []
    assert "undated" in failed


def test_a_feed_with_no_entries_at_all_does_not_fail_the_run(monkeypatch):
    """A quiet feed is not a broken one. eugeneyan published nothing for 30 days."""
    feeds = [FeedSource(name="quiet", url="https://ex.com/f", tag="quiet", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed", lambda content, source: m.ParseResult([], undated=0, dated=0, bozo=False))

    articles, failed, _ = m.collect_articles(feeds, client=None)

    assert failed == []


def test_a_feed_with_old_dated_entries_and_one_undated_is_not_escalated(monkeypatch):
    """Two properly dated entries older than the cutoff plus one hand-rolled
    undated entry is a healthy, quiet feed, not a failed one: escalation must
    key on whether anything got a date at all, not on whether anything
    survived the age cutoff."""
    feeds = [FeedSource(name="quiet", url="https://ex.com/f", tag="quiet", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(
        m, "parse_feed", lambda content, source: m.ParseResult([], undated=1, dated=2, bozo=False)
    )

    articles, failed, _ = m.collect_articles(feeds, client=None)

    assert "quiet" not in failed


def test_a_feed_that_is_entirely_undated_is_still_escalated(monkeypatch):
    feeds = [FeedSource(name="broken", url="https://ex.com/f", tag="broken", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(
        m, "parse_feed", lambda content, source: m.ParseResult([], undated=3, dated=0, bozo=False)
    )

    articles, failed, _ = m.collect_articles(feeds, client=None)

    assert "broken" in failed


def test_a_clean_feed_is_reported_ok(monkeypatch):
    feeds = [FeedSource(name="clean", url="https://ex.com/f", tag="clean", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source: m.ParseResult([_article("a")], 0, 1, False))

    result = m.collect_articles(feeds, client=None)

    assert result.ok == ["clean"]
    assert result.failed == []


def test_a_bozo_feed_is_not_ok_and_not_failed(monkeypatch):
    """It parsed something, so it is not a failure worth going red over. But it
    may be a truncated body, so nothing may be pruned against it."""
    feeds = [FeedSource(name="bz", url="https://ex.com/f", tag="bz", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source: m.ParseResult([_article("a")], 0, 1, True))

    result = m.collect_articles(feeds, client=None)

    assert result.ok == []
    assert result.failed == []


def test_a_feed_that_raises_is_neither_ok_nor_silent(monkeypatch):
    feeds = [FeedSource(name="boom", url="https://ex.com/f", tag="boom", tier=1)]

    def blow_up(url, client):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(m, "fetch_feed", blow_up)

    result = m.collect_articles(feeds, client=None)

    assert result.ok == []
    assert "boom" in result.failed


def test_a_fully_undated_feed_is_failed_and_not_ok(monkeypatch):
    feeds = [FeedSource(name="u", url="https://ex.com/f", tag="u", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source: m.ParseResult([], 3, 0, False))

    result = m.collect_articles(feeds, client=None)

    assert "u" in result.failed
    assert result.ok == []


def test_feed_failures_reach_runs_failed_list(tmp_path, monkeypatch):
    """The third link in parse -> collect_articles -> run -> main -> exit 1.
    Every other `_setup`-based test hardcodes collect_articles to return no
    feed failures, so this is the only test that would notice `+ feed_failures`
    being dropped from run()'s `failed` list."""
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: ([], ["undated"], []))
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    _, failed = m.run(
        feeds_path=str(tmp_path / "feeds.yaml"),
        targets_path=str(tmp_path / "targets.yaml"),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda _: None,
    )

    assert "undated" in failed


def test_collect_articles_isolates_failing_feed(monkeypatch):
    feeds = [
        FeedSource(name="bad", url="https://ex.com/bad", tag="s", tier=1),
        FeedSource(name="good", url="https://ex.com/good", tag="s", tier=1),
    ]
    good_articles = [_article("g1")]

    def fake_fetch(url, client):
        if url == "https://ex.com/bad":
            raise RuntimeError("connection reset")
        return b"content"

    def fake_parse(content, source):
        return (
            m.ParseResult(good_articles, undated=0, dated=1, bozo=False)
            if source.name == "good"
            else m.ParseResult([], undated=0, dated=0, bozo=False)
        )

    monkeypatch.setattr(m, "fetch_feed", fake_fetch)
    monkeypatch.setattr(m, "parse_feed", fake_parse)

    articles, failed, _ = m.collect_articles(feeds, client=None)

    assert articles == good_articles


def test_main_exits_nonzero_when_a_target_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(
        "targets:\n  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: ([], [], []))
    monkeypatch.setattr(
        "sys.argv",
        [
            "aggregator",
            "--feeds", str(tmp_path / "feeds.yaml"),
            "--targets", str(tmp_path / "targets.yaml"),
            "--state", str(tmp_path / "state.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        m.main()

    assert exc_info.value.code == 1


def test_main_does_not_exit_on_a_clean_run(tmp_path, monkeypatch):
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: ([], [], []))
    monkeypatch.setattr(
        "sys.argv",
        [
            "aggregator",
            "--feeds", str(tmp_path / "feeds.yaml"),
            "--targets", str(tmp_path / "targets.yaml"),
            "--state", str(tmp_path / "state.json"),
        ],
    )

    m.main()  # must not raise SystemExit


ONE_TARGET = "targets:\n  - name: core\n    type: slack\n    url: https://ex.com/core\n"
TIER1_ONLY = (
    "targets:\n  - name: core\n    type: slack\n"
    "    url: https://ex.com/core\n    tiers: [1]\n"
)


def test_feed_that_yielded_nothing_is_seeded_on_a_later_run(tmp_path, monkeypatch):
    """The seed must be per-feed: a feed down during the first run is seeded when
    it recovers, not dumped 20-per-run."""
    feeds = _feeds("alpha", "beta")
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))

    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")], monkeypatch, feeds)
    m.run(**kw)                                    # beta is down: yields nothing
    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha"]

    both = [_article("a1", tag="alpha"), _article("b1", tag="beta"),
            _article("b2", tag="beta")]
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: (both, [], []))
    m.run(**kw)                                    # beta recovers

    assert sent == []                              # seeded, not dumped
    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha", "beta"]


def test_feed_added_later_seeds_silently(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"))
    m.run(**kw)

    (tmp_path / "feeds.yaml").write_text(_feeds("alpha", "beta"), encoding="utf-8")
    monkeypatch.setattr(
        m, "collect_articles",
        lambda feeds, client: ([_article("a1", tag="alpha"), _article("b1", tag="beta")], [], []),
    )
    m.run(**kw)

    assert sent == []
    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha", "beta"]


def test_filtered_out_feed_does_not_latch_its_tag(tmp_path, monkeypatch):
    """Regression: seeding keyed on global feed health latched a filtered-out
    tag with zero recorded ids, and could never re-seed it."""
    articles = [_article("a1", tier=1, tag="alpha"), _article("b1", tier=2, tag="beta")]
    kw = _setup(tmp_path, TIER1_ONLY, articles, monkeypatch, _feeds("alpha", "beta"))
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha"]


def test_widening_a_filter_seeds_instead_of_dumping(tmp_path, monkeypatch):
    articles = [_article("a1", tier=1, tag="alpha"), _article("b1", tier=2, tag="beta")]
    kw = _setup(tmp_path, TIER1_ONLY, articles, monkeypatch, _feeds("alpha", "beta"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))
    m.run(**kw)

    (tmp_path / "targets.yaml").write_text(
        TIER1_ONLY.replace("tiers: [1]", "tiers: [1, 2]"), encoding="utf-8"
    )
    m.run(**kw)

    assert sent == []
    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha", "beta"]


def test_legacy_list_state_migrates_and_reposts_nothing(tmp_path, monkeypatch):
    articles = [_article("a1", tag="alpha"), _article("b1", tag="beta")]
    kw = _setup(tmp_path, ONE_TARGET, articles, monkeypatch, _feeds("alpha", "beta"))
    (tmp_path / "state.json").write_text(
        json.dumps({"targets": {"core": ["a1", "b1"]}}), encoding="utf-8"
    )
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))

    m.run(**kw)

    assert sent == []
    entry = load_state(kw["state_path"])["core"]
    assert entry["seen"] == ["a1", "b1"]
    assert entry["seeded_tags"] == ["alpha", "beta"]


def test_orphaned_legacy_target_key_never_serialises_as_null(tmp_path, monkeypatch):
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"))
    (tmp_path / "state.json").write_text(
        json.dumps({"targets": {"core": ["a1"], "deleted-target": ["z"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    orphan = load_state(kw["state_path"])["deleted-target"]
    assert orphan["seeded_tags"] == ["alpha"]
    assert "null" not in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_seeded_tags_are_pruned_when_a_feed_leaves_feeds_yaml(tmp_path, monkeypatch):
    """Unpruned, a removed-then-re-added feed stays latched while its ids age out
    of `seen` under the cap, and dumps its window on return."""
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"))
    save_state(kw["state_path"], {"core": {"seen": ["a1"], "seeded_tags": ["alpha", "gone"]}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seeded_tags"] == ["alpha"]
