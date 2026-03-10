#!/usr/bin/env python3
"""RSS Feed Reader with SQLite storage, LLM-based scoring, and static HTML generation.

Usage:
    python main.py fetch     # RSS → SQLite
    python main.py curate    # SQLite (new articles) → score → summarize → HTML + MD → Slack DM
"""

import xml.etree.ElementTree as ET
import sqlite3
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import sys
import traceback

import shutil
import subprocess
import tempfile

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


def load_topics(path="topics.yaml"):
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
            curated INTEGER DEFAULT 0,
            score INTEGER
        )
    """)
    # Migrate: add score column if missing (existing DBs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    if "score" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN score INTEGER")
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
            if len(summary) > 2000:
                summary = summary[:2000] + "..."

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

        log.info(f"[fetch] Done. {new} new articles stored.")
    finally:
        conn.close()


# --- curate command ---

def _build_topics_text(topics_config):
    """Build topics text for scoring prompt."""
    topics = topics_config.get("topics", [])
    lines = ["Topics of interest:"]
    for i, t in enumerate(topics, 1):
        lines.append(f"  {i}. {t}")

    scoring_prompt = topics_config.get("scoring_prompt", "")
    if scoring_prompt:
        lines.append(f"\nScoring guidance:\n{scoring_prompt}")
    return "\n".join(lines)


def _score_batch(batch, topics_text, client, model, max_tokens):
    """Score a single batch of articles."""
    articles_text = ""
    for i, a in enumerate(batch):
        articles_text += (
            f"\n[{i}] Feed: {a['feed']}\n"
            f"    Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Summary: {a['summary']}\n"
        )

    prompt = f"""You are a research assistant. Score EVERY article by relevance to the following topics.

## Topics
{topics_text}

## Articles
{articles_text}

## Instructions
Score each article from 1 to 5:
  5 = directly about one of the topics
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
    return score_map, usage.input_tokens, usage.output_tokens


BATCH_SIZE = 200
SUMMARY_BATCH_SIZE = 10


