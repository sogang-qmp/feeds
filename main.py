#!/usr/bin/env python3
"""RSS Feed Reader with SQLite storage, LLM-based scoring, and static HTML generation.

Usage:
    python main.py fetch     # RSS → SQLite
    python main.py curate    # SQLite (new articles) → score → HTML → Slack
"""

import xml.etree.ElementTree as ET
import sqlite3
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sys
import traceback

import anthropic
import feedparser
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Haiku pricing per 1M tokens
HAIKU_INPUT_COST = 0.80   # $/1M input tokens
HAIKU_OUTPUT_COST = 4.00  # $/1M output tokens

log = logging.getLogger("feeds")


def setup_logging(base_dir):
    """Configure logging with 90-day rotating file retention."""
    log_dir = base_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "feeds.log",
        when="midnight",
        backupCount=90,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))

    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(console)


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def load_research_profile(path="research_profile.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def init_db(db_path):
    """Initialize SQLite database and return connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            link TEXT UNIQUE,
            feed TEXT,
            folder TEXT,
            title TEXT,
            authors TEXT,
            summary TEXT,
            published TEXT,
            fetched_at TEXT,
            curated INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def parse_opml(path):
    """Parse OPML file and return list of feed URLs with titles, preserving folder structure and order."""
    tree = ET.parse(path)
    feeds = []
    body = tree.find(".//body")
    for folder in body:
        folder_name = folder.get("title") or folder.get("text") or ""
        if folder.get("xmlUrl"):
            feeds.append({
                "title": folder.get("title") or folder.get("text") or folder.get("xmlUrl"),
                "url": folder.get("xmlUrl"),
                "folder": "",
            })
            continue
        for outline in folder:
            url = outline.get("xmlUrl")
            if url:
                feeds.append({
                    "title": outline.get("title") or outline.get("text") or url,
                    "url": url,
                    "folder": folder_name,
                })
    return feeds


# --- fetch command ---

def fetch_articles(feeds, conn):
    """Fetch RSS feeds and store new articles in SQLite."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0

    for feed_info in feeds:
        log.info(f"  Fetching: {feed_info['title']}...")
        try:
            parsed = feedparser.parse(feed_info["url"])
        except Exception as e:
            log.warning(f"    Error fetching {feed_info['title']}: {e}")
            continue

        for entry in parsed.entries:
            published = None
            for date_field in ("published_parsed", "updated_parsed"):
                t = getattr(entry, date_field, None)
                if t:
                    published = datetime(*t[:6], tzinfo=timezone.utc)
                    break

            if not published:
                continue

            link = getattr(entry, "link", "")
            if not link:
                continue

            summary = getattr(entry, "summary", "") or ""
            if len(summary) > 500:
                summary = summary[:500] + "..."

            authors = ""
            if hasattr(entry, "authors") and entry.authors:
                authors = ", ".join(a.get("name", "") for a in entry.authors if a.get("name"))
            elif hasattr(entry, "author"):
                authors = entry.author or ""

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, feed, folder, title, authors, summary, published, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (link, feed_info["title"], feed_info.get("folder", ""),
                     getattr(entry, "title", "No title"), authors, summary,
                     published.isoformat(), now),
                )
                if conn.total_changes:
                    new_count += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    return new_count


def cmd_fetch(args, base_dir, config):
    """fetch subcommand: RSS → SQLite."""
    feed_cfg = config.get("feeds", {})
    opml_path = base_dir / feed_cfg.get("opml_file", "feeds.opml")
    db_path = base_dir / feed_cfg.get("db", "feeds.db")

    conn = init_db(db_path)
    try:
        feeds = parse_opml(opml_path)
        log.info(f"[fetch] Fetching {len(feeds)} feeds...")

        before = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        fetch_articles(feeds, conn)
        after = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        new = after - before
        pending = conn.execute("SELECT COUNT(*) FROM articles WHERE curated=0").fetchone()[0]

        log.info(f"[fetch] Done. {new} new articles stored. {pending} pending curation.")
    finally:
        conn.close()


