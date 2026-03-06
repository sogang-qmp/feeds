#!/usr/bin/env python3
"""RSS Feed Reader with LLM-based recommendation and Slack notification."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
from collections import OrderedDict
import json
import sys
import time
import traceback

import anthropic
import feedparser
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def load_research_profile(path="research_profile.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def parse_opml(path):
    """Parse OPML file and return list of feed URLs with titles."""
    tree = ET.parse(path)
    feeds = []
    for outline in tree.iter("outline"):
        url = outline.get("xmlUrl")
        if url:
            feeds.append({
                "title": outline.get("title") or outline.get("text") or url,
                "url": url,
            })
    return feeds


def fetch_articles(feeds, config, max_per_feed=20, days_lookback=1):
    """Fetch recent articles from all feeds."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_lookback)
    articles = []

    for feed_info in feeds:
        print(f"  Fetching: {feed_info['title']}...")
        try:
            parsed = feedparser.parse(feed_info["url"])
        except Exception as e:
            print(f"    Error fetching {feed_info['title']}: {e}")
            continue

        count = 0
        for entry in parsed.entries:
            if count >= max_per_feed:
                break

            published = None
            for date_field in ("published_parsed", "updated_parsed"):
                t = getattr(entry, date_field, None)
                if t:
                    published = datetime(*t[:6], tzinfo=timezone.utc)
                    break

            if published and published < cutoff:
                continue

            summary = getattr(entry, "summary", "") or ""
            max_summary = config.get("feeds", {}).get("max_summary_length", 500)
            if len(summary) > max_summary:
                summary = summary[:max_summary] + "..."

            # Extract authors
            authors = ""
            if hasattr(entry, "authors") and entry.authors:
                authors = ", ".join(a.get("name", "") for a in entry.authors if a.get("name"))
            elif hasattr(entry, "author"):
                authors = entry.author or ""

            articles.append({
                "feed": feed_info["title"],
                "title": getattr(entry, "title", "No title"),
                "link": getattr(entry, "link", ""),
                "summary": summary,
                "authors": authors,
                "published": published.isoformat() if published else "unknown",
            })
            count += 1

    return articles