def score_articles(articles, topics_config, config):
    """Use Haiku to score every article on a 1-5 scale."""
    if not articles:
        return []

    api_cfg = config["anthropic"]
    topics_text = _build_topics_text(topics_config)
    client = anthropic.Anthropic(api_key=api_cfg["api_key"])
    model = api_cfg.get("scoring_model", "claude-haiku-4-5-20251001")
    max_tokens = api_cfg.get("max_tokens", 16384)

    scored = []
    total_input = 0
    total_output = 0
    failed_batches = 0
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start:start + BATCH_SIZE]
        batch_num = start // BATCH_SIZE + 1
        log.info(f"  Scoring batch {batch_num}/{total_batches} ({len(batch)} articles)...")

        try:
            score_map, inp_tok, out_tok = _score_batch(batch, topics_text, client, model, max_tokens)
            total_input += inp_tok
            total_output += out_tok
        except (anthropic.APIError, json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning(f"  Batch {batch_num} failed: {e}")
            score_map = {}
            failed_batches += 1
        for i, a in enumerate(batch):
            scored.append({**a, "score": score_map.get(i, 1)})

    cost = (total_input * HAIKU_INPUT_COST + total_output * HAIKU_OUTPUT_COST) / 1_000_000
    log.info(f"  Scoring: {total_input} input + {total_output} output tokens = ${cost:.4f}")
    if failed_batches:
        msg = f"{failed_batches}/{total_batches} batch(es) failed — affected articles scored as 1"
        log.warning(f"  {msg}")
        send_error_to_slack(config, f"Scoring partial failure: {msg}")

    return scored


def _summarize_batch(batch, client, model, max_tokens):
    """Summarize a batch of high-scoring articles using LLM."""
    articles_text = ""
    for i, a in enumerate(batch):
        articles_text += (
            f"\n[{i}] Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Abstract: {a['summary']}\n"
        )

    prompt = f"""Summarize each article based on its title and abstract.
{articles_text}

For each article, provide:
- "summary": 2-3 sentence summary of the key contribution
- "key_points": list of 2-4 main findings or contributions

Return a JSON array:
[{{"index": 0, "summary": "...", "key_points": ["...", "..."]}}, ...]

Write in the same language as each article's title/abstract. Return ONLY the JSON array."""

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

    summaries = json.loads(response_text)
    if not isinstance(summaries, list):
        raise ValueError(f"Expected JSON array, got {type(summaries).__name__}")

    summary_map = {}
    for s in summaries:
        if isinstance(s, dict) and "index" in s:
            summary_map[s["index"]] = {
                "summary": s.get("summary", ""),
                "key_points": s.get("key_points", []),
            }

    usage = message.usage
    return summary_map, usage.input_tokens, usage.output_tokens


def summarize_articles(scored, config, threshold=4):
    """Summarize high-scoring articles using LLM. Returns new list with summary_info attached."""
    high = [a for a in scored if a["score"] >= threshold]
    if not high:
        log.info("  No articles above summary threshold.")
        return [{**a, "summary_info": {}} for a in scored]

    api_cfg = config["anthropic"]
    client = anthropic.Anthropic(api_key=api_cfg["api_key"])
    model = api_cfg.get("summary_model", api_cfg.get("scoring_model", "claude-haiku-4-5-20251001"))
    max_tokens = api_cfg.get("max_tokens", 16384)

    total_input = 0
    total_output = 0
    failed_batches = 0
    summary_data = {}
    total_batches = (len(high) + SUMMARY_BATCH_SIZE - 1) // SUMMARY_BATCH_SIZE

    for start in range(0, len(high), SUMMARY_BATCH_SIZE):
        batch = high[start:start + SUMMARY_BATCH_SIZE]
        batch_num = start // SUMMARY_BATCH_SIZE + 1
        log.info(f"  Summarizing batch {batch_num}/{total_batches} ({len(batch)} articles)...")

        try:
            smap, inp_tok, out_tok = _summarize_batch(batch, client, model, max_tokens)
            total_input += inp_tok
            total_output += out_tok
            for i, a in enumerate(batch):
                if i in smap:
                    summary_data[a["link"]] = smap[i]
        except (anthropic.APIError, json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning(f"  Summary batch {batch_num} failed: {e}")
            failed_batches += 1

    cost = (total_input * HAIKU_INPUT_COST + total_output * HAIKU_OUTPUT_COST) / 1_000_000
    log.info(f"  Summary: {total_input} input + {total_output} output tokens = ${cost:.4f}")
    if failed_batches:
        log.warning(f"  {failed_batches}/{total_batches} summary batch(es) failed")

    return [
        {**a, "summary_info": summary_data.get(a["link"], {})}
        for a in scored
    ]


def generate_summary_md(scored, today, threshold=4):
    """Generate markdown summary for high-scoring articles."""
    high = [a for a in scored if a["score"] >= threshold and a.get("summary_info")]
    if not high:
        return ""

    total = len(scored)
    lines = [
        f"# Feeds Summary — {today}\n",
        f"> {len(high)} articles with score >= {threshold} (out of {total} total)\n",
    ]

    for a in high:
        lines.append("---\n")
        lines.append(f"### {a['title']}\n")
        authors = a.get("authors", "") or "N/A"
        lines.append(f"**Authors:** {authors}  ")
        lines.append(f"**Source:** {a['feed']} | **Score:** {a['score']}  ")
        lines.append(f"[Read paper]({a['link']})\n")

        info = a.get("summary_info", {})
        if info.get("summary"):
            lines.append(f"{info['summary']}\n")
        if info.get("key_points"):
            lines.append("**Key points:**")
            for p in info["key_points"]:
                lines.append(f"- {p}")
            lines.append("")

    return "\n".join(lines)


def render_summary_html(md_text, today, ga_id=""):
    """Convert summary markdown to a styled HTML page."""
    import markdown

    body = markdown.markdown(md_text, extensions=["extra"])

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Summary — {today}</title>
{_ga_snippet(ga_id)}
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; line-height: 1.6; }}
h1 {{ font-size: 1.3em; }}
h3 {{ margin-top: 1.5em; margin-bottom: 0.3em; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
blockquote {{ color: #555; border-left: 3px solid #ccc; margin: 0.5em 0; padding: 0.3em 1em; }}
strong {{ color: #333; }}
ul {{ padding-left: 1.5em; }}
li {{ margin: 0.3em 0; }}
a {{ color: #1a0dab; text-decoration: none; }}
a:visited {{ color: #681da8; }}
a:hover {{ text-decoration: underline; }}
@media (max-width: 600px) {{
  body {{ margin: 1em auto; padding: 0 0.5em; }}
}}
</style></head><body>
<p><a href="index.html">&larr; All feeds</a></p>
{body}
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}},{{left:'\\\\(',right:'\\\\)',display:false}},{{left:'\\\\[',right:'\\\\]',display:true}}]}});">
</script>
</body></html>"""


def deploy_summary(md, today, base_dir, ga_id=""):
    """Write summary markdown and HTML to summaries/ directory."""
    out_dir = base_dir / "summaries"
    out_dir.mkdir(exist_ok=True)

    md_path = out_dir / f"{today}.md"
    md_path.write_text(md)
    log.info(f"  Written summary to {md_path}")

    html = render_summary_html(md, today, ga_id)
    html_path = out_dir / f"{today}.html"
    html_path.write_text(html)
    log.info(f"  Written summary HTML to {html_path}")

    return md_path


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


def generate_html(scored_articles, today, ga_id="", summary_count=0):
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

    summary_link = ""
    if summary_count > 0:
        summary_link = (
            f'<p class="summary-link">'
            f'<a href="../summaries/{today}.html">Summary ({summary_count} articles)</a>'
            f'</p>'
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Feeds — {today}</title>
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
.summary-link {{ margin: 0.5em 0; }}
.summary-link a {{ background: #1a73e8; color: #fff; padding: 6px 14px; border-radius: 4px; font-size: 0.9em; text-decoration: none; }}
.summary-link a:hover {{ background: #1557b0; }}
a {{ color: #1a0dab; text-decoration: none; }}
a:visited {{ color: #681da8; }}
a:hover {{ text-decoration: underline; }}
@media (max-width: 600px) {{
  body {{ margin: 1em auto; padding: 0 0.5em; }}
  .score {{ width: 1.5em; font-size: 0.85em; }}
  td {{ padding: 3px 4px; font-size: 0.85em; }}
}}
</style></head><body>
<p><a href="index.html">&larr; All feeds</a></p>
<h1>Feeds &mdash; {today}</h1>
{summary_link}
<table>{rows}</table>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}},{{left:'\\\\(',right:'\\\\)',display:false}},{{left:'\\\\[',right:'\\\\]',display:true}}]}});">
</script>
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
    opml_path = base_dir / "feeds.opml"
    feeds = parse_opml(opml_path) if opml_path.exists() else []
    update_index(out_dir, ga_id, feeds)
    return path


def update_index(out_dir, ga_id="", feeds=None):
    """Regenerate index.html listing all date pages and subscribed feeds."""
    pages = sorted(out_dir.glob("2*.html"), reverse=True)
    items = ""
    for p in pages:
        name = p.stem
        items += f'<li><a href="{p.name}">{name}</a></li>\n'

    feed_section = ""
    if feeds:
        from collections import OrderedDict
        folders = OrderedDict()
        for f in feeds:
            folders.setdefault(f["folder"], []).append(f["title"])
        feed_rows = ""
        for folder, titles in folders.items():
            if folder:
                feed_rows += f'<li class="feed-folder">{folder}</li>\n'
            for t in titles:
                feed_rows += f"<li>{t}</li>\n"
        feed_section = f'<h2>Subscribed Feeds ({len(feeds)})</h2>\n<ul class="feed-list">{feed_rows}</ul>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Feeds</title>
{_ga_snippet(ga_id)}
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.3em; }}
h2 {{ font-size: 1.1em; margin-top: 2em; color: #555; }}
ul {{ list-style: none; padding: 0; }}
li {{ padding: 4px 0; font-size: 0.9em; }}
a {{ color: #1a0dab; text-decoration: none; }}
a:visited {{ color: #681da8; }}
a:hover {{ text-decoration: underline; }}
.feed-folder {{ font-weight: bold; font-size: 1.1em; color: #007bff; padding: 8px 0 2px 0; }}
.feed-list li {{ padding: 2px 0 2px 1em; color: #444; }}
</style></head><body>
<h1>Feeds</h1>
<ul>{items}</ul>
{feed_section}
</body></html>"""
    (out_dir / "index.html").write_text(html)


def deploy_to_github_pages(base_dir, gh_repo):
    """Push html/ and summaries/ to the gh-pages branch of the given GitHub repo."""
    html_dir = base_dir / "html"
    summaries_dir = base_dir / "summaries"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Clone gh-pages branch (shallow)
        subprocess.run(
            ["git", "clone", "--branch", "gh-pages", "--depth", "1",
             f"https://github.com/{gh_repo}.git", str(tmp_path / "repo")],
            check=True, capture_output=True, text=True,
        )
        repo = tmp_path / "repo"

        # Copy html files (skip symlinks)
        for f in html_dir.iterdir():
            if f.is_file() and not f.is_symlink():
                shutil.copy2(f, repo / f.name)

        # Copy summaries
        dest_summaries = repo / "summaries"
        dest_summaries.mkdir(exist_ok=True)
        if summaries_dir.exists():
            for f in summaries_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest_summaries / f.name)

        # Commit and push
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, capture_output=True,
        )
        if result.returncode == 0:
            log.info("  No changes to deploy.")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "commit", "-m", f"Deploy {today}"],
            cwd=repo, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "push"], cwd=repo, check=True, capture_output=True, text=True,
        )
        log.info(f"  Deployed to https://github.com/{gh_repo} (gh-pages)")


