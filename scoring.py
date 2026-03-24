"""Article scoring via Claude LLM."""

import json
import logging

from llm import run_claude, extract_json

log = logging.getLogger("feeds")

BATCH_SIZE = 200


def build_profile_text(profile):
    """Build profile text for scoring prompt."""
    researcher = profile.get("researcher", {})
    areas = profile.get("research_areas", {})
    keywords = profile.get("keywords", {})

    def flatten(obj):
        if isinstance(obj, list):
            items = []
            for item in obj:
                if isinstance(item, (dict, list)):
                    items.extend(flatten(item))
                else:
                    items.append(str(item))
            return items
        if isinstance(obj, dict):
            items = []
            for v in obj.values():
                items.extend(flatten(v))
            return items
        return [str(obj)]

    text = (
        f"Name: {researcher.get('name', 'N/A')}\n"
        f"Position: {researcher.get('position', 'N/A')}\n"
        f"Affiliation: {researcher.get('affiliation', 'N/A')}\n"
        f"Research areas: {', '.join(flatten(areas))}\n"
        f"Keywords: {', '.join(flatten(keywords))}\n"
    )

    # Current interests get prominent placement
    interests = profile.get("current_interests", [])
    if interests:
        text += "\nCurrent interests (score these HIGHER):\n"
        for ci in interests:
            weight = ci.get("weight", "medium").upper()
            topic = ci.get("topic", "")
            examples = ci.get("examples", [])
            text += f"  [{weight} PRIORITY] {topic}\n"
            if examples:
                text += f"    Examples: {', '.join(examples)}\n"

    scoring_prompt = profile.get("scoring_prompt", "")
    if scoring_prompt:
        text += f"\nScoring guidance:\n{scoring_prompt}"
    return text


def _score_batch(batch, profile_text):
    """Score a single batch of articles via Claude Code subprocess."""
    articles_text = ""
    for i, a in enumerate(batch):
        articles_text += (
            f"\n[{i}] Feed: {a['feed']}\n"
            f"    Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Summary: {a['summary']}\n"
        )

    prompt = f"""You are a research assistant. Score EVERY article by relevance to this researcher's interests.

## Researcher Profile
{profile_text}

## Articles
{articles_text}

## Instructions
Score each article from 1 to 5:
  5 = directly related to researcher's core topics
  4 = closely related
  3 = somewhat related
  2 = tangentially related
  1 = not related

Return a JSON array with ALL {len(batch)} articles. Each element:
- "index": article index number
- "score": integer 1-5

You MUST include every article. Return ONLY the JSON array."""

    result_text = run_claude(prompt, model="haiku", timeout=300)
    scores = extract_json(result_text)
    if not isinstance(scores, list):
        raise ValueError(f"Expected JSON array, got {type(scores).__name__}")

    score_map = {}
    for s in scores:
        if isinstance(s, dict) and "index" in s and "score" in s:
            score_map[s["index"]] = s["score"]
    return score_map


def score_articles(articles, profile, config, notify_fn=None):
    """Use Claude Code to score every article on a 1-5 scale, batching if needed."""
    if not articles:
        return []

    profile_text = build_profile_text(profile)

    scored = []
    failed_batches = 0
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start:start + BATCH_SIZE]
        batch_num = start // BATCH_SIZE + 1
        log.info(f"  Scoring batch {batch_num}/{total_batches} ({len(batch)} articles)...")

        try:
            score_map = _score_batch(batch, profile_text)
        except Exception as e:
            log.warning(f"  Batch {batch_num} failed: {e}")
            score_map = {}
            failed_batches += 1
        for i, a in enumerate(batch):
            scored.append({**a, "score": score_map.get(i, 1)})

    if failed_batches:
        msg = f"{failed_batches}/{total_batches} batch(es) failed — affected articles scored as 1"
        log.warning(f"  {msg}")
        if notify_fn:
            notify_fn(config, f"Scoring partial failure: {msg}")

    return scored


def sort_by_opml(scored, feeds):
    """Sort scored articles by OPML folder/feed order, then score desc."""
    feed_order = {}
    for i, f in enumerate(feeds):
        key = (f["folder"], f["title"])
        if key not in feed_order:
            feed_order[key] = i

    scored.sort(key=lambda r: (feed_order.get((r["folder"], r["feed"]), 999), -r["score"]))
    return scored
