# feeds

LLM-based RSS feed recommender. Reads RSS feeds, uses Claude to filter articles relevant to your research profile, and posts recommendations to Slack.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare `feeds.opml`

Export your RSS subscriptions as an OPML file from your feed reader (e.g., Feedly, Inoreader), or create one manually. Example structure:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>My Feeds</title></head>
  <body>
    <outline text="Journals" title="Journals">
      <outline type="rss" text="Nature" title="Nature"
        xmlUrl="http://www.nature.com/nature/current_issue/rss" />
      <outline type="rss" text="PRL" title="PRL"
        xmlUrl="http://feeds.aps.org/rss/recent/prl.xml" />
    </outline>
  </body>
</opml>
```

### 3. Prepare `research_profile.yaml`

This file describes your research interests so the LLM can match relevant articles. See `research_profile.yaml` in this repo for the full schema.

**Tip:** Use an AI assistant (e.g., Claude) with access to your publication history or memories to generate this file. For example, ask Claude:

> "Based on what you know about my research, generate a `research_profile.yaml` with my research areas, keywords, and methods."

This works especially well if you've been using Claude with project memory or have shared your CV/papers in a conversation.

### 4. Set up `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your credentials:

- **`anthropic.api_key`** - Your Anthropic API key
- **`slack.bot_token`** - Slack bot token (`xoxb-...`) or user token (`xoxp-...`)
- **`slack.channel`** - Target Slack channel (e.g., `#journal-feed`)

The bot/user must have `chat:write` permission and be a member of the target channel.

## Usage

```bash
# Dry run (prints to terminal, no Slack)
python main.py --dry-run

# Send to Slack
python main.py

# Override lookback window (default: 1 day)
python main.py --days 3
```

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `anthropic.api_key` | (required) | Anthropic API key |
| `anthropic.model` | `claude-sonnet-4-6` | Claude model to use |
| `anthropic.max_tokens` | `8192` | Max response tokens |
| `slack.bot_token` | (required) | Slack bot or user token |
| `slack.channel` | `#journal-feed` | Target Slack channel |
| `slack.max_message_length` | `4000` | Split messages at this length |
| `feeds.opml_file` | `feeds.opml` | Path to OPML file |
| `feeds.max_articles_per_feed` | `20` | Max articles fetched per feed |
| `feeds.max_summary_length` | `500` | Truncate article summaries |
| `feeds.days_lookback` | `1` | Only include articles from last N days |
