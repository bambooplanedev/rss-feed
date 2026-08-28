# AI News RSS Aggregator → Telegram, Discord, Slack, webhooks

Fetches AI-news RSS/Atom feeds and posts each new article as a structured
message to every configured target, twice a day, via GitHub Actions. Dedup state
lives in `state.json`, committed back to the repo each run.

## Sources

Edit `feeds.yaml` — one `{ name, url, tag, tier }` entry per feed. No code change needed.
`tier` describes what the source *is*, not how good it is: `1` = practice
(hands-on agent/harness engineering, applied LLMs), `2` = primary (a lab or
platform publishing its own work), `3` = research (papers, alignment, analysis).
**Tags must be unique** — seed state is tracked per tag, so two feeds sharing one
would seed together and swallow each other's articles.

Articles published more than `parse.MAX_AGE_DAYS` (30) days ago are dropped at
parse time and never reach a target. A feed's RSS window is not its publication
window — some carry their entire archive — and without this bound an id evicted
from `seen` is re-offered, looks new, and is delivered again.

An entry whose date cannot be parsed at all is dropped too, since nothing can
age-bound it. `_published` first tries feedparser, then RFC 2822, then ISO 8601,
so a non-English day name or a space-separated timestamp is recovered rather
than discarded. A feed that yields entries but **no** parseable dates fails the
run: dropping its articles is right, and doing it quietly is not.

## Targets

Edit `targets.yaml` — one entry per destination. Four types:

| type | required keys | notes |
|---|---|---|
| `telegram` | `token`, `chat_id` | HTML formatting |
| `discord` | `url` | incoming webhook; mentions are suppressed |
| `slack` | `url` | incoming webhook |
| `webhook` | `url` | posts the raw article as JSON |

Optional on any target: `tz` (defaults to `--tz`), and the filters `tiers`,
`tags`, `exclude_tags`. Filters combine with AND; an absent filter does not filter.

`${VAR}` is resolved from the environment at load time.

**Getting a webhook URL:** Discord — channel Settings → Integrations → Webhooks →
New Webhook → Copy Webhook URL. Slack — create a Slack app, enable Incoming
Webhooks, Add New Webhook to Workspace, copy the URL.

> **Webhook URLs are secrets.** Anyone who has one can post to your channel.
> Keep them in GitHub Secrets and reference them as `${VAR}` — never commit the
> literal URL.

Three things worth knowing:

- **Each target dedups independently, per feed.** A target seeds each feed the
  first time that feed delivers it an article, and posts nothing for it. So
  adding a target *or a feed* later starts it from "now" instead of dumping the
  backlog — including when a feed was unreachable during the target's first run,
  or when widening a target's filter brings a new feed into range.
- **Renaming a target makes it a new target** — it re-seeds, and the old key
  stays in `state.json` as an orphan. Delete the old key by hand if it bothers you.
- **Two targets with the same URL deliver everything twice**, and the state
  will look perfectly healthy. The dedup key is the target `name`, not the URL.
- **The state format is one-way, and rolling back is worse than it looks.**
  Older code reads each entry as a flat id list; given the current
  `{"seen": [...], "seeded_tags": [...]}` it iterates the *keys*, dedups against
  nothing, and reposts the entire backlog until it crashes. Rolling back means
  deleting `state.json` first (which also reposts) or converting each entry back
  to its `seen` list by hand. The latter is the one that keeps the channel quiet.

## One-time setup

1. **Create a Telegram bot:** message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow prompts, copy the **bot token**.
2. **Create a channel** (or use an existing one) and **add the bot as an admin**
   with permission to post messages.
3. **Get the channel chat id:** post any message in the channel, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `channel_post.chat.id` (a negative number like `-1001234567890`), or use the
   channel's public `@username`.
4. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. **Keep the repo private** (recommended) — scheduled workflows in *public*
   repos are auto-disabled after 60 days of inactivity.

## Schedule

Runs at **07:17 and 18:17 UTC** (`.github/workflows/aggregate.yml`). Scheduled
runs can be delayed at high load, so treat times as approximate. Use the
**Run workflow** button (workflow_dispatch) to trigger manually.

> **First run** seeds every feed that delivers the target an article (capped at
> `MAX_IDS` ids per target) as "already seen" and posts nothing — this avoids
> dumping the backlog. Seeding is per feed, not per target: a feed that was
> unreachable that run is seeded whenever it next delivers, so an outage during
> the first run costs nothing. Only items published after a feed is seeded are
> posted.

## Local development

```bash
pip install -r requirements-dev.txt
pytest -v                          # run the test suite
python -m aggregator.main --dry-run   # print messages for every target, send nothing
```
