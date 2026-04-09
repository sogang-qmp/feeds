"""RSS feed parsing and fetching."""

import logging
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import feedparser
import requests

log = logging.getLogger("feeds")


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


def fetch_articles(feeds, conn):
    """Fetch RSS feeds and store new articles in SQLite."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0

    for feed_info in feeds:
        log.info(f"  Fetching: {feed_info['title']}...")
        try:
            resp = requests.get(feed_info["url"], timeout=60)
            parsed = feedparser.parse(resp.content)
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
                cur = conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, feed, folder, title, authors, summary, published, fetched_at, source_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (link, feed_info["title"], feed_info.get("folder", ""),
                     getattr(entry, "title", "No title"), authors, summary,
                     published.isoformat(), now, "rss"),
                )
                if cur.rowcount > 0:
                    new_count += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    return new_count
