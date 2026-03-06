# feeds

LLM-scored RSS feed reader. Fetches articles from RSS feeds into a local SQLite database, scores them by relevance to your research profile using Claude Haiku, and publishes a static HTML page with results.

## Architecture

```
feeds.opml → [fetch] → SQLite DB → [curate] → HTML + Slack notification
```

- **`fetch`** — Parses OPML, fetches all RSS entries, stores in SQLite (deduped by link)
- **`curate`** — Scores uncurated articles with Haiku (1-5), generates static HTML grouped by OPML folder/feed order, sends link to Slack

HTML is served as static files via nginx (see `nginx-feeds.conf`).

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare `feeds.opml`

```bash
cp feeds.example.opml feeds.opml
```

Export your RSS subscriptions as OPML from your feed reader (e.g., Feedly), or edit manually. Feeds are grouped by folder.

### 3. Prepare `research_profile.yaml`

```bash
cp research_profile.example.yaml research_profile.yaml
```

This file is passed directly as LLM context for scoring, so it's free-form — structure it however you like. A good approach is to ask an LLM that already knows your work (e.g., Claude with project memory) to generate it:

> "Based on what you know about my research, generate a research_profile.yaml describing my areas, keywords, and methods."

### 4. Set up `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Edit with your credentials:

- **`anthropic.api_key`** — Anthropic API key
- **`slack.bot_token`** — Slack bot/user token with `chat:write` permission
- **`slack.channel`** — Target Slack channel (e.g., `#journal-feed`)

## Usage

```bash
# Fetch RSS feeds into SQLite
python main.py fetch

# Score new articles, generate HTML, send link to Slack
python main.py curate

# Curate without sending to Slack
python main.py --dry-run curate
```

### Cron (daily at 9am)

```
0 9 * * * cd /path/to/feeds && python main.py fetch && python main.py curate
```

## Output

- `html/{YYYY-MM-DD}.html` — Daily scored feed page
- `html/latest.html` — Symlink to most recent curation
- `html/index.html` — Listing of all curated pages
- `logs/feeds.log` — Rotating log with 90-day retention, includes LLM API costs

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `anthropic.api_key` | (required) | Anthropic API key |
| `anthropic.scoring_model` | `claude-haiku-4-5-20251001` | Model for scoring |
| `slack.bot_token` | (required) | Slack bot or user token |
| `slack.channel` | `#journal-feed` | Target Slack channel |
| `slack.log_channel` | `#log` | Error notification channel |
| `feeds.opml_file` | `feeds.opml` | Path to OPML file |
| `feeds.db` | `feeds.db` | SQLite database path |
| `deploy.base_url` | `https://example.com/feeds` | Public URL base for HTML |

## Tests

```bash
python -m pytest test_main.py -v
```
