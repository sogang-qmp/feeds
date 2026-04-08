"""Direct scraper for anthropic.com posts.

Anthropic does not publish official RSS feeds. The community-mirrored
feeds in feeds.opml lag (Olshansk: ~1-2 weeks) or are dead (conoro
anthropic-engineering hasn't updated since Jan 2025). This source pulls
posts straight from anthropic.com's Sanity CMS payload embedded in each
section's HTML response, so the journal-feed sees new posts the same day
they go up.

Inserts use the same `feed` titles as the OPML entries ("Anthropic
News", "Anthropic Engineering", "Anthropic Research") so sort_by_opml
keeps them in their existing positions, and dedupe vs the RSS mirrors
happens automatically via the UNIQUE(link) constraint on `articles`.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

import requests

log = logging.getLogger("feeds")

UA = "Mozilla/5.0 (compatible; vesper-feeds/1.0)"
FOLDER = "AI Labs & Research"

# (feed_title, section_path)
SECTIONS = [
    ("Anthropic News", "news"),
    ("Anthropic Engineering", "engineering"),
    ("Anthropic Research", "research"),
]

# The Sanity post records appear inside __next_f streamed JS strings as
# escaped JSON. We unescape, then loosely match on field order:
#   "publishedOn":"...", ..., "current":"<slug>", ..., "summary":"...", ..., "title":"..."
POST_RE = re.compile(
    r'"publishedOn":"(?P<date>[^"]+)"'
    r'.{0,400}?"current":"(?P<slug>[^"]+)"'
    r'.{0,3000}?"summary":"(?P<summary>[^"]*)"'
    r'.{0,400}?"title":"(?P<title>[^"]+)"',
    re.S,
)


def _unescape(s):
    return (s.replace('\\"', '"')
             .replace('\\u0026', '&')
             .replace('\\n', ' ')
             .replace('\\/', '/'))


def _parse_section(section_path):
    url = f"https://www.anthropic.com/{section_path}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    html = _unescape(resp.text)
    seen = {}
    for m in POST_RE.finditer(html):
        slug = m["slug"]
        if slug in seen:
            continue
        try:
            published = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        except ValueError:
            continue
        summary = m["summary"].strip()
        if len(summary) > 500:
            summary = summary[:500] + "..."
        seen[slug] = {
            "link": f"https://www.anthropic.com/{section_path}/{slug}",
            "title": m["title"].strip(),
            "summary": summary,
            "published": published,
        }
    return list(seen.values())


def fetch_anthropic(conn):
    """Fetch Anthropic news/engineering/research posts into the articles table."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    for feed_title, section_path in SECTIONS:
        log.info(f"  Fetching: {feed_title} (anthropic.com scraper)...")
        try:
            posts = _parse_section(section_path)
        except Exception as e:
            log.warning(f"    Error fetching {feed_title}: {e}")
            continue
        for p in posts:
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, feed, folder, title, authors, summary, published, fetched_at, source_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p["link"], feed_title, FOLDER, p["title"], "",
                     p["summary"], p["published"].isoformat(), now, "rss"),
                )
                if cur.rowcount > 0:
                    new_count += 1
            except sqlite3.IntegrityError:
                pass
    conn.commit()
    return new_count
