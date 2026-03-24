"""Literature search via OpenAlex API.

OpenAlex provides free access to scholarly metadata with generous rate limits
(10,000 requests/day with polite pool). No API key required.
"""

import logging
from datetime import datetime

import requests

log = logging.getLogger("feeds")

OPENALEX_API = "https://api.openalex.org/works"
MAILTO = "youngwoo9202@gmail.com"  # polite pool — higher rate limits


def generate_queries(profile):
    """Generate 12-18 search queries from research profile.

    Strategy:
    - Current interests: topic + examples as queries (high weight first)
    - Top 6 strong keywords as standalone queries
    - Cross-topic pairs from strong keywords (every 3rd pair)
    - 2 abbreviated primary research areas
    - Max 18 queries total
    """
    queries = []

    # Current interests first (highest priority)
    interests = profile.get("current_interests", [])
    high = [ci for ci in interests if ci.get("weight") == "high"]
    medium = [ci for ci in interests if ci.get("weight") != "high"]
    for ci in high + medium:
        topic = ci.get("topic", "")
        if topic:
            queries.append(topic)
        for ex in ci.get("examples", [])[:2]:
            queries.append(ex)

    keywords = profile.get("keywords", {})
    strong = keywords.get("strong", [])

    # Top 6 strong keywords as standalone queries
    for kw in strong[:6]:
        queries.append(kw)

    # Cross-topic pairs from strong keywords (every 3rd pair)
    pairs = []
    for i in range(len(strong)):
        for j in range(i + 1, len(strong)):
            pairs.append((strong[i], strong[j]))
    for idx, (a, b) in enumerate(pairs):
        if idx % 3 == 0:
            queries.append(f"{a} {b}")
        if len(queries) >= 16:
            break

    # Add 2 abbreviated primary research areas
    areas = profile.get("research_areas", {}).get("primary", [])
    for area in areas[:2]:
        words = area.split()
        abbreviated = " ".join(words[:4]) if len(words) > 4 else area
        if abbreviated not in queries:
            queries.append(abbreviated)

    return queries[:18]


def _reconstruct_abstract(inverted_index):
    """Reconstruct abstract text from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    # inverted_index = {"word": [pos1, pos2], ...}
    words = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    if not words:
        return ""
    max_pos = max(words.keys())
    return " ".join(words.get(i, "") for i in range(max_pos + 1))


def search_openalex(query, per_page=25, year_from=2024, mailto=MAILTO, sort="relevance_score:desc"):
    """Search OpenAlex for papers matching query.

    Args:
        sort: OpenAlex sort field. Default relevance_score:desc.
              Use "cited_by_count:desc" for high-citation classics.
    """
    params = {
        "search": query,
        "per_page": per_page,
        "filter": f"publication_year:>{year_from - 1}",
        "sort": sort,
        "mailto": mailto,
    }

    try:
        resp = requests.get(OPENALEX_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"[literature] OpenAlex search failed for '{query}': {e}")
        return []

    results = []
    for work in data.get("results", []):
        doi_url = work.get("doi") or ""
        doi = doi_url.replace("https://doi.org/", "") if doi_url else ""

        # Extract arXiv ID from locations
        arxiv_id = ""
        for loc in work.get("locations") or []:
            landing = loc.get("landing_page_url") or ""
            if "arxiv.org" in landing:
                # Extract ID from URL like https://arxiv.org/abs/2401.12345
                parts = landing.rstrip("/").split("/")
                if parts:
                    arxiv_id = parts[-1]
                break

        # Build link: prefer DOI, fallback arXiv, then OpenAlex
        if doi_url:
            link = doi_url
        elif arxiv_id:
            link = f"https://arxiv.org/abs/{arxiv_id}"
        else:
            link = work.get("id", "")  # OpenAlex URL

        if not link:
            continue

        # Authors: first 5 + "et al."
        authorships = work.get("authorships") or []
        author_names = []
        for a in authorships:
            name = a.get("author", {}).get("display_name", "")
            if name:
                author_names.append(name)
        if len(author_names) > 5:
            authors_str = ", ".join(author_names[:5]) + " et al."
        else:
            authors_str = ", ".join(author_names)

        # Abstract: reconstruct from inverted index
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
        if len(abstract) > 500:
            abstract = abstract[:500] + "..."

        # Venue: primary source
        venue = ""
        primary_loc = work.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        venue = source.get("display_name") or ""

        year = work.get("publication_year")
        cites = work.get("cited_by_count") or 0

        results.append({
            "link": link,
            "title": work.get("title") or "",
            "authors": authors_str,
            "summary": abstract,
            "published": work.get("publication_date") or f"{year}-01-01",
            "venue": venue,
            "year": year,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "citation_count": cites,
            "source_type": "literature",
            "feed": venue or "OpenAlex",
            "folder": "",
        })

    return results


def fetch_literature(profile, conn, config=None):
    """Fetch literature from OpenAlex and insert into DB.

    Args:
        profile: Research profile dict (from research_profile.yaml)
        conn: SQLite connection
        config: Optional config dict for settings

    Returns:
        Count of new papers inserted
    """
    config = config or {}
    lit_cfg = config.get("literature", {})
    year_range = lit_cfg.get("year_range", "2024-2026")
    max_results = lit_cfg.get("max_results_per_query", 25)

    # Parse year_from from year_range like "2024-2026"
    try:
        year_from = int(year_range.split("-")[0])
    except (ValueError, IndexError):
        year_from = 2024

    queries = generate_queries(profile)
    log.info(f"[literature] Searching {len(queries)} queries via OpenAlex...")

    inserted = 0
    for i, query in enumerate(queries):
        log.info(f"  [{i+1}/{len(queries)}] '{query}'")
        papers = search_openalex(query, per_page=max_results, year_from=year_from)

        for paper in papers:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, title, authors, summary, published, fetched_at, curated,
                        source_type, feed, folder, doi, arxiv_id, citation_count, venue, year)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        paper["link"],
                        paper["title"],
                        paper["authors"],
                        paper["summary"],
                        paper["published"],
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                        paper["source_type"],
                        paper["feed"],
                        paper["folder"],
                        paper["doi"],
                        paper["arxiv_id"],
                        paper["citation_count"],
                        paper["venue"],
                        paper["year"],
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception as e:
                log.warning(f"[literature] Insert failed: {e}")
        conn.commit()

    log.info(f"[literature] Done. {inserted} new papers inserted.")
    return inserted
