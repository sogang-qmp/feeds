"""HTML generation and deployment for feed pages."""

import logging
from collections import OrderedDict
from pathlib import Path

from sources.rss import parse_opml

log = logging.getLogger("feeds")



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


def _render_rss_section(articles):
    """Render RSS articles grouped by folder/feed as a table."""
    folders = OrderedDict()
    for a in articles:
        folder = a.get("folder", "")
        feed = a["feed"]
        folders.setdefault(folder, OrderedDict())
        folders[folder].setdefault(feed, []).append(a)

    rows = ""
    for folder_name, feeds in folders.items():
        if folder_name:
            rows += f'<tr class="folder-row"><td colspan="3" class="folder">{folder_name}</td></tr>\n'
        for feed_name, feed_articles in feeds.items():
            count = len(feed_articles)
            rows += f'<tr class="feed-row"><td colspan="3" class="feed">{feed_name} <span class="count">({count})</span></td></tr>\n'
            for a in feed_articles:
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

    return f"<table>{rows}</table>"


def _render_literature_section(recommendations):
    """Render LLM-curated paper recommendations as the Literature tab.

    Uses the same tier-based layout (Recent Articles, Classic Foundations, Exploratory)
    that was previously shown as "Recommended Reads" above the tabs.
    """
    if not recommendations:
        return '<p class="empty-tab">No literature recommendations today.</p>'

    tiers = [
        ("recent", "Recent Articles", "#0d7377", "#e6f7f7"),
        ("classic", "Classic Foundations", "#b8860b", "#fdf6e3"),
        ("exploratory", "Exploratory", "#6a1b9a", "#f3e5f5"),
    ]

    html = ""
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

        html += f"""<div class="rec-tier" style="border-left: 4px solid {accent}; background: {bg};">
  <div class="rec-tier-label" style="color: {accent};">{tier_label}</div>
  {items}
</div>
"""

    return html


def _render_github_section(articles):
    """Render GitHub repos grouped by score descending, velocity within groups."""
    by_score = {}
    for a in articles:
        s = a.get("score", 1)
        by_score.setdefault(s, []).append(a)

    html = ""
    for score in sorted(by_score.keys(), reverse=True):
        items = sorted(by_score[score], key=lambda a: a.get("velocity") or 0, reverse=True)
        html += f'<h3 class="score-group">Score {score} <span class="count">({len(items)})</span></h3>\n'
        for a in items:
            title = a.get("title", "")
            link = a.get("link", "")
            description = a.get("summary", "") or a.get("description", "") or ""
            stars = a.get("stars", "")
            language = a.get("language", "") or ""
            trending = a.get("trending_category", "")
            title_html = f'<a href="{link}" target="_blank" rel="noopener" class="gh-repo">{title}</a>' if link else f'<span class="gh-repo">{title}</span>'

            trend_badge = ""
            if trending == "hot":
                trend_badge = '<span class="trend-badge" title="Trending fast">&#128293;</span>'
            elif trending == "rising":
                trend_badge = '<span class="trend-badge" title="Rising">&#128200;</span>'

            meta_parts = []
            if description:
                meta_parts.append(description)
            if stars not in ("", None):
                meta_parts.append(f"&#9733;{stars}")
            if language:
                meta_parts.append(language)
            meta_html = " &middot; ".join(meta_parts)

            html += f'<div class="gh-item">'
            html += f'<span class="score-badge s{score}">{score}</span>'
            html += f'<div class="gh-detail"><div class="gh-title">{trend_badge}{title_html}</div>'
            if meta_html:
                html += f'<div class="gh-meta">{meta_html}</div>'
            html += f'</div></div>\n'

    return html


