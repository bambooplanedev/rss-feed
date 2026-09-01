import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import aggregator.main as m
from aggregator.models import Article, FeedSource
from aggregator.sinks import PermanentSendError, TargetDeadError
from aggregator.state import load_state, save_state

FEEDS = "feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n"

# run() computes one of these per run and threads it down; collect_articles
# only passes it through, so any fixed instant does here.
CUTOFF = datetime(2026, 6, 1, tzinfo=timezone.utc)


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


def _setup(tmp_path, targets_yaml, articles, monkeypatch, feeds_yaml=FEEDS, ok=("s",)):
    (tmp_path / "feeds.yaml").write_text(feeds_yaml, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(targets_yaml, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles",
                        lambda feeds, client, cutoff: m.CollectResult(articles, [], list(ok)))
    return dict(
        feeds_path=str(tmp_path / "feeds.yaml"),
        targets_path=str(tmp_path / "targets.yaml"),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda _: None,
    )


def _buckets(*names, tags=("s",)):
    """Targets already past the seed, so a test can exercise delivery.
    Replaces _seeded, which returned the old two-field shape."""
    return {n: {"seen": {t: [] for t in tags}} for n in names}


TWO_TARGETS = (
    "targets:\n"
    "  - name: core\n    type: discord\n    url: https://ex.com/core\n    tiers: [1]\n"
    "  - name: all\n    type: slack\n    url: https://ex.com/all\n"
)


def test_migrate_carries_seeded_tags_forward_as_buckets(tmp_path):
    """The thirteen unseeded feeds must stay unseeded. Giving a tag an empty
    bucket means 'seeded, remembers nothing', which makes its whole window new."""
    articles = [_article("s1", tag="s"), _article("n1", tag="new")]
    state = {"tg": {"seen": ["s1", "gone"], "seeded_tags": ["s"]}}

    out = m._migrate(state, articles, {"s", "new"}, {"s", "new"})

    assert out["tg"]["seen"] == {"s": ["s1"]}      # 'new' gets NO key
    assert "new" not in out["tg"]["seen"]


def test_migrate_drops_ids_it_cannot_attribute_when_every_feed_fetched(tmp_path):
    articles = [_article("s1", tag="s")]
    state = {"tg": {"seen": ["s1", "aged-out"], "seeded_tags": ["s"]}}

    out = m._migrate(state, articles, {"s"}, {"s"})

    assert out["tg"]["seen"]["s"] == ["s1"]


def test_migrate_parks_unattributable_ids_with_a_seeded_feed_that_did_not_fetch():
    """Dropping them would make that feed's window look new. Every one was
    already delivered, so an over-inclusive bucket can only suppress something
    already sent — and the feed's next clean fetch prunes it back."""
    articles = [_article("s1", tag="s")]
    state = {"tg": {"seen": ["s1", "down1"], "seeded_tags": ["s", "down"]}}

    out = m._migrate(state, articles, {"s", "down"}, {"s"})     # 'down' not ok

    assert "down1" in out["tg"]["seen"]["down"]


def test_migrate_drops_a_bucket_whose_feed_left_feeds_yaml():
    state = {"tg": {"seen": {"s": ["1"], "gone": ["2"]}}}
    out = m._migrate(state, [], {"s"}, {"s"})
    assert "gone" not in out["tg"]["seen"]


def test_migrate_leaves_an_already_migrated_entry_alone():
    state = {"tg": {"seen": {"s": ["1"]}}}
    assert m._migrate(state, [], {"s"}, {"s"})["tg"]["seen"] == {"s": ["1"]}


def test_new_target_is_seeded_and_sends_nothing(tmp_path, monkeypatch):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a"), _article("b")], monkeypatch)
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert sent == []
    assert counts == {"core": 0, "all": 0}   # ran and delivered nothing, not absent
    assert failed == []
    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["a", "b"]


def test_a_feed_seen_for_the_first_time_is_recorded_and_sends_nothing(tmp_path, monkeypatch):
    articles = [_article("a"), _article("b")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert sent == []
    assert counts["core"] == 0
    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["a", "b"]


def test_a_feed_that_fetched_cleanly_and_published_nothing_gets_an_empty_bucket(tmp_path, monkeypatch):
    """The eugeneyan case. Seeding on clean fetch rather than first delivery is
    what stops its next article being swallowed."""
    kw = _setup(tmp_path, TWO_TARGETS, [], monkeypatch)
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"] == {"s": []}


def test_an_id_the_feed_no_longer_offers_is_pruned(tmp_path, monkeypatch):
    kw = _setup(tmp_path, TWO_TARGETS, [_article("new")], monkeypatch)
    save_state(kw["state_path"], {"core": {"seen": {"s": ["old", "new"]}},
                                  "all": {"seen": {"s": ["old", "new"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["new"]


def test_an_id_the_feed_still_offers_survives_delivery(tmp_path, monkeypatch):
    articles = [_article("keep", 9), _article("fresh", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], {"core": {"seen": {"s": ["keep"]}},
                                  "all": {"seen": {"s": ["keep"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["keep", "fresh"]


def test_an_article_capped_out_of_this_run_does_not_enter_the_bucket(tmp_path, monkeypatch):
    """The test the spec's first revision could not have failed: 25 queued, 20
    sent, and the 5 unsent must still be new next run."""
    articles = [_article(f"a{i}", 9) for i in range(25)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    bucket = load_state(kw["state_path"])["core"]["seen"]["s"]
    assert len(bucket) == m.MAX_SENDS_PER_RUN
    assert "a24" not in bucket


def test_a_target_dead_mid_queue_leaves_the_bucket_untouched(tmp_path, monkeypatch):
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def dead_for_core(article, target, client):
        if target.name == "core":
            raise TargetDeadError("target 'core': HTTP 401 unauthorized")

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == []


def test_a_feed_that_did_not_fetch_cleanly_is_neither_seeded_nor_pruned(tmp_path, monkeypatch):
    """A bozo parse or a failed fetch gives no information about what the feed
    holds, so its bucket must survive the run unchanged."""
    kw = _setup(tmp_path, TWO_TARGETS, [], monkeypatch, ok=())
    save_state(kw["state_path"], {"core": {"seen": {"s": ["old"]}},
                                  "all": {"seen": {"s": ["old"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["old"]


def test_a_feed_offering_nothing_does_not_empty_an_existing_bucket(tmp_path, monkeypatch):
    """Zero offered is indistinguishable from a truncated body. Emptying the
    bucket on that evidence makes the feed's whole window new on recovery."""
    kw = _setup(tmp_path, TWO_TARGETS, [], monkeypatch)
    save_state(kw["state_path"], {"core": {"seen": {"s": ["old"]}},
                                  "all": {"seen": {"s": ["old"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["old"]


def test_a_bozo_feed_that_yields_articles_seeds_instead_of_dumping(tmp_path, monkeypatch):
    """A bozo body is not evidence for pruning, but it is still a first
    sighting. Refusing to seed it delivers the feed's whole window 20 per run."""
    articles = [_article(f"a{i}", 9) for i in range(25)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch, ok=())
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))

    m.run(**kw)

    assert sent == []
    assert load_state(kw["state_path"])["core"]["seen"]["s"] == [a.id for a in articles]


def test_a_feed_offering_only_new_ids_does_not_empty_a_bucket_disjoint_from_them(tmp_path, monkeypatch):
    """A partial-truncation body can serve a subset disjoint from what's
    recorded. That's not evidence the feed's real window excludes the
    recorded ids, so a non-empty bucket must survive even when nothing in it
    intersects `offered`."""
    kw = _setup(tmp_path, TWO_TARGETS, [_article("new", tier=2)], monkeypatch)
    save_state(kw["state_path"], {"core": {"seen": {"s": ["old"]}},
                                  "all": {"seen": {"s": ["old"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["old"]


def test_filters_route_different_articles_to_different_targets(tmp_path, monkeypatch):
    articles = [_article("t1", tier=1), _article("t3", tier=3)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    m.run(**kw)

    assert sorted(sent) == [("all", "t1"), ("all", "t3"), ("core", "t1")]


def test_articles_are_fetched_once_for_all_targets(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_collect(feeds, client, cutoff):
        calls["n"] += 1
        return [_article("a")], [], []

    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", counting_collect)
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)
    state_path = str(tmp_path / "state.json")
    save_state(state_path, _buckets("core", "all"))

    m.run(feeds_path=str(tmp_path / "feeds.yaml"),
          targets_path=str(tmp_path / "targets.yaml"),
          state_path=state_path, sleep=lambda _: None)

    assert calls["n"] == 1


def test_transient_failure_stops_one_target_and_spares_the_others(tmp_path, monkeypatch):
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))
    sent = []

    def flaky(article, target, client):
        if target.name == "core":
            raise RuntimeError("service down")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", flaky)

    counts, failed = m.run(**kw)

    state = load_state(kw["state_path"])
    assert state["core"]["seen"]["s"] == []          # nothing recorded: retry next run
    assert state["all"]["seen"]["s"] == ["a", "b"]   # the other target is unaffected
    assert counts["all"] == 2
    assert "core" in failed


# Asserts exact order on `seen`, so it pins the buffering flush order: pending
# ids flush before the id that just delivered.
def test_permanent_failure_does_not_block_the_queue(tmp_path, monkeypatch):
    """A deleted webhook must not poison every later article for that target."""
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))
    sent = []

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("404")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", picky)

    counts, failed = m.run(**kw)

    assert ("core", "good") in sent                # the queue kept moving
    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["bad", "good"]
    assert counts["core"] == 1                     # 'bad' is recorded but not counted as sent
    assert "core" in failed


def test_dead_target_with_a_short_queue_records_nothing(tmp_path, monkeypatch):
    """The case a per-run counter misses: two articles, both fail, the streak
    never reaches three. Under a counter both ids were burned unrecoverably."""
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def dead_for_core(article, target, client):
        if target.name == "core":
            raise PermanentSendError("400 Bad Request: chat not found")

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == []
    assert counts["core"] == 0
    assert "core" in failed


def test_dead_target_with_a_long_queue_stops_after_the_streak(tmp_path, monkeypatch):
    articles = [_article(str(i), 9) for i in range(10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))
    attempts = []

    def dead_for_core(article, target, client):
        if target.name == "core":
            attempts.append(article.id)
            raise PermanentSendError("400 Bad Request: chat not found")

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert len(attempts) == m.PERMANENT_FAILURE_STREAK      # stopped hammering
    assert load_state(kw["state_path"])["core"]["seen"]["s"] == []   # and burned nothing
    assert "core" in failed


def test_a_bad_article_after_a_success_is_recorded(tmp_path, monkeypatch):
    articles = [_article("good1", 9), _article("bad", 10), _article("good2", 11)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("400 can't parse entities")

    monkeypatch.setattr(m.sinks, "send", picky)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["good1", "bad", "good2"]


def test_a_bad_article_before_any_success_is_committed_once_one_lands(tmp_path, monkeypatch):
    articles = [_article("bad", 9), _article("good", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def picky(article, target, client):
        if target.name == "core" and article.id == "bad":
            raise PermanentSendError("400 can't parse entities")

    monkeypatch.setattr(m.sinks, "send", picky)

    m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["bad", "good"]


def test_streak_log_after_a_delivery_does_not_claim_nothing_was_sent(tmp_path, monkeypatch, caplog):
    """`streak` counts consecutive failures since the last success, not since
    the run started. A target that delivers once and then dies must not log
    a message claiming nothing was delivered, or that the whole queue retries
    when the delivered id is already recorded."""
    articles = [_article(str(i), 9) for i in range(5)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

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
    save_state(kw["state_path"], _buckets("core", "all"))
    sent = []

    def dead_for_core(article, target, client):
        if target.name == "core":
            raise TargetDeadError("target 'core': HTTP 401 unauthorized")
        sent.append((target.name, article.id))

    monkeypatch.setattr(m.sinks, "send", dead_for_core)

    counts, failed = m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == []   # nothing burned
    assert counts["core"] == 0
    assert "core" in failed
    assert sorted(a for t, a in sent if t == "all") == ["a", "b"]  # other target unharmed


def test_target_dead_is_retried_in_full_next_run(tmp_path, monkeypatch):
    """The whole queue must survive, not just the article that hit the 401."""
    articles = [_article("a", 9), _article("b", 10)]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

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
    save_state(kw["state_path"], _buckets("core", "all"))
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
    save_state(kw["state_path"], _buckets("core", "all"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, _ = m.run(**kw)

    assert counts["core"] == m.MAX_SENDS_PER_RUN
    assert len(load_state(kw["state_path"])["core"]["seen"]["s"]) == m.MAX_SENDS_PER_RUN


def test_unset_env_in_one_target_does_not_reduce_the_others(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    targets = (
        "targets:\n"
        "  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n"
        "  - name: alive\n    type: slack\n    url: https://ex.com/alive\n"
    )
    kw = _setup(tmp_path, targets, [_article("a"), _article("b")], monkeypatch)
    save_state(kw["state_path"], _buckets("alive"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    counts, failed = m.run(**kw)

    assert counts["alive"] == 2                    # the working target delivered in full
    assert failed == ["dead"]


def test_state_is_saved_after_each_target(tmp_path, monkeypatch):
    """Process death mid-run must not cost the targets that already finished."""
    articles = [_article("a")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def send_then_die(article, target, client):
        if target.name == "all":
            raise KeyboardInterrupt("runner evicted")

    monkeypatch.setattr(m.sinks, "send", send_then_die)

    with pytest.raises(KeyboardInterrupt):
        m.run(**kw)

    assert load_state(kw["state_path"])["core"]["seen"]["s"] == ["a"]


def test_dry_run_prints_with_target_prefix_and_persists_nothing(tmp_path, monkeypatch, capsys):
    # Already-seeded state, so this article is genuinely new post-seed rather
    # than being absorbed by dry-run's own in-memory seeding.
    kw = _setup(tmp_path, TWO_TARGETS, [_article("a")], monkeypatch)
    save_state(kw["state_path"], _buckets("core", "all"))

    def explode(*a, **k):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(m.sinks, "send", explode)

    m.run(**kw, dry_run=True)

    out = capsys.readouterr().out
    assert "[core]" in out and "[all]" in out
    assert load_state(kw["state_path"]) == _buckets("core", "all")  # untouched


def test_dry_run_seeds_unseeded_feeds_so_the_preview_matches_a_real_run(tmp_path, monkeypatch):
    """A fresh feed must be seeded in memory during a dry run, not left
    unseeded. Otherwise select_new runs against an unseeded bucket and the
    preview reports the MAX_SENDS_PER_RUN ceiling (20) instead of what a real
    run would actually send (1 here: the lone new article from the
    already-seeded feed; the 25 from the fresh feed get seeded, not queued)."""
    articles = ([_article("s-new", tag="s")]
                + [_article(f"n{i}", tag="new") for i in range(25)])
    targets_yaml = "targets:\n  - name: t\n    type: discord\n    url: https://ex.com/t\n"
    kw = _setup(tmp_path, targets_yaml, articles, monkeypatch,
                feeds_yaml=_feeds("s", "new"), ok=("s", "new"))
    save_state(kw["state_path"], {"t": {"seen": {"s": ["old"]}}})
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    dry_sent, dry_failed = m.run(**kw, dry_run=True)
    real_sent, real_failed = m.run(**kw, dry_run=False)

    assert dry_sent["t"] == 1
    assert dry_sent == real_sent
    assert dry_failed == real_failed == []


def test_dry_run_validates_the_config(tmp_path, monkeypatch):
    kw = _setup(tmp_path, "targets:\n  - name: x\n    type: nope\n    url: u\n", [], monkeypatch)
    with pytest.raises(ValueError):
        m.run(**kw, dry_run=True)


def test_a_feed_with_entries_but_no_dated_ones_fails_the_run(monkeypatch):
    """A feed that silently vanishes from the channel must be a red run, not a
    log line — this repo had a feed dead for 29 days that nobody noticed."""
    feeds = [FeedSource(name="undated", url="https://ex.com/f", tag="undated", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed", lambda content, source, cutoff: m.ParseResult([], undated=3, dated=0, bozo=False))

    articles, failed, _ = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert articles == []
    assert "undated" in failed


def test_a_feed_offering_more_than_the_warn_threshold_logs_a_warning(monkeypatch, caplog):
    """The only sensor on MAX_IDS_PER_FEED's real invariant: it must exceed
    what a feed can OFFER, not what it publishes in the window, because
    pruning turns an overflow into a permanent repost loop."""
    articles = [_article(f"a{i}") for i in range(m.OFFER_WARN_THRESHOLD + 1)]
    feeds = [FeedSource(name="busy", url="https://ex.com/f", tag="busy", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source, cutoff: m.ParseResult(articles, undated=0,
                                                               dated=len(articles), bozo=False))

    with caplog.at_level("WARNING", logger="aggregator"):
        m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    [record] = [r for r in caplog.records if "busy" in r.message]
    assert str(m.OFFER_WARN_THRESHOLD + 1) in record.message


def test_a_feed_at_or_below_the_warn_threshold_does_not_warn(monkeypatch, caplog):
    articles = [_article(f"a{i}") for i in range(m.OFFER_WARN_THRESHOLD)]
    feeds = [FeedSource(name="quiet", url="https://ex.com/f", tag="quiet", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source, cutoff: m.ParseResult(articles, undated=0,
                                                               dated=len(articles), bozo=False))

    with caplog.at_level("WARNING", logger="aggregator"):
        m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert caplog.records == []


def test_a_feed_with_no_entries_at_all_does_not_fail_the_run(monkeypatch):
    """A quiet feed is not a broken one. eugeneyan published nothing for 30 days."""
    feeds = [FeedSource(name="quiet", url="https://ex.com/f", tag="quiet", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed", lambda content, source, cutoff: m.ParseResult([], undated=0, dated=0, bozo=False))

    articles, failed, _ = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert failed == []


def test_a_feed_with_old_dated_entries_and_one_undated_is_not_escalated(monkeypatch):
    """Two properly dated entries older than the cutoff plus one hand-rolled
    undated entry is a healthy, quiet feed, not a failed one: escalation must
    key on whether anything got a date at all, not on whether anything
    survived the age cutoff."""
    feeds = [FeedSource(name="quiet", url="https://ex.com/f", tag="quiet", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(
        m, "parse_feed", lambda content, source, cutoff: m.ParseResult([], undated=1, dated=2, bozo=False)
    )

    articles, failed, _ = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert "quiet" not in failed


def test_a_feed_that_is_entirely_undated_is_still_escalated(monkeypatch):
    feeds = [FeedSource(name="broken", url="https://ex.com/f", tag="broken", tier=1)]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(
        m, "parse_feed", lambda content, source, cutoff: m.ParseResult([], undated=3, dated=0, bozo=False)
    )

    articles, failed, _ = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert "broken" in failed


def test_a_clean_feed_is_reported_ok(monkeypatch):
    feeds = [FeedSource(name="clean", url="https://ex.com/f", tag="clean", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source, cutoff: m.ParseResult([_article("a")], 0, 1, False))

    result = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert result.ok == ["clean"]
    assert result.failed == []


def test_a_bozo_feed_is_not_ok_and_not_failed(monkeypatch):
    """It parsed something, so it is not a failure worth going red over. But it
    may be a truncated body, so nothing may be pruned against it."""
    feeds = [FeedSource(name="bz", url="https://ex.com/f", tag="bz", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source, cutoff: m.ParseResult([_article("a")], 0, 1, True))

    result = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert result.ok == []
    assert result.failed == []


def test_a_feed_that_raises_is_neither_ok_nor_silent(monkeypatch):
    feeds = [FeedSource(name="boom", url="https://ex.com/f", tag="boom", tier=1)]

    def blow_up(url, client):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(m, "fetch_feed", blow_up)

    result = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert result.ok == []
    assert "boom" in result.failed


def test_a_fully_undated_feed_is_failed_and_not_ok(monkeypatch):
    feeds = [FeedSource(name="u", url="https://ex.com/f", tag="u", tier=1)]
    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed",
                        lambda content, source, cutoff: m.ParseResult([], 3, 0, False))

    result = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert "u" in result.failed
    assert result.ok == []


def test_feed_failures_reach_runs_failed_list(tmp_path, monkeypatch):
    """The third link in parse -> collect_articles -> run -> main -> exit 1.
    Every other `_setup`-based test hardcodes collect_articles to return no
    feed failures, so this is the only test that would notice `+ feed_failures`
    being dropped from run()'s `failed` list."""
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(TWO_TARGETS, encoding="utf-8")
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client, cutoff: ([], ["undated"], []))
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

    def fake_parse(content, source, cutoff):
        return (
            m.ParseResult(good_articles, undated=0, dated=1, bozo=False)
            if source.name == "good"
            else m.ParseResult([], undated=0, dated=0, bozo=False)
        )

    monkeypatch.setattr(m, "fetch_feed", fake_fetch)
    monkeypatch.setattr(m, "parse_feed", fake_parse)

    articles, failed, _ = m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert articles == good_articles


def test_main_exits_nonzero_when_a_target_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)
    (tmp_path / "feeds.yaml").write_text(FEEDS, encoding="utf-8")
    (tmp_path / "targets.yaml").write_text(
        "targets:\n  - name: dead\n    type: discord\n    url: ${GONE_HOOK}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client, cutoff: ([], [], []))
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
    monkeypatch.setattr(m, "collect_articles", lambda feeds, client, cutoff: ([], [], []))
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

    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")], monkeypatch, feeds,
                ok=("alpha",))
    m.run(**kw)                                    # beta is down: yields nothing
    assert load_state(kw["state_path"])["core"]["seen"] == {"alpha": ["a1"]}

    both = [_article("a1", tag="alpha"), _article("b1", tag="beta"),
            _article("b2", tag="beta")]
    monkeypatch.setattr(m, "collect_articles",
                        lambda feeds, client, cutoff: m.CollectResult(both, [], ["alpha", "beta"]))
    m.run(**kw)                                    # beta recovers

    assert sent == []                              # seeded, not dumped
    assert load_state(kw["state_path"])["core"]["seen"] == {
        "alpha": ["a1"], "beta": ["b1", "b2"],
    }


def test_feed_added_later_seeds_silently(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"), ok=("alpha",))
    m.run(**kw)

    (tmp_path / "feeds.yaml").write_text(_feeds("alpha", "beta"), encoding="utf-8")
    monkeypatch.setattr(
        m, "collect_articles",
        lambda feeds, client, cutoff: m.CollectResult(
            [_article("a1", tag="alpha"), _article("b1", tag="beta")], [], ["alpha", "beta"]),
    )
    m.run(**kw)

    assert sent == []
    assert load_state(kw["state_path"])["core"]["seen"] == {"alpha": ["a1"], "beta": ["b1"]}


def test_narrowing_then_widening_a_filter_delivers_nothing(tmp_path, monkeypatch):
    """Replaces test_filtered_out_feed_does_not_latch_its_tag. Buckets ignore
    the target's filter, so an excluded tag is still seeded and pruned and
    widening later finds nothing new. The old bug was latching with ZERO ids
    recorded; buckets are built from `offered`, not from `matched`."""
    articles = [_article("t2", 9, tier=2, tag="two")]
    kw = _setup(tmp_path, TWO_TARGETS, articles, monkeypatch,
                feeds_yaml=_feeds("s", "two"), ok=("s", "two"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append((t.name, a.id)))

    m.run(**kw)

    # core filters tiers [1], so it never sends the tier-2 article — but its
    # bucket holds it anyway, which is what makes widening later safe.
    assert load_state(kw["state_path"])["core"]["seen"]["two"] == ["t2"]
    assert ("core", "t2") not in sent


def test_widening_a_filter_seeds_instead_of_dumping(tmp_path, monkeypatch):
    articles = [_article("a1", tier=1, tag="alpha"), _article("b1", tier=2, tag="beta")]
    kw = _setup(tmp_path, TIER1_ONLY, articles, monkeypatch, _feeds("alpha", "beta"),
                ok=("alpha", "beta"))
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))
    m.run(**kw)

    (tmp_path / "targets.yaml").write_text(
        TIER1_ONLY.replace("tiers: [1]", "tiers: [1, 2]"), encoding="utf-8"
    )
    m.run(**kw)

    assert sent == []
    assert load_state(kw["state_path"])["core"]["seen"] == {"alpha": ["a1"], "beta": ["b1"]}


def test_legacy_list_state_migrates_and_reposts_nothing(tmp_path, monkeypatch):
    """The bare-list shape predates `seeded_tags` entirely: no key to carry
    forward, so every id is an unattributable orphan with nowhere to park and
    the target migrates to an empty {"seen": {}} — every tag its articles carry
    is then fresh and seeds from `offered` instead of reposting."""
    articles = [_article("a1", tag="alpha"), _article("b1", tag="beta")]
    kw = _setup(tmp_path, ONE_TARGET, articles, monkeypatch, _feeds("alpha", "beta"), ok=())
    (tmp_path / "state.json").write_text(
        json.dumps({"targets": {"core": ["a1", "b1"]}}), encoding="utf-8"
    )
    sent = []
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: sent.append(a.id))

    m.run(**kw)

    assert sent == []
    entry = load_state(kw["state_path"])["core"]
    assert entry["seen"] == {"alpha": ["a1"], "beta": ["b1"]}


def test_orphaned_legacy_target_key_never_serialises_as_null(tmp_path, monkeypatch):
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"), ok=())
    (tmp_path / "state.json").write_text(
        json.dumps({"targets": {"core": ["a1"], "deleted-target": ["z"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    orphan = load_state(kw["state_path"])["deleted-target"]
    assert orphan["seen"] == {}
    assert "null" not in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_seeded_tags_are_pruned_when_a_feed_leaves_feeds_yaml(tmp_path, monkeypatch):
    """Unpruned, a removed-then-re-added feed stays latched while its ids age out
    of `seen` under the cap, and dumps its window on return."""
    kw = _setup(tmp_path, ONE_TARGET, [_article("a1", tag="alpha")],
                monkeypatch, _feeds("alpha"), ok=("alpha",))
    (tmp_path / "state.json").write_text(
        json.dumps({"targets": {
            "core": {"seen": ["a1"], "seeded_tags": ["alpha", "gone"]},
        }}),
        encoding="utf-8",
    )
    monkeypatch.setattr(m.sinks, "send", lambda a, t, c: None)

    m.run(**kw)

    assert "gone" not in load_state(kw["state_path"])["core"]["seen"]


def test_every_feed_in_a_run_is_bounded_by_the_same_cutoff(monkeypatch):
    """parse_feed used to compute its own `now() - MAX_AGE_DAYS`, so a run
    spanning sixteen sequential fetches applied sixteen different cutoffs. An
    article sitting on the 30-day boundary was then admitted or dropped
    depending on how long the preceding feeds took."""
    seen = []
    feeds = [FeedSource(name=t, url=f"https://ex.com/{t}", tag=t, tier=1)
             for t in ("a", "b", "c")]

    monkeypatch.setattr(m, "fetch_feed", lambda url, client: b"content")
    monkeypatch.setattr(m, "parse_feed", lambda content, source, cutoff: (
        seen.append(cutoff) or m.ParseResult([], 0, 1, False)))

    m.collect_articles(feeds, client=None, cutoff=CUTOFF)

    assert seen == [CUTOFF] * 3
