"""Article recommendation via OpenAlex candidate collection + haiku curation."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from llm import run_claude, extract_json
from scoring import build_profile_text
from sources.literature import generate_queries, search_openalex

log = logging.getLogger("feeds")


def _load_recommendation_history(base_dir):
    """Load recommendation history to avoid repeating papers."""
    path = base_dir / "recommendations_history.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_recommendation_history(base_dir, history):
    """Save recommendation history, keeping last 30 days."""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in history.items() if k >= cutoff}
    path = base_dir / "recommendations_history.json"
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


def _collect_candidates(profile, config=None):
    """Collect candidate papers from OpenAlex.

    Two pools:
    - Recent: publication_year >= 2024, relevance sorted (for recent + exploratory tiers)
    - Classic: no year limit, cited_by_count sorted (for classic tier)

    Returns deduplicated list of paper dicts.
    """
    config = config or {}
    lit_cfg = config.get("literature", {})
    max_per_query = lit_cfg.get("max_results_per_query", 20)

    queries = generate_queries(profile)
    log.info(f"[recommend] Collecting candidates from {len(queries)} queries...")

    seen_links = set()
    candidates = []

    # Pool 1: Recent papers (2024+)
    for q in queries:
        papers = search_openalex(q, per_page=max_per_query, year_from=2024)
        for p in papers:
            if p["link"] not in seen_links:
                seen_links.add(p["link"])
                candidates.append(p)

    # Pool 2: Classic high-citation papers (no year limit, sorted by citations)
    classic_queries = queries[:5]
    for q in classic_queries:
        papers = search_openalex(q, per_page=10, year_from=1990, sort="cited_by_count:desc")
        for p in papers:
            if p["link"] not in seen_links:
                seen_links.add(p["link"])
                candidates.append(p)

    log.info(f"[recommend] Collected {len(candidates)} unique candidates.")
    return candidates


def _curate_candidates(candidates, profile, recent_urls):
    """Use haiku to select top 12 papers from candidates and assign tiers.

    Args:
        candidates: List of paper dicts from OpenAlex
        profile: Research profile dict
        recent_urls: URLs to exclude (previously recommended)

    Returns:
        List of 12 recommendation dicts with tier, title, authors, url, why, etc.
    """
    # Filter out previously recommended
    if recent_urls:
        url_set = set(recent_urls)
        candidates = [c for c in candidates if c["link"] not in url_set]

    if not candidates:
        return []

    # Prepare candidate text for haiku (limit to top 100 by citation)
    candidates_sorted = sorted(candidates, key=lambda c: c.get("citation_count", 0), reverse=True)
    pool = candidates_sorted[:100]

    profile_text = build_profile_text(profile)

    candidates_text = ""
    for i, c in enumerate(pool):
        candidates_text += (
            f"\n[{i}] Title: {c['title']}\n"
            f"    Authors: {c['authors']}\n"
            f"    Year: {c.get('year', 'N/A')} | Citations: {c.get('citation_count', 0)}\n"
            f"    Venue: {c.get('venue', 'N/A')}\n"
            f"    Abstract: {c.get('summary', '')[:300]}\n"
            f"    URL: {c['link']}\n"
        )

    prompt = f"""You are a research literature curator for a computational physicist.

## Researcher Profile
{profile_text}

## Candidate Papers
{candidates_text}

## Task
Select exactly 12 papers from the candidates above and assign each to a tier:

| Tier | Count | Criteria |
|---|---|---|
| **recent** | 5 | Published 2024-2026, on active research topics matching current interests |
| **classic** | 5 | Foundational/highly-cited papers underpinning the researcher's methods |
| **exploratory** | 2 | Outside current scope but high-potential intersection — surprising connections |

## Selection Rules
- STRONGLY prefer papers matching current interests (especially HIGH PRIORITY ones)
- Prefer papers hitting >= 2 profile topics simultaneously
- For recent tier: prefer 2025-2026 publications
- For classic tier: prefer highly-cited foundational works
- For exploratory: find genuinely surprising cross-discipline connections

## Output Format
Return ONLY a JSON array of 12 elements. Each element:
- "index": candidate index number from the list above
- "tier": "recent" | "classic" | "exploratory"
- "why": one sentence explaining relevance

Return ONLY the JSON array."""

    result_text = run_claude(prompt, model="haiku", timeout=120)
    selections = extract_json(result_text)

    if not isinstance(selections, list):
        raise ValueError(f"Expected JSON array, got {type(selections).__name__}")

    # Map selections back to full paper data
    recommendations = []
    for sel in selections[:12]:
        idx = sel.get("index", -1)
        if idx < 0 or idx >= len(pool):
            continue
        paper = pool[idx]
        recommendations.append({
            "tier": sel.get("tier", "recent"),
            "title": paper["title"],
            "authors": paper["authors"],
            "ref": paper.get("venue", ""),
            "year": paper.get("year", ""),
            "url": paper["link"],
            "why": sel.get("why", ""),
        })

    return recommendations


def recommend_articles(profile, base_dir, config=None):
    """Collect OpenAlex candidates and curate top 12 via haiku.

    Replaces the old sonnet web-search approach with:
    1. OpenAlex API search (profile + current_interests driven queries)
    2. Haiku selects top 12 and assigns tiers
    """
    history = _load_recommendation_history(base_dir)
    recent_urls = []
    for urls in history.values():
        recent_urls.extend(urls)

    candidates = _collect_candidates(profile, config)

    if not candidates:
        log.warning("[recommend] No candidates found from OpenAlex.")
        return []

    recommendations = _curate_candidates(candidates, profile, recent_urls)

    # Save to history
    today = datetime.now().strftime("%Y-%m-%d")
    new_urls = [r.get("url", "") for r in recommendations if r.get("url")]
    history[today] = new_urls
    _save_recommendation_history(base_dir, history)

    return recommendations