# --- curate command ---

def _build_profile_text(profile):
    """Build profile text for scoring prompt."""
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

    text = (
        f"Name: {researcher.get('name', 'N/A')}\n"
        f"Position: {researcher.get('position', 'N/A')}\n"
        f"Affiliation: {researcher.get('affiliation', 'N/A')}\n"
        f"Research areas: {', '.join(flatten(areas))}\n"
        f"Keywords: {', '.join(flatten(keywords))}\n"
    )
    scoring_prompt = profile.get("scoring_prompt", "")
    if scoring_prompt:
        text += f"\nScoring guidance:\n{scoring_prompt}"
    return text


def _score_batch(batch, profile_text, client, model, max_tokens):
    """Score a single batch of articles."""
    articles_text = ""
    for i, a in enumerate(batch):
        articles_text += (
            f"\n[{i}] Feed: {a['feed']}\n"
            f"    Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Summary: {a['summary']}\n"
        )

    prompt = f"""You are a research assistant. Score EVERY article by relevance to this researcher's interests.

## Researcher Profile
{profile_text}

## Articles
{articles_text}

## Instructions
Score each article from 1 to 5:
  5 = directly related to researcher's core topics
  4 = closely related
  3 = somewhat related
  2 = tangentially related
  1 = not related

Return a JSON array with ALL {len(batch)} articles. Each element:
- "index": article index number
- "score": integer 1-5

You MUST include every article. Return ONLY the JSON array."""

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    if not message.content:
        raise ValueError("LLM returned empty content")

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0].strip()

    scores = json.loads(response_text)
    if not isinstance(scores, list):
        raise ValueError(f"Expected JSON array, got {type(scores).__name__}")
    score_map = {}
    for s in scores:
        if isinstance(s, dict) and "index" in s and "score" in s:
            score_map[s["index"]] = s["score"]

    usage = message.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    return score_map, input_tokens, output_tokens


BATCH_SIZE = 200


def score_articles(articles, profile, config):
    """Use Haiku to score every article on a 1-5 scale, batching if needed."""
    if not articles:
        return []

    api_cfg = config["anthropic"]
    profile_text = _build_profile_text(profile)
    client = anthropic.Anthropic(api_key=api_cfg["api_key"])
    model = api_cfg.get("scoring_model", "claude-haiku-4-5-20251001")
    max_tokens = api_cfg.get("max_tokens", 16384)

    scored = []
    total_input = 0
    total_output = 0
    failed_batches = 0
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start:start + BATCH_SIZE]
        batch_num = start // BATCH_SIZE + 1
        total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
        log.info(f"  Scoring batch {batch_num}/{total_batches} ({len(batch)} articles)...")

        try:
            score_map, inp_tok, out_tok = _score_batch(batch, profile_text, client, model, max_tokens)
            total_input += inp_tok
            total_output += out_tok
        except (anthropic.APIError, json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning(f"  Batch {batch_num} failed: {e}")
            score_map = {}
            failed_batches += 1
        for i, a in enumerate(batch):
            scored.append({**a, "score": score_map.get(i, 1)})

    cost = (total_input * HAIKU_INPUT_COST + total_output * HAIKU_OUTPUT_COST) / 1_000_000
    log.info(f"  LLM usage: {total_input} input + {total_output} output tokens = ${cost:.4f}")
    if failed_batches:
        msg = f"{failed_batches}/{total_batches} batch(es) failed — affected articles scored as 1"
        log.warning(f"  {msg}")
        send_error_to_slack(config, f"Scoring partial failure: {msg}")

    return scored


def sort_by_opml(scored, feeds):
    """Sort scored articles by OPML folder/feed order, then score desc."""
    feed_order = {}
    for i, f in enumerate(feeds):
        key = (f["folder"], f["title"])
        if key not in feed_order:
            feed_order[key] = i

    scored.sort(key=lambda r: (feed_order.get((r["folder"], r["feed"]), 999), -r["score"]))
    return scored


def _ga_snippet(ga_id):
    """Return Google Analytics snippet if GA ID is configured."""
    if not ga_id:
        return ""
    return (
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga_id}"></script>'
        f"<script>window.dataLayer=window.dataLayer||[];"
        f"function gtag(){{dataLayer.push(arguments)}}"
        f"gtag('js',new Date());gtag('config','{ga_id}');</script>"
    )


