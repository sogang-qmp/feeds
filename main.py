#!/usr/bin/env python3
"""RSS Feed Reader with SQLite storage, LLM-based scoring, and static HTML generation.

Usage:
    python main.py fetch     # RSS -> SQLite
    python main.py curate    # SQLite (new articles) -> score -> HTML -> Slack
"""

import argparse
import logging
import logging.handlers
import sys
import traceback
from datetime import datetime
from pathlib import Path

import yaml
from slack_sdk.errors import SlackApiError

from db import init_db
from notify import send_link_to_slack, send_error_to_slack
from recommend import recommend_articles
from rendering import generate_html, deploy_html
from scoring import score_articles, sort_by_opml
from sources.rss import parse_opml, fetch_articles

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


def cmd_fetch(args, base_dir, config):
    """fetch subcommand: RSS -> SQLite."""
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


def cmd_curate(args, base_dir, config):
    """curate subcommand: score new articles -> HTML -> Slack."""
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
        scored = score_articles(articles, profile, config, notify_fn=send_error_to_slack)

        # Sort by OPML order
        feeds = parse_opml(opml_path)
        scored = sort_by_opml(scored, feeds)

        # Recommendations
        recommendations = None
        log.info("[curate] Generating recommendations...")
        try:
            recommendations = recommend_articles(profile, base_dir)
            log.info(f"[curate] Got {len(recommendations)} recommendations.")
        except Exception as e:
            log.warning(f"[curate] Recommendations failed (proceeding without): {e}")

        # Generate HTML
        log.info("[curate] Generating HTML...")
        ga_id = config.get("analytics", {}).get("ga_id", "")
        html = generate_html(scored, today, ga_id, recommendations)
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

    # Slack (after DB is closed -- failure here shouldn't affect curation state)
    if args.dry_run:
        base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
        log.info(f"[curate] Dry run -- URL would be: {base_url}/{today}.html")
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