def generate_html(scored_articles, today, ga_id="", recommendations=None):
    """Generate static HTML page with tabbed sections: RSS, Literature, GitHub.

    Literature tab shows LLM-curated recommendations (~10 papers).
    RSS tab shows scored feed articles.
    GitHub tab shows profile-matched repos.
    """
    # Split articles by source_type
    rss_articles = [a for a in scored_articles if a.get("source_type", "rss") == "rss"]
    gh_articles = [a for a in scored_articles if a.get("source_type") == "github"]

    has_lit = recommendations and len(recommendations) > 0
    has_gh = len(gh_articles) > 0
    has_tabs = has_lit or has_gh

    # Lit count = number of recommendations
    lit_count = len(recommendations) if recommendations else 0

    # Build subtitle with counts
    count_parts = []
    if rss_articles:
        count_parts.append(f"{len(rss_articles)} articles")
    if lit_count:
        count_parts.append(f"{lit_count} papers")
    if gh_articles:
        count_parts.append(f"{len(gh_articles)} repos")
    subtitle = " &middot; ".join(count_parts)

    # Build main content
    if has_tabs:
        rss_content = _render_rss_section(rss_articles) if rss_articles else '<p class="empty-tab">No RSS articles today.</p>'
        lit_content = _render_literature_section(recommendations)
        gh_content = _render_github_section(gh_articles) if gh_articles else '<p class="empty-tab">No GitHub repos today.</p>'

        main_content = f"""<div class="tabs-wrapper">
<input type="radio" id="tab-rss" name="tabs" checked>
<label for="tab-rss">RSS <span class="tab-count">{len(rss_articles)}</span></label>
<input type="radio" id="tab-lit" name="tabs">
<label for="tab-lit">Literature <span class="tab-count">{lit_count}</span></label>
<input type="radio" id="tab-gh" name="tabs">
<label for="tab-gh">GitHub <span class="tab-count">{len(gh_articles)}</span></label>
<div class="tab-content" id="content-rss">{rss_content}</div>
<div class="tab-content" id="content-lit">{lit_content}</div>
<div class="tab-content" id="content-gh">{gh_content}</div>
</div>"""
    else:
        # RSS-only: render as before (no tabs)
        main_content = _render_rss_section(rss_articles)

    # Tab CSS (only needed when tabs are present)
    tab_css = ""
    if has_tabs:
        tab_css = """
/* --- Tabs --- */
.tabs-wrapper { margin-top: 1.5em; }
.tabs-wrapper input[type="radio"] { display: none; }
.tabs-wrapper label {
  display: inline-block; padding: 8px 18px; cursor: pointer;
  font-family: system-ui, -apple-system, sans-serif; font-size: 0.95em;
  color: var(--muted); border-bottom: 3px solid transparent;
  margin-right: 4px; transition: color 0.15s, border-color 0.15s;
}
.tabs-wrapper label:hover { color: var(--fg); }
.tab-count {
  font-size: 0.8em; color: var(--muted); font-family: 'JetBrains Mono', monospace;
  margin-left: 2px;
}
#tab-rss:checked ~ label[for="tab-rss"],
#tab-lit:checked ~ label[for="tab-lit"],
#tab-gh:checked ~ label[for="tab-gh"] {
  color: var(--fg); border-bottom-color: var(--accent); font-weight: 600;
}
.tab-content { display: none; padding-top: 1em; }
#tab-rss:checked ~ #content-rss { display: block; }
#tab-lit:checked ~ #content-lit { display: block; }
#tab-gh:checked ~ #content-gh { display: block; }
.empty-tab { color: var(--muted); font-style: italic; }
/* --- Literature items --- */
.score-group { font-family: system-ui, -apple-system, sans-serif; font-size: 1em; margin: 1.2em 0 0.5em; color: var(--fg); }
.lit-item, .gh-item { display: flex; gap: 8px; align-items: baseline; padding: 4px 0; }
.lit-item + .lit-item, .gh-item + .gh-item { border-top: 1px solid #eee; }
.score-badge {
  font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.85em;
  min-width: 1.8em; text-align: center; padding: 2px 4px; border-radius: 3px;
  flex-shrink: 0;
}
.lit-detail, .gh-detail { flex: 1; min-width: 0; }
.lit-title, .gh-title { font-size: 0.92em; line-height: 1.4; }
.lit-meta, .gh-meta { font-size: 0.8em; color: var(--muted); margin-top: 2px; }
.gh-repo { font-family: 'JetBrains Mono', monospace; font-size: 0.95em; }
.trend-badge { margin-right: 4px; }
"""

    subtitle_html = f'<p class="subtitle">{subtitle}</p>' if subtitle else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Feeds &mdash; {today}</title>
{_ga_snippet(ga_id)}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #fafaf8; --fg: #1a1a1a; --muted: #666;
  --accent: #1a0dab; --visited: #681da8;
}}
body {{ font-family: Georgia, Charter, serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: var(--fg); background: var(--bg); }}
h1 {{ font-family: system-ui, -apple-system, sans-serif; font-size: 1.3em; margin-bottom: 0.2em; }}
.subtitle {{ font-family: system-ui, -apple-system, sans-serif; font-size: 0.85em; color: var(--muted); margin-top: 0; }}
/* --- Literature tier items --- */
.rec-tier {{ padding: 12px 16px; margin-bottom: 12px; border-radius: 6px; }}
.rec-tier-label {{ font-weight: 700; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
.rec-item {{ padding: 6px 0; }}
.rec-item + .rec-item {{ border-top: 1px solid rgba(0,0,0,0.06); }}
.rec-title {{ font-size: 0.92em; line-height: 1.4; }}
.rec-title a {{ color: var(--accent); text-decoration: none; }}
.rec-title a:visited {{ color: var(--visited); }}
.rec-title a:hover {{ text-decoration: underline; }}
.rec-meta {{ font-size: 0.8em; color: #555; margin-top: 2px; }}
.rec-why {{ font-size: 0.8em; color: var(--muted); font-style: italic; margin-top: 2px; }}
/* --- Feed table --- */
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 4px 8px; vertical-align: top; font-size: 0.9em; }}
.folder-row td {{ padding-top: 28px; }}
.folder {{ font-weight: bold; font-size: 1.1em; color: #007bff; padding: 0; }}
.feed-row td {{ padding-top: 16px; }}
.feed {{ font-weight: bold; font-size: 0.95em; padding: 4px 0; }}
.count {{ font-weight: normal; color: #888; font-size: 0.85em; }}
.score {{ text-align: center; font-weight: bold; width: 2em; font-family: 'JetBrains Mono', monospace; }}
.s5 {{ background: #c62828; color: #fff; }}
.s4 {{ background: #ef6c00; color: #fff; }}
.s3 {{ background: #f9a825; }}
.s2 {{ background: #e0e0e0; }}
.s1 {{ background: #f0f0ee; color: #999; }}
.authors {{ display: block; font-size: 0.85em; color: #000; }}
a {{ color: var(--accent); text-decoration: none; }}
a:visited {{ color: var(--visited); }}
a:hover {{ text-decoration: underline; }}
{tab_css}
@media (max-width: 600px) {{
  body {{ margin: 1em auto; padding: 0 0.5em; }}
  .score {{ width: 1.5em; font-size: 0.85em; }}
  td {{ padding: 3px 4px; font-size: 0.85em; }}
  .rec-tier {{ padding: 10px 12px; }}
  .tabs-wrapper label {{ padding: 6px 12px; font-size: 0.9em; }}
}}
</style></head><body>
<p><a href="index.html">&larr; All feeds</a></p>
<h1>Feeds &mdash; {today}</h1>
{subtitle_html}
{main_content}
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