def generate_html(scored_articles, today, ga_id=""):
    """Generate static HTML page with articles grouped by folder/feed in OPML order."""
    from collections import OrderedDict

    folders = OrderedDict()
    for a in scored_articles:
        folder = a.get("folder", "")
        feed = a["feed"]
        folders.setdefault(folder, OrderedDict())
        folders[folder].setdefault(feed, []).append(a)

    rows = ""
    for folder_name, feeds in folders.items():
        if folder_name:
            rows += f'<tr class="folder-row"><td colspan="3" class="folder">{folder_name}</td></tr>\n'
        for feed_name, articles in feeds.items():
            count = len(articles)
            rows += f'<tr class="feed-row"><td colspan="3" class="feed">{feed_name} <span class="count">({count})</span></td></tr>\n'
            for a in articles:
                score = a["score"]
                score_class = f"s{score}"
                title = a.get("title", "")
                link = a.get("link", "")
                authors = a.get("authors", "") or ""
                title_html = f'<a href="{link}" target="_blank" rel="noopener">{title}</a>' if link else title
                author_html = f'<span class="authors">{authors}</span>' if authors else ""
                rows += (
                    f"<tr>"
                    f'<td class="score {score_class}">{score}</td>'
                    f"<td>{title_html}{author_html}</td>"
                    f"</tr>\n"
                )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Feeds — {today}</title>
{_ga_snippet(ga_id)}
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.3em; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 4px 8px; vertical-align: top; font-size: 0.9em; }}
.folder-row td {{ padding-top: 28px; }}
.folder {{ font-weight: bold; font-size: 1.1em; color: #007bff; padding: 0; }}
.feed-row td {{ padding-top: 16px; }}
.feed {{ font-weight: bold; font-size: 0.95em; padding: 4px 0; }}
.count {{ font-weight: normal; color: #888; font-size: 0.85em; }}
.score {{ text-align: center; font-weight: bold; width: 2em; }}
.s5 {{ background: #c62828; color: #fff; }}
.s4 {{ background: #ef6c00; color: #fff; }}
.s3 {{ background: #f9a825; }}
.s2 {{ background: #e0e0e0; }}
.s1 {{ background: #f5f5f5; color: #999; }}
.authors {{ display: block; font-size: 0.85em; color: #000; }}
a {{ color: #1a0dab; text-decoration: none; }}
a:visited {{ color: #681da8; }}
a:hover {{ text-decoration: underline; }}
</style></head><body>
<p><a href="index.html">&larr; All feeds</a></p>
<h1>Feeds &mdash; {today}</h1>
<table>{rows}</table>
</body></html>"""
    return html


def deploy_html(html, today, base_dir, ga_id=""):
    """Write HTML to local html/ directory and update index."""
    out_dir = base_dir / "html"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{today}.html"
    path.write_text(html)
    log.info(f"  Written to {path}")
    # latest.html always points to most recent curation
    latest = out_dir / "latest.html"
    latest.unlink(missing_ok=True)
    latest.symlink_to(path.name)
    update_index(out_dir, ga_id)
    return path


def update_index(out_dir, ga_id=""):
    """Regenerate index.html listing all date pages in reverse chronological order."""
    pages = sorted(out_dir.glob("2*.html"), reverse=True)
    items = ""
    for p in pages:
        name = p.stem
        items += f'<li><a href="{p.name}">{name}</a></li>\n'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Feeds</title>
{_ga_snippet(ga_id)}
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.3em; }}
ul {{ list-style: none; padding: 0; }}
li {{ padding: 4px 0; }}
a {{ color: #1a0dab; text-decoration: none; font-size: 1.1em; }}
a:visited {{ color: #681da8; }}
a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>Feeds</h1>
<ul>{items}</ul>
</body></html>"""
    (out_dir / "index.html").write_text(html)


def send_link_to_slack(config, today):
    """Send just the link to Slack #journal-feed."""
    slack_cfg = config["slack"]
    client = WebClient(token=slack_cfg["bot_token"])
    channel = slack_cfg["channel"]
    base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
    url = f"{base_url}/{today}.html"

    client.chat_postMessage(
        channel=channel,
        text=f"*Feeds — {today}*\n{url}",
        unfurl_links=False,
        unfurl_media=False,
    )
    log.info(f"  Sent link to {channel}")


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
        log.info(f"Failed to send error to Slack: {traceback.format_exc()}")


def cmd_curate(args, base_dir, config):
    """curate subcommand: score new articles → HTML → Slack."""
    feed_cfg = config.get("feeds", {})
    db_path = base_dir / feed_cfg.get("db", "feeds.db")
    opml_path = base_dir / feed_cfg.get("opml_file", "feeds.opml")
    today = datetime.now().strftime("%Y-%m-%d")

    conn = init_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE curated=0 ORDER BY id"
        ).fetchall()

        articles = [dict(r) for r in rows]
        log.info(f"[curate] {len(articles)} new articles to process.")

        if not articles:
            log.info("[curate] Nothing to curate.")
            return

        # Score
        profile = load_research_profile(base_dir / args.profile)
        log.info("[curate] Scoring...")
        scored = score_articles(articles, profile, config)

        # Sort by OPML order
        feeds = parse_opml(opml_path)
        scored = sort_by_opml(scored, feeds)

        # Generate HTML
        log.info("[curate] Generating HTML...")
        ga_id = config.get("analytics", {}).get("ga_id", "")
        html = generate_html(scored, today, ga_id)
        deploy_html(html, today, base_dir, ga_id)

        # Mark as curated
        ids = [a["id"] for a in articles]
        conn.execute(
            f"UPDATE articles SET curated=1 WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
    finally:
        conn.close()

    # Slack (after DB is closed — failure here shouldn't affect curation state)
    if args.dry_run:
        base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
        log.info(f"[curate] Dry run — URL would be: {base_url}/{today}.html")
    else:
        log.info("[curate] Sending link to Slack...")
        try:
            send_link_to_slack(config, today)
        except SlackApiError as e:
            log.error(f"[curate] Failed to send Slack notification: {e}")
            send_error_to_slack(config, f"Curation succeeded but Slack notification failed:\n{e}")
        log.info("[curate] Done!")


def main():
    parser = argparse.ArgumentParser(description="RSS Feed Recommender")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--profile", default="research_profile.yaml", help="Research profile path")
    parser.add_argument("--dry-run", action="store_true", help="Skip Slack notification")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("fetch", help="Fetch RSS feeds into SQLite")
    sub.add_parser("curate", help="Score new articles, generate HTML, notify Slack")
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    base_dir = Path(__file__).parent
    setup_logging(base_dir)
    config = load_config(base_dir / args.config)

    if args.command == "fetch":
        cmd_fetch(args, base_dir, config)
    elif args.command == "curate":
        cmd_curate(args, base_dir, config)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        try:
            base_dir = Path(__file__).parent
            config = load_config(base_dir / "config.yaml")
            send_error_to_slack(config, tb[-3000:])
        except Exception:
            log.info(f"Failed to report error: {traceback.format_exc()}")
        sys.exit(1)