def recommend_articles(articles, profile, config):
    """Use Claude to score and recommend articles based on research profile."""
    if not articles:
        print("No articles to recommend.")
        return []

    api_cfg = config["anthropic"]

    # Build profile text from hierarchical structure
    researcher = profile.get("researcher", {})
    areas = profile.get("research_areas", {})
    keywords = profile.get("keywords", {})

    def flatten(obj):
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            items = []
            for v in obj.values():
                items.extend(flatten(v))
            return items
        return [str(obj)]

    profile_text = (
        f"Name: {researcher.get('name', 'N/A')}\n"
        f"Position: {researcher.get('position', 'N/A')}\n"
        f"Affiliation: {researcher.get('affiliation', 'N/A')}\n"
        f"Research areas: {', '.join(flatten(areas))}\n"
        f"Keywords: {', '.join(flatten(keywords))}\n"
    )
    scoring_prompt = profile.get("scoring_prompt", "")
    if scoring_prompt:
        profile_text += f"\nScoring guidance:\n{scoring_prompt}"

    articles_text = ""
    for i, a in enumerate(articles):
        articles_text += (
            f"\n[{i}] Feed: {a['feed']}\n"
            f"    Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Summary: {a['summary']}\n"
            f"    Link: {a['link']}\n"
            f"    Published: {a['published']}\n"
        )

    prompt = f"""You are a research assistant. Given a researcher's profile and a list of recent articles from RSS feeds, select ALL relevant articles for this researcher. Be generous — include any article with reasonable relevance.

## Researcher Profile
{profile_text}

## Articles
{articles_text}

## Instructions
Return a JSON array of recommended articles. Each element should have:
- "index": the article index number from the list above
- "title": the article title
- "link": the article URL
- "feed": the source feed name
- "authors": the article authors

Rank by relevance (most relevant first). Include all articles that have any reasonable connection to the researcher's profile.

Return ONLY the JSON array, no other text."""

    client = anthropic.Anthropic(api_key=api_cfg["api_key"])

    print(f"  Asking Claude to recommend from {len(articles)} articles...")
    message = client.messages.create(
        model=api_cfg.get("model", "claude-sonnet-4-6"),
        max_tokens=api_cfg.get("max_tokens", 8192),
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    # Strip markdown code fences if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0].strip()

    recommendations = json.loads(response_text)
    return recommendations


def _split_message(text, max_len):
    """Split text into chunks at line boundaries."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


def _group_by_feed(recommendations):
    grouped = OrderedDict()
    for rec in recommendations:
        feed = rec.get("feed", "Unknown")
        grouped.setdefault(feed, []).append(rec)
    return grouped


def format_slack_message(recommendations):
    """Format recommendations as mrkdwn text grouped by feed."""
    if not recommendations:
        return "No relevant articles found today."

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"*Recommended Papers - {today}*\n"]

    grouped = _group_by_feed(recommendations)
    for feed_name, recs in grouped.items():
        lines.append(f"*[{feed_name}]*")
        for rec in recs:
            authors = rec.get("authors", "")
            lines.append(f"  *<{rec['link']}|{rec['title']}>*")
            if authors:
                lines.append(f"  {authors}")
        lines.append("")

    return "\n".join(lines)


def send_error_to_slack(config, error_msg):
    """Send error notification to Slack #log channel."""
    try:
        slack_cfg = config["slack"]
        client = WebClient(token=slack_cfg["bot_token"])
        log_channel = slack_cfg.get("log_channel", "#log")
        client.chat_postMessage(
            channel=log_channel,
            text=f"*[feeds] Error*\n```{error_msg}```",
        )
    except Exception:
        print(f"Failed to send error to Slack: {traceback.format_exc()}")


def send_to_slack(config, recommendations):
    """Send recommendations to Slack channel."""
    slack_cfg = config["slack"]
    client = WebClient(token=slack_cfg["bot_token"])
    channel = slack_cfg["channel"]

    text = format_slack_message(recommendations)

    max_msg_len = slack_cfg.get("max_message_length", 4000)
    if len(text) <= max_msg_len:
        client.chat_postMessage(channel=channel, text=text)
    else:
        chunks = _split_message(text, max_msg_len)
        for chunk in chunks:
            client.chat_postMessage(channel=channel, text=chunk)

    print(f"  Sent {len(recommendations)} recommendations to {channel}")


def main():
    parser = argparse.ArgumentParser(description="RSS Feed Recommender")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--profile", default="research_profile.yaml", help="Research profile path")
    parser.add_argument("--dry-run", action="store_true", help="Print recommendations without sending to Slack")
    parser.add_argument("--days", type=int, help="Override days_lookback from config")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    config = load_config(base_dir / args.config)
    profile = load_research_profile(base_dir / args.profile)

    feed_cfg = config.get("feeds", {})
    opml_path = base_dir / feed_cfg.get("opml_file", "feeds.opml")
    max_per_feed = feed_cfg.get("max_articles_per_feed", 20)
    days_lookback = args.days or feed_cfg.get("days_lookback", 1)

    # Step 1: Parse OPML and fetch articles
    print("[1/3] Fetching RSS feeds...")
    feeds = parse_opml(opml_path)
    print(f"  Found {len(feeds)} feeds in OPML")
    articles = fetch_articles(feeds, config, max_per_feed, days_lookback)
    print(f"  Collected {len(articles)} recent articles")

    if not articles:
        print("No recent articles found. Exiting.")
        return

    # Step 2: LLM-based recommendation
    print("[2/3] Getting recommendations...")
    recommendations = recommend_articles(articles, profile, config)
    print(f"  Got {len(recommendations)} recommendations")

    # Step 3: Send to Slack
    if args.dry_run:
        print("\n[3/3] Dry run - printing recommendations:\n")
        grouped = _group_by_feed(recommendations)
        for feed_name, recs in grouped.items():
            print(f"[{feed_name}]")
            for rec in recs:
                authors = rec.get("authors", "")
                print(f"  {rec['title']}")
                print(f"  {authors}")
                print(f"  {rec['link']}")
                print()

    else:
        print("[3/3] Sending to Slack...")
        try:
            send_to_slack(config, recommendations)
            print("Done!")
        except SlackApiError as e:
            print(f"  Slack error: {e.response['error']}")
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            base_dir = Path(__file__).parent
            config = load_config(base_dir / "config.yaml")
            send_error_to_slack(config, tb[-3000:])
        except Exception:
            print(f"Failed to report error: {traceback.format_exc()}")
        sys.exit(1)
