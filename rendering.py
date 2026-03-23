"""HTML generation and deployment for feed pages."""

import logging
from collections import OrderedDict
from pathlib import Path

from sources.rss import parse_opml

log = logging.getLogger("feeds")


def _recommendations_html(recommendations):
    """Generate HTML section for AI-recommended papers."""
    if not recommendations:
        return ""

    tiers = [
        ("recent", "Recent Frontiers", "#0d7377", "#e6f7f7"),
        ("classic", "Classic Foundations", "#b8860b", "#fdf6e3"),
        ("exploratory", "Exploratory", "#6a1b9a", "#f3e5f5"),
    ]

    sections = ""
    for tier_key, tier_label, accent, bg in tiers:
        papers = [r for r in recommendations if r.get("tier") == tier_key]
        if not papers:
            continue

        items = ""
        for p in papers:
            title = p.get("title", "")
            url = p.get("url", "")
            authors = p.get("authors", "")
            ref = p.get("ref", "")
            year = p.get("year", "")
            why = p.get("why", "")

            title_html = f'<a href="{url}" target="_blank" rel="noopener">{title}</a>' if url else title
            ref_str = f"{ref} ({year})" if ref else str(year)

            items += f"""<div class="rec-item">
  <div class="rec-title">{title_html}</div>
  <div class="rec-meta">{authors} &middot; {ref_str}</div>
  <div class="rec-why">{why}</div>
</div>
"""

        sections += f"""<div class="rec-tier" style="border-left: 4px solid {accent}; background: {bg};">
  <div class="rec-tier-label" style="color: {accent};">{tier_label}</div>
  {items}
</div>
"""

    return f"""<div class="recommendations">
  <h2 class="rec-heading">Recommended Reads</h2>
  {sections}
</div>
"""


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


def generate_html(scored_articles, today, ga_id="", recommendations=None):
    """Generate static HTML page with recommendations + articles grouped by folder/feed."""
    rec_html = _recommendations_html(recommendations) if recommendations else ""

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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Feeds — {today}</title>
{_ga_snippet(ga_id)}
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.3em; }}
/* --- Recommendations --- */
.recommendations {{ margin-bottom: 2.5em; }}
.rec-heading {{ font-size: 1.15em; margin-bottom: 0.8em; color: #333; }}
.rec-tier {{ padding: 12px 16px; margin-bottom: 12px; border-radius: 6px; }}
.rec-tier-label {{ font-weight: 700; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
.rec-item {{ padding: 6px 0; }}
.rec-item + .rec-item {{ border-top: 1px solid rgba(0,0,0,0.06); }}
.rec-title {{ font-size: 0.92em; line-height: 1.4; }}
.rec-title a {{ color: #1a0dab; text-decoration: none; }}
.rec-title a:visited {{ color: #681da8; }}
.rec-title a:hover {{ text-decoration: underline; }}
.rec-meta {{ font-size: 0.8em; color: #555; margin-top: 2px; }}
.rec-why {{ font-size: 0.8em; color: #666; font-style: italic; margin-top: 2px; }}
/* --- Feed table --- */
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
@media (max-width: 600px) {{
  body {{ margin: 1em auto; padding: 0 0.5em; }}
  .score {{ width: 1.5em; font-size: 0.85em; }}
  td {{ padding: 3px 4px; font-size: 0.85em; }}
  .rec-tier {{ padding: 10px 12px; }}
}}
</style></head><body>
<p><a href="index.html">&larr; All feeds</a></p>
<h1>Feeds &mdash; {today}</h1>
{rec_html}
<table>{rows}</table>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}},{{left:'\\\\(',right:'\\\\)',display:false}},{{left:'\\\\[',right:'\\\\]',display:true}}]}});">
</script>
</body></html>"""
    return html


def deploy_html(html, today, base_dir, ga_id="", feeds=None):
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
    if feeds is None:
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
