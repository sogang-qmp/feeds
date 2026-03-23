"""Article recommendation via Claude LLM with web search."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from llm import run_claude, extract_json
from scoring import build_profile_text


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
    # Prune old entries
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in history.items() if k >= cutoff}
    path = base_dir / "recommendations_history.json"
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


def recommend_articles(profile, base_dir):
    """Use Claude Code (sonnet + web search) to find recommended papers."""
    profile_text = build_profile_text(profile)
    history = _load_recommendation_history(base_dir)
    recent_urls = []
    for urls in history.values():
        recent_urls.extend(urls)

    dedup_note = ""
    if recent_urls:
        dedup_note = f"\n\nDo NOT recommend any of these previously recommended URLs:\n" + "\n".join(recent_urls[-60:])

    prompt = f"""You are a research literature assistant for a computational quantum materials physicist.

## Researcher Profile
{profile_text}

## Task
Search the web for articles relevant to this researcher and recommend them in three tiers:

| Tier | Count | Criteria |
|---|---|---|
| **recent** | 5 | Published 2024–2026, on active research frontiers |
| **classic** | 5 | Foundational papers underpinning the methods or materials |
| **exploratory** | 2 | Outside the researcher's current scope but high-potential intersection — find surprising connections |

## Selection Rules
- Prefer papers hitting >=2 profile topics simultaneously
- Flag any paper directly relevant to ongoing code/infrastructure (JAX ab initio, surface states, image potential states)
- Every paper must include a working DOI or arXiv link — verify the URL resolves before including it. If you cannot verify, exclude the paper.
{dedup_note}

## Output Format
Return ONLY a JSON array. Each element:
- "tier": "recent" | "classic" | "exploratory"
- "authors": abbreviated author string (e.g. "Kim et al.")
- "title": paper title
- "ref": journal/arXiv reference (e.g. "Phys. Rev. B 109, 045123")
- "year": integer
- "url": verified DOI or arXiv URL
- "why": one sentence on why it's relevant

Return ONLY the JSON array, no other text."""

    result_text = run_claude(prompt, model="sonnet", timeout=600)
    recommendations = extract_json(result_text)

    if not isinstance(recommendations, list):
        raise ValueError(f"Expected JSON array, got {type(recommendations).__name__}")

    # Save to history
    today = datetime.now().strftime("%Y-%m-%d")
    new_urls = [r.get("url", "") for r in recommendations if r.get("url")]
    history[today] = new_urls
    _save_recommendation_history(base_dir, history)

    return recommendations
