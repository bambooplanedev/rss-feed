# AI News RSS Aggregator → Telegram, Discord, Slack, webhooks

Fetches AI-news RSS/Atom feeds and posts each new article as a structured
message to every configured target, twice a day, via GitHub Actions. Dedup state
lives in `state.json`, committed back to the repo each run.

## Sources

Edit `feeds.yaml` — one `{ name, url, tag, tier }` entry per feed. No code change needed.
`tier` is a required integer matching the source's tier: `1` = core news, `2` = labs/primary,
`3` = analysis.

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

- **Each target dedups independently.** A target that is absent from
  `state.json` seeds on its first run and posts nothing, so adding a target
  later starts it from "now" instead of dumping the backlog.
- **Renaming a target makes it a new target** — it re-seeds, and the old key
  stays in `state.json` as an orphan. Delete the old key by hand if it bothers you.
- **Two targets with the same URL deliver everything twice**, and the state
  will look perfectly healthy. The dedup key is the target `name`, not the URL.
- **The state format is one-way.** Rolling back to a single-target version of
  this code requires deleting `state.json` first, or it will repost the entire
  backlog.

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

> **First run** seeds every current feed item as "already seen" and posts
> nothing — this avoids dumping the backlog. Only items published after the
> first run are posted thereafter.

## Local development

```bash
pip install -r requirements-dev.txt
pytest -v                          # run the test suite
python -m aggregator.main --dry-run   # print messages for every target, send nothing
```
