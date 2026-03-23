"""Literature search via Semantic Scholar API."""

import logging
import time
from datetime import datetime

import requests

log = logging.getLogger("feeds")


def generate_queries(profile):
    """Generate 10-15 search queries from research profile keywords.

    Strategy:
    - Top 8 strong keywords as standalone queries
    - Cross-topic pairs from strong keywords (every 3rd pair)
    - 2-3 abbreviated primary research areas
    - Max 15 queries total
    """
    queries = []

    keywords = profile.get("keywords", {})
    strong = keywords.get("strong", [])

    # Top 8 strong keywords as standalone queries
    for kw in strong[:8]:
        queries.append(kw)

    # Cross-topic pairs from strong keywords (every 3rd pair)
    pairs = []
    for i in range(len(strong)):
        for j in range(i + 1, len(strong)):
            pairs.append((strong[i], strong[j]))
    for idx, (a, b) in enumerate(pairs):
        if idx % 3 == 0:
            queries.append(f"{a} {b}")
        if len(queries) >= 13:
            break

    # Add 2-3 abbreviated primary research areas
    areas = profile.get("research_areas", {}).get("primary", [])
    for area in areas[:3]:
        # Abbreviate: take first few words if long
        words = area.split()
        if len(words) > 4:
            abbreviated = " ".join(words[:4])
        else:
            abbreviated = area
        if abbreviated not in queries:
            queries.append(abbreviated)
        if len(queries) >= 15:
            break

    return queries[:15]


def search_semantic_scholar(query, limit=20, year_range="2024-2026", api_key=None):
    """Search Semantic Scholar API for papers matching query.

    Returns list of article dicts ready for DB insertion.
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": limit,
        "fields": "title,abstract,authors,year,externalIds,url,citationCount,venue",
        "year": year_range,
    }
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"[literature] Semantic Scholar search failed for '{query}': {e}")
        return []

    papers = data.get("data", [])
    results = []
    for paper in papers:
        ext_ids = paper.get("externalIds") or {}
        doi = ext_ids.get("DOI")
        arxiv_id = ext_ids.get("ArXiv")

        # Build link: prefer DOI, fallback arXiv, then S2 URL
        if doi:
            link = f"https://doi.org/{doi}"
        elif arxiv_id:
            link = f"https://arxiv.org/abs/{arxiv_id}"
        else:
            link = paper.get("url", "")

        if not link:
            continue

        # Normalize authors: first 5 + "et al."
        raw_authors = paper.get("authors") or []
        author_names = [a.get("name", "") for a in raw_authors if a.get("name")]
        if len(author_names) > 5:
            authors_str = ", ".join(author_names[:5]) + " et al."
        else:
            authors_str = ", ".join(author_names)

        # Truncate abstract to 500 chars
        abstract = paper.get("abstract") or ""
        if len(abstract) > 500:
            abstract = abstract[:500] + "..."

        venue = paper.get("venue") or ""

        results.append({
            "link": link,
            "title": paper.get("title") or "",
            "authors": authors_str,
            "summary": abstract,
            "published": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "venue": venue,
            "year": paper.get("year"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "citation_count": paper.get("citationCount") or 0,
            "source_type": "literature",
            "feed": venue or "Semantic Scholar",
            "folder": "",
        })

    return results


def fetch_literature(profile, conn, config=None):
    """Fetch literature from Semantic Scholar and insert into DB.

    Args:
        profile: Research profile dict (from research_profile.yaml)
        conn: SQLite connection
        config: Optional config dict for API key and settings

    Returns:
        Count of new papers inserted
    """
    config = config or {}
    lit_cfg = config.get("literature", {})
    api_key = lit_cfg.get("semantic_scholar_api_key")
    year_range = lit_cfg.get("year_range", "2024-2026")
    max_results = lit_cfg.get("max_results_per_query", 20)

    queries = generate_queries(profile)
    log.info(f"[literature] Searching {len(queries)} queries...")

    inserted = 0
    for i, query in enumerate(queries):
        papers = search_semantic_scholar(
            query, limit=max_results, year_range=year_range, api_key=api_key
        )
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
                log.warning(f"[literature] Failed to insert paper '{paper.get('title', '')}': {e}")
        conn.commit()

        # Rate limit between queries
        if i < len(queries) - 1:
            time.sleep(1.5)

    log.info(f"[literature] Done. {inserted} new papers inserted.")
    return inserted