def send_link_to_slack(config, today, has_summary=False):
    """Send the daily feed link as a Slack DM."""
    slack_cfg = config["slack"]
    client = WebClient(token=slack_cfg["bot_token"])
    user_id = slack_cfg["user_id"]
    base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
    url = f"{base_url}/{today}.html"

    text = f"*Feeds — {today}*\n{url}"
    if has_summary:
        summary_url = f"{base_url}/summaries/{today}.html"
        text += f"\n\n*Summary:* {summary_url}"

    client.chat_postMessage(
        channel=user_id,
        text=text,
        unfurl_links=False,
        unfurl_media=False,
    )
    log.info(f"  Sent DM to {user_id}")


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
    """curate subcommand: score → summarize → HTML + MD → Slack DM."""
    feed_cfg = config.get("feeds", {})
    db_path = base_dir / feed_cfg.get("db", "feeds.db")
    opml_path = base_dir / feed_cfg.get("opml_file", "feeds.opml")
    today = datetime.now().strftime("%Y-%m-%d")

    conn = init_db(db_path)
    has_summary = False
    try:
        # Select articles published within the last 2 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        rows = conn.execute(
            "SELECT * FROM articles WHERE published >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()

        articles = [dict(r) for r in rows]
        log.info(f"[curate] {len(articles)} articles in the last 2 days.")

        if not articles:
            log.info("[curate] Nothing to curate.")
            return

        # Split: already scored vs needs scoring
        to_score = [a for a in articles if a["score"] is None]
        already_scored = [a for a in articles if a["score"] is not None]
        log.info(f"[curate] {len(already_scored)} already scored, {len(to_score)} to score.")

        # Score only new articles
        topics = load_topics(base_dir / args.topics)
        if to_score:
            log.info("[curate] Scoring...")
            newly_scored = score_articles(to_score, topics, config)

            # Save scores to DB
            for a in newly_scored:
                conn.execute(
                    "UPDATE articles SET score=? WHERE id=?",
                    (a["score"], a["id"]),
                )
            conn.commit()
        else:
            newly_scored = []

        # Combine: use cached scores for already-scored, fresh scores for new
        scored = already_scored + newly_scored

        # Sort by OPML order
        feeds = parse_opml(opml_path)
        scored = sort_by_opml(scored, feeds)

        # Summarize high-scoring articles
        threshold = topics.get("summary_threshold", 4)
        log.info(f"[curate] Summarizing articles with score >= {threshold}...")
        scored = summarize_articles(scored, config, threshold)

        # Generate HTML
        summary_count = len([a for a in scored if a["score"] >= threshold and a.get("summary_info")])
        log.info("[curate] Generating HTML...")
        ga_id = config.get("analytics", {}).get("ga_id", "")
        html = generate_html(scored, today, ga_id, summary_count)
        deploy_html(html, today, base_dir, ga_id)

        # Generate summary markdown
        summary_md = generate_summary_md(scored, today, threshold)
        if summary_md:
            deploy_summary(summary_md, today, base_dir, ga_id)
            has_summary = True
    finally:
        conn.close()

    # Deploy to GitHub Pages
    deploy_cfg = config.get("deploy", {})
    gh_repo = deploy_cfg.get("gh_repo")
    if gh_repo and not args.dry_run:
        log.info("[curate] Deploying to GitHub Pages...")
        try:
            deploy_to_github_pages(base_dir, gh_repo)
        except Exception as e:
            log.error(f"[curate] GitHub Pages deploy failed: {e}")
            send_error_to_slack(config, f"GitHub Pages deploy failed:\n{e}")

    # Slack DM (after DB is closed — failure here shouldn't affect curation state)
    if args.dry_run:
        base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
        log.info(f"[curate] Dry run — URL would be: {base_url}/{today}.html")
    else:
        log.info("[curate] Sending DM...")
        try:
            send_link_to_slack(config, today, has_summary)
        except SlackApiError as e:
            log.error(f"[curate] Failed to send Slack DM: {e}")
            send_error_to_slack(config, f"Curation succeeded but Slack DM failed:\n{e}")
        log.info("[curate] Done!")


def main():
    parser = argparse.ArgumentParser(description="RSS Feed Recommender")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--topics", default="topics.yaml", help="Topics file path")
    parser.add_argument("--dry-run", action="store_true", help="Skip Slack notification")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("fetch", help="Fetch RSS feeds into SQLite")
    sub.add_parser("curate", help="Score, summarize, generate HTML, notify via DM")
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
