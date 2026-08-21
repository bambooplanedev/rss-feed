from datetime import datetime, timezone

import pytest

import aggregator.main as m
from aggregator.models import Article, FeedSource


def _article(id_, hour):
    return Article(
        id=id_, title=f"T{id_}", url=f"https://ex.com/{id_}", source="S",
        tag="s", tier=1, published=datetime(2026, 7, 2, hour, tzinfo=timezone.utc), summary="",
    )


def test_first_run_seeds_and_posts_nothing(tmp_path, monkeypatch):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n")

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("a", 9), _article("b", 10)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda *a, **k: sent.append(a))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", send_delay=0)

    assert posted == 0
    assert sent == []
    # state now seeded with both ids
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a", "b"}


def test_second_run_posts_only_new_in_time_order(tmp_path, monkeypatch):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n")

    from aggregator.state import save_state
    save_state(state, ["a"])  # 'a' already seen

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("c", 10), _article("b", 9), _article("a", 8)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda text, token, chat_id, client, **k: sent.append(text))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", send_delay=0)

    assert posted == 2
    # oldest-first: b (09:00) before c (10:00)
    assert "T" + "b" in sent[0]
    assert "T" + "c" in sent[1]
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a", "b", "c"}


def test_dry_run_does_not_send_or_persist(tmp_path, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n")
    from aggregator.state import save_state
    save_state(state, ["a"])

    monkeypatch.setattr(m, "collect_articles", lambda feeds, client: [_article("b", 9)])
    sent = []
    monkeypatch.setattr(m, "send_message", lambda *a, **k: sent.append(a))

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                   token="T", chat_id="1", dry_run=True, send_delay=0)

    assert posted == 1
    assert sent == []                       # nothing actually sent
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a"}  # state unchanged
    assert "T" + "b" in capsys.readouterr().out


def test_collect_articles_isolates_failing_feed(monkeypatch):
    good = FeedSource(name="Good", url="https://good/feed", tag="g", tier=1)
    bad = FeedSource(name="Bad", url="https://bad/feed", tag="b", tier=1)

    def fake_fetch(url, client):
        if "bad" in url:
            raise RuntimeError("boom")
        return b"<rss></rss>"

    monkeypatch.setattr(m, "fetch_feed", fake_fetch)
    monkeypatch.setattr(m, "parse_feed", lambda content, source: [_article("x", 9)])

    import httpx
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        out = m.collect_articles([good, bad], client)
    assert len(out) == 1  # bad feed skipped, good feed kept


def test_failed_send_isolated_and_progress_persisted(tmp_path, monkeypatch):
    state = str(tmp_path / "state.json")
    feeds_file = tmp_path / "feeds.yaml"
    feeds_file.write_text("feeds:\n  - name: S\n    url: https://ex.com/feed\n    tag: s\n    tier: 1\n")

    from aggregator.state import save_state
    save_state(state, ["a"])  # 'a' already seen

    monkeypatch.setattr(
        m, "collect_articles",
        lambda feeds, client: [_article("b", 9), _article("c", 10), _article("d", 11)],
    )

    calls = []

    def fake_send(text, token, chat_id, client, **k):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("boom")

    monkeypatch.setattr(m, "send_message", fake_send)

    posted = m.run(feeds_path=str(feeds_file), state_path=state,
                    token="T", chat_id="1", send_delay=0)

    assert posted == 1
    from aggregator.state import load_state
    assert set(load_state(state)) == {"a", "b"}
    assert "c" not in load_state(state)
    assert "d" not in load_state(state)


def test_run_requires_credentials_unless_dry_run():
    with pytest.raises(ValueError):
        m.run(feeds_path="feeds.yaml", state_path="unused", token="", chat_id="", send_delay=0)
