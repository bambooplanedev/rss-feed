# AI News RSS Aggregator → Telegram — Design

**Date:** 2026-07-05
**Status:** Approved (design phase)

## 1. Purpose

Aggregate AI-news articles from a curated list of RSS/Atom feeds and post each
new article as a well-structured message to a Telegram channel, on a scheduled
cadence, running always-on with no server to manage.

The Telegram channel is the single output and acts as a durable, machine-readable
log. A future agent (out of scope here) will consume this channel, judge each
post against user-defined rules, and append approved items to a separate
"Bank of Themes" git repo. **That agent and the Bank of Themes are explicitly
deferred** — this project only produces the well-structured Telegram feed they
will later read.

## 2. Scope

**In scope (build now):**
- Curated, verified source list (see §4).
- Aggregator: fetch & parse feeds → dedup against state → post new items to Telegram.
- One structured message per article.
- Scheduled, always-on execution via GitHub Actions.
- Persistent dedup state committed back to the repo.

**Out of scope (deferred, design seam only):**
- The decision/filtering agent (user's "rules prism").
- The Bank-of-Themes git repo.
- Any reading-back of the Telegram channel.

The only obligation this project has toward the deferred work is the
**message format contract** (§7): each Telegram post carries the machine-readable
essentials a future consumer needs (title, source, timestamp, canonical URL).

## 3. Architecture

Linear pipeline, run on a schedule:

```
feeds.yaml (source list)
      │
      ▼
[1] Fetch & parse each feed  ──► normalized Article {id, title, url, source, tag, published, summary}
      │
      ▼
[2] Dedup against state.json  (drop already-seen items by id)
      │
      ▼
[3] Post each new Article → Telegram (one message per article, oldest→newest)
      │
      ▼
[4] Update state.json with newly-posted ids  →  commit back to repo
```

Two systems will eventually share **only the Telegram channel**; the aggregator
has no knowledge of the future agent.

## 4. Source list (verified live 2026-07-05)

Posting is unfiltered until the agent exists, so **source selection is the volume
control**. Selected set targets ~10–15 posts/day.

**Tier 1 — Core news**
- MIT Technology Review AI — `https://www.technologyreview.com/topic/artificial-intelligence/feed`
- Ars Technica AI — `https://arstechnica.com/ai/feed/`
- The Verge AI — `https://www.theverge.com/rss/ai-artificial-intelligence/index.xml`
- TechCrunch AI — `https://techcrunch.com/category/artificial-intelligence/feed/`
- Wired AI — `https://www.wired.com/feed/tag/ai/latest/rss`
- AI News — `https://www.artificialintelligence-news.com/feed/`
- Towards Data Science — `https://towardsdatascience.com/feed`

**Tier 2 — Primary sources / labs**
- OpenAI news — `https://openai.com/news/rss.xml`
- Google DeepMind blog — `https://deepmind.google/blog/rss.xml`
- Google AI blog — `https://blog.google/technology/ai/rss/`

**Tier 3 — Analysis & newsletters**
- Import AI (Jack Clark) — `https://jack-clark.net/feed/`
- Simon Willison — `https://simonwillison.net/atom/everything/`
- The Gradient — `https://thegradient.pub/rss/`
- Berkeley BAIR — `https://bair.berkeley.edu/blog/feed.xml`

**Deferred (Tier 4 firehoses, not included):** Hugging Face blog, MarkTechPost,
arXiv cs.AI, Hacker News "AI" keyword — high volume/noise; add once the filtering
agent can tame them.

**Excluded (dead / no feed):** The Batch (no official feed), Anthropic (no feed),
Synced Review (defunct), VentureBeat AI category (dormant).

Notes carried forward for implementation:
- The Verge / Ars Technica / Wired hosts may block some fetchers; use a normal
  User-Agent header. Confirmed reachable via standard HTTP.
- DeepMind feed is metadata-heavy with thin descriptions — summary may be short.

## 5. Stack

- **Python 3.13**
- `feedparser==6.0.12` — RSS/Atom parsing (handles both formats + malformed feeds).
  Note: feedparser does **not** fetch; we fetch bytes with httpx and hand the
  content to feedparser.
- `httpx==0.28.1` — feed fetching + Telegram Bot API calls (explicit timeouts).
- `PyYAML` — read `feeds.yaml`.
- Standard-library **`zoneinfo`** for timezone-aware timestamp formatting (§7) —
  no third-party tz dependency.
- Dependencies pinned in **`requirements.txt`** for reproducible CI runs (enables
  `actions/setup-python` pip caching, §9).
- No database; state is a JSON file in the repo (§6).

## 6. State & deduplication

- **`state.json`** in the repo: a set of seen article ids (plus a small amount of
  metadata such as last-run timestamp).
- **Dedup id** = feed entry `guid`/`id`; fallback to a **normalized URL**
  (strip tracking query params, trailing slash, scheme-normalize) when no guid.
- **Malformed feeds:** feedparser sets a `bozo` flag instead of raising. Log the
  `bozo` reason but **still use whatever entries parsed** — do not discard the
  whole feed on a bozo bit.
- **First run** seeds every currently-present item as "seen" and **posts nothing**,
  preventing a backlog dump. Subsequent runs post only ids not in state.
- After a successful run, newly-posted ids are added and `state.json` is
  **committed back to the repo** by the workflow.
- Optional hygiene: cap `state.json` size by retaining ids from, e.g., the last
  60 days.

## 7. Message format (the contract)

**One structured message per article**, posted oldest→newest within a run.
Telegram HTML parse mode:

```
🔹 <b>{title}</b>
{source} · {published as HH:MM, DD Mon}

{summary — plain text, first ~300 chars, ellipsized}

{canonical_url}
#{source_tag}
```

- `canonical_url` appears on its own line (reliable link + easy to parse).
- `#{source_tag}` is a stable per-source hashtag (e.g. `#techcrunch`) for grouping.
- Fields are consistently placed so a future agent can parse posts deterministically.
- Titles/summaries HTML-escaped; summary stripped of HTML tags.

## 8. Configuration

- **`feeds.yaml`** — list of `{ name, url, tag, tier }`. Adding/removing a source
  is a one-line edit, no code change.
- **`tier`** is an integer (1/2/3) recording the source's editorial tier from §4
  (1 = core news, 2 = labs/primary, 3 = analysis/newsletters). It is carried on
  `FeedSource` so a future consumer (e.g. the filtering agent) can weight or
  group by source importance; it does not affect current output.
- Cadence and schedule live in the GitHub Actions workflow (§9).

## 9. Hosting & scheduling — GitHub Actions

- **Schedule:** cron at **07:17 and 18:17 UTC**. GitHub Actions `schedule:` cron
  is **always UTC — it does NOT accept a timezone**, so times are expressed
  directly in UTC (no conversion, no DST handling).
  - **Off-peak minutes on purpose:** scheduled runs can be delayed at high load,
    *especially at the top of every hour*, and over-loaded jobs may be dropped.
    Avoid `:00`; treat delivery time as **approximate**, not punctual.
- **Steps:** `actions/checkout@v6` → `actions/setup-python@v6` (`cache: 'pip'`)
  → `pip install -r requirements.txt` → run aggregator → commit updated
  `state.json` back to the repo.
- **Commit-back permissions:** workflow sets `permissions: contents: write` and
  relies on checkout's default `persist-credentials: true`. Pushes made with the
  default `GITHUB_TOKEN` **do not trigger another workflow run**, so committing
  `state.json` each run cannot cause a recursive loop.
- **`concurrency` group** (e.g. `group: aggregator`, `cancel-in-progress: false`)
  serializes runs so two overlapping runs never race on committing `state.json`.
- **Private repo recommended:** the 60-day-inactivity auto-disable of scheduled
  workflows applies to **public** repos only. A private repo sidesteps it
  entirely (our per-run `state.json` commit would also keep a public repo alive,
  but only while items keep flowing).
- **Always-on, free, no server.** Later the agent/Bank-of-Themes can live in the
  same or a sibling repo.

## 10. Secrets

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` stored as **GitHub Actions secrets**.
- Bot created via @BotFather; bot added as admin of the target channel so it can post.
- No secrets committed to the repo.

## 11. Error handling

- **Per-feed isolation:** each feed fetch/parse is wrapped in try/except. A dead,
  slow, or malformed feed logs a warning and the run continues with the others.
- **Telegram send failures:** retry with backoff; if a message ultimately fails,
  its id is **not** marked seen, so it retries next run (accept possible rare dup
  over silent loss).
- **Telegram rate limits (single channel ~20 messages/minute):** because we post
  one message per article, insert a **~3–4s delay between sends**, and on an HTTP
  `429` honor the `retry_after` value from the response as authoritative before
  retrying. Message body kept well under Telegram's 4096-char limit via the
  ~300-char summary truncation (§7).
- **Fetch etiquette:** explicit timeouts on all HTTP calls; a normal browser-like
  `User-Agent` header (some sources — Verge/Ars/Wired — block default clients).
- Optional: on repeated failure of a specific feed, include a brief notice.

## 12. Testing

- **Unit:** normalization (guid/url dedup key), summary truncation + HTML
  stripping, message formatting, state read/write, first-run seeding logic.
- **Feed parsing:** run against saved sample feed fixtures (RSS 2.0 + Atom +
  a malformed sample) — no network in unit tests.
- **Telegram:** mock the Bot API; assert payload shape and parse mode.
- **Dry-run mode:** a flag that prints messages to stdout instead of sending,
  for safe end-to-end checks locally.

## 13. Future seam (recorded, not built)

- The Telegram channel is the integration point. Because every post is one
  article with a consistent, machine-readable layout (§7), the future agent can
  read the channel (bot-admin `channel_post` updates, or MTProto history),
  evaluate each post against the user's rules, and append approved items to the
  Bank-of-Themes git repo — **without any change to this aggregator.**
