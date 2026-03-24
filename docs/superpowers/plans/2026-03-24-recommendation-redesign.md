# Recommendation Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the recommendation system to use `current_interests` from profile, OpenAlex+haiku for literature curation, and stars velocity for GitHub trending detection.

**Architecture:** Profile-driven dynamic query generation replaces hardcoded queries. Literature switches from sonnet web search to OpenAlex candidate pool + haiku curation. GitHub adds velocity calculation (stars/age) for trending detection. RSS scoring adds current_interests to prompt.

**Tech Stack:** Python 3.13, OpenAlex API, Claude haiku via CLI subprocess, SQLite, gh CLI

**Pre-existing state:** `sources/github.py` has uncommitted changes (AI physics queries updated to remove MLP terms). These will be replaced entirely in Task 6. Commit or stash existing changes before starting.

---

## Chunk 1: Profile Extension + Scoring

### Task 1: Add `current_interests` to research_profile.yaml

**Files:**
- Modify: `research_profile.yaml:147` (before `opportunity_filters`)

- [ ] **Step 1: Add current_interests section**

Add before the `opportunity_filters` section:

```yaml
# ============================================================
# Current Interests (dynamic — update frequently)
# These get extra weight in scoring and drive query generation.
# ============================================================
current_interests:
  - topic: "AI agents for scientific research (vibe physics)"
    weight: high
    examples:
      - "LLM autonomously conducting physics calculations"
      - "long-running Claude for scientific computing"
      - "agentic workflow for ab initio simulation"
  - topic: "agentic AI and autonomous research workflows"
    weight: high
    examples:
      - "Ralph loop, CLAUDE.md-driven research"
      - "AI grad student model"
      - "autonomous scientific discovery"
  - topic: "electron-phonon coupling in 2D materials"
    weight: medium
  - topic: "moiré physics and flat bands"
    weight: medium
```

- [ ] **Step 2: Verify YAML parses correctly**

Run: `python -c "import yaml; d=yaml.safe_load(open('research_profile.yaml')); print(len(d['current_interests']), 'interests'); [print(f'  {i[\"topic\"]} ({i[\"weight\"]})') for i in d['current_interests']]"`
Expected: 4 interests printed with topics and weights

- [ ] **Step 3: Commit**

```bash
git add research_profile.yaml
git commit -m "feat: add current_interests section to research profile"
```

### Task 2: Add `current_interests` to scoring prompt

**Files:**
- Modify: `scoring.py:13-45` (`build_profile_text`)
- Test: `test_main.py` (TestBuildProfileText)

- [ ] **Step 1: Write failing test**

Add to `test_main.py` in `TestBuildProfileText`:

```python
def test_includes_current_interests(self, sample_profile):
    sample_profile["current_interests"] = [
        {"topic": "AI agents for physics", "weight": "high",
         "examples": ["vibe physics", "long-running Claude"]},
        {"topic": "moiré physics", "weight": "medium"},
    ]
    text = build_profile_text(sample_profile)
    assert "AI agents for physics" in text
    assert "HIGH PRIORITY" in text or "high" in text.lower()
    assert "moiré physics" in text

def test_no_current_interests(self, sample_profile):
    text = build_profile_text(sample_profile)
    # Should not crash when current_interests is absent
    assert "N/A" in text or "Research areas" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_main.py::TestBuildProfileText::test_includes_current_interests -v`
Expected: FAIL — "AI agents for physics" not in text

- [ ] **Step 3: Implement — update build_profile_text**

In `scoring.py`, modify `build_profile_text` to append current_interests:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_main.py::TestBuildProfileText -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scoring.py test_main.py
git commit -m "feat: include current_interests in scoring prompt"
```

### Task 3: Update test fixtures with current_interests

**Files:**
- Modify: `test_main.py` (sample_profile fixture)

- [ ] **Step 1: Add current_interests to sample_profile fixture**

In `test_main.py`, update the `sample_profile` fixture:

```python
@pytest.fixture
def sample_profile():
    return {
        "researcher": {"name": "Test User", "position": "Professor", "affiliation": "Test U"},
        "research_areas": {
            "primary": ["condensed matter", "phonon physics"],
            "methods": ["DFT", "machine learning"],
        },
        "keywords": {
            "strong": ["phonon", "DFT"],
            "moderate": ["VASP"],
        },
        "current_interests": [
            {"topic": "AI agents for physics", "weight": "high",
             "examples": ["vibe physics", "autonomous simulation"]},
            {"topic": "moiré phonons", "weight": "medium"},
        ],
    }
```

- [ ] **Step 2: Run all tests to verify nothing breaks**

Run: `pytest test_main.py -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add test_main.py
git commit -m "test: add current_interests to test fixtures"
```

## Chunk 2: Literature Recommendation Redesign

### Task 4: Update literature query generation to use current_interests

**Files:**
- Modify: `sources/literature.py:18-60` (`generate_queries`)
- Test: `test_literature.py` (TestGenerateQueries)

- [ ] **Step 1: Write failing test**

Add to `test_literature.py` in `TestGenerateQueries`:

```python
def test_includes_current_interests_queries(self, sample_profile):
    sample_profile["current_interests"] = [
        {"topic": "AI agents for physics", "weight": "high",
         "examples": ["vibe physics", "autonomous simulation"]},
    ]
    queries = generate_queries(sample_profile)
    combined = " ".join(queries).lower()
    assert "ai agents" in combined or "vibe physics" in combined or "autonomous" in combined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_literature.py::TestGenerateQueries::test_includes_current_interests_queries -v`
Expected: FAIL

- [ ] **Step 3: Implement — update generate_queries in literature.py**

Replace the `generate_queries` function in `sources/literature.py`:

```python
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
    # Sort by weight: high first
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
```

- [ ] **Step 4: Update existing test assertions for new query limits**

In `test_literature.py`, update:
- `test_length_within_bounds`: change `assert 5 <= len(queries) <= 15` to `assert 5 <= len(queries) <= 18`
- `test_max_15_queries`: rename to `test_max_18_queries`, change assertion to `assert len(queries) <= 18`
- `test_contains_strong_keywords`: change `strong[:8]` to `strong[:6]` and `found >= 5` to `found >= 4`

- [ ] **Step 5: Run all literature tests**

Run: `pytest test_literature.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add sources/literature.py test_literature.py
git commit -m "feat: literature queries include current_interests"
```

### Task 5: Rewrite recommend.py — OpenAlex candidates + haiku curation

**Files:**
- Rewrite: `recommend.py` (full rewrite)
- Test: `test_recommend.py` (new file)

- [ ] **Step 1: Write tests for the new recommend module**

Create `test_recommend.py`:

```python
"""Tests for recommend.py — OpenAlex + haiku curation pipeline."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recommend import (
    _load_recommendation_history,
    _save_recommendation_history,
    _collect_candidates,
    _curate_candidates,
    recommend_articles,
)


@pytest.fixture
def sample_profile():
    return {
        "researcher": {"name": "Test User", "position": "Professor", "affiliation": "Test U"},
        "research_areas": {
            "primary": ["condensed matter", "phonon physics"],
            "methods": ["DFT"],
        },
        "keywords": {
            "strong": ["phonon", "DFT", "2D materials"],
            "moderate": ["VASP"],
        },
        "current_interests": [
            {"topic": "AI agents for physics", "weight": "high",
             "examples": ["vibe physics"]},
        ],
    }


@pytest.fixture
def mock_candidates():
    return [
        {"title": f"Paper {i}", "link": f"https://doi.org/10.1234/p{i}",
         "authors": "Smith et al.", "summary": f"Abstract {i}",
         "year": 2025, "citation_count": 10 * i, "venue": "PRL"}
        for i in range(20)
    ]


class TestRecommendationHistory:
    def test_load_missing_file(self, tmp_path):
        history = _load_recommendation_history(tmp_path)
        assert history == {}

    def test_save_and_load(self, tmp_path):
        history = {"2026-03-24": ["https://doi.org/10.1234/a"]}
        _save_recommendation_history(tmp_path, history)
        loaded = _load_recommendation_history(tmp_path)
        assert "2026-03-24" in loaded


class TestCollectCandidates:
    def test_returns_list_of_dicts(self, sample_profile):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            candidates = _collect_candidates(sample_profile)
        assert isinstance(candidates, list)

    def test_deduplicates_by_link(self, sample_profile):
        paper = {
            "id": "https://openalex.org/W111",
            "title": "Dup Paper",
            "doi": "https://doi.org/10.1234/dup",
            "publication_year": 2025,
            "publication_date": "2025-01-01",
            "cited_by_count": 5,
            "abstract_inverted_index": {"test": [0]},
            "authorships": [{"author": {"display_name": "Alice"}}],
            "primary_location": {"source": {"display_name": "Nature"}},
            "locations": [],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [paper, paper]}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            candidates = _collect_candidates(sample_profile)
        links = [c["link"] for c in candidates]
        assert len(links) == len(set(links))


class TestCurateCandidates:
    def test_returns_12_recommendations(self, sample_profile, mock_candidates):
        haiku_response = json.dumps([
            {"index": i, "tier": ["recent", "classic", "exploratory"][i % 3],
             "why": f"Relevant because {i}"}
            for i in range(12)
        ])
        with patch("recommend.run_claude", return_value=haiku_response):
            recs = _curate_candidates(mock_candidates, sample_profile, [])
        assert len(recs) == 12
        assert all("tier" in r for r in recs)
        assert all("why" in r for r in recs)

    def test_handles_haiku_failure(self, sample_profile, mock_candidates):
        with patch("recommend.run_claude", side_effect=RuntimeError("timeout")):
            with pytest.raises(RuntimeError):
                _curate_candidates(mock_candidates, sample_profile, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_recommend.py -v`
Expected: FAIL — `_collect_candidates` and `_curate_candidates` don't exist

- [ ] **Step 3a: Add `sort` parameter to `search_openalex`**

In `sources/literature.py`, update `search_openalex` signature and params:

```python
def search_openalex(query, per_page=25, year_from=2024, mailto=MAILTO, sort="relevance_score:desc"):
```

And change the params dict:

```python
    params = {
        "search": query,
        "per_page": per_page,
        "filter": f"publication_year:>{year_from - 1}",
        "sort": sort,
        "mailto": mailto,
    }
```

- [ ] **Step 3b: Implement — rewrite recommend.py**

```python
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
    classic_queries = queries[:5]  # Use top 5 queries for classics
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
```

- [ ] **Step 4: Run recommend tests**

Run: `pytest test_recommend.py -v`
Expected: all PASS

- [ ] **Step 5: Run all tests to check for regressions**

Run: `pytest -v`
Expected: all PASS (test_main.py::TestCmdCurate may need patching update — see next step)

- [ ] **Step 6: Fix test_main.py curate test if needed**

The `TestCmdCurate` test mocks `recommend.run_claude`. Since `recommend_articles` now also calls `search_openalex`, we need to mock that too. Update the mock in `test_main.py::TestCmdCurate::test_marks_articles_as_curated`:

```python
with patch("scoring.run_claude", return_value=scores_json), \
     patch("sources.literature.requests.get") as mock_get, \
     patch("recommend.run_claude", return_value='[]'):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"results": []}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp
    main.cmd_curate(args, tmp_dir, sample_config)
```

- [ ] **Step 7: Run all tests again**

Run: `pytest -v`
Expected: all PASS

- [ ] **Step 8: Update main.py to pass config to recommend_articles**

In `main.py:144`, change:

```python
recommendations = recommend_articles(profile, base_dir)
```

to:

```python
recommendations = recommend_articles(profile, base_dir, config)
```

- [ ] **Step 9: Commit**

```bash
git add recommend.py test_recommend.py test_main.py main.py
git commit -m "feat: rewrite literature recommendations — OpenAlex + haiku curation"
```

## Chunk 3: GitHub Redesign

### Task 6: Dynamic query generation from profile (remove hardcoded queries)

**Files:**
- Modify: `sources/github.py:10-110` (remove hardcoded lists, rewrite `generate_queries`)
- Test: `test_github.py` (TestGenerateQueries)

- [ ] **Step 1: Write failing test for dynamic query generation**

Add to `test_github.py` in `TestGenerateQueries`:

```python
def test_uses_current_interests(self):
    profile = {
        **SAMPLE_PROFILE,
        "current_interests": [
            {"topic": "AI agents for physics", "weight": "high",
             "examples": ["vibe physics", "autonomous simulation"]},
        ],
    }
    qs = generate_queries(profile)
    combined = " ".join(qs).lower()
    assert "ai agents" in combined or "vibe physics" in combined

def test_no_mlp_queries(self):
    """User doesn't want MLP/materials-prediction queries."""
    profile = {**SAMPLE_PROFILE, "current_interests": []}
    qs = generate_queries(profile)
    combined = " ".join(qs).lower()
    assert "interatomic potential" not in combined
    assert "force field" not in combined
    assert "materials prediction" not in combined
    assert "materials discovery" not in combined
```

- [ ] **Step 2: Run tests to check current state**

Run: `pytest test_github.py::TestGenerateQueries -v`

- [ ] **Step 3: Rewrite generate_queries to be fully dynamic**

Replace the entire top section of `sources/github.py` (hardcoded lists + generate_queries):

```python
"""GitHub repository search via gh CLI."""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger("feeds")


# Fallback queries when profile has no current_interests or keywords
_FALLBACK_QUERIES = [
    "DFT automation python",
    "ab initio phonon",
    "GW approximation code",
    "Bethe-Salpeter equation",
]


def generate_queries(profile):
    """Generate GitHub search queries dynamically from profile.

    Priority order:
    1. current_interests topics + examples (6 slots)
    2. Profile methods/tools from keywords (3 slots)
    3. Cross-topic from strong keywords (3 slots)
    Returns max 12 queries.
    """
    queries = []

    # 1. Current interests — topics and examples
    interests = profile.get("current_interests", []) if profile else []
    high = [ci for ci in interests if ci.get("weight") == "high"]
    medium = [ci for ci in interests if ci.get("weight") != "high"]
    for ci in high + medium:
        topic = ci.get("topic", "")
        if topic:
            queries.append(topic)
        for ex in ci.get("examples", [])[:2]:
            queries.append(ex)
        if len(queries) >= 6:
            break

    # 2. Tool/code queries from profile keywords
    tool_keywords = set()
    if profile and isinstance(profile, dict):
        methods = []
        areas = profile.get("research_areas", {})
        if isinstance(areas, dict):
            methods = areas.get("methods", [])
        kw = profile.get("keywords", {})
        if isinstance(kw, dict):
            for bucket in ("strong", "moderate"):
                for k in kw.get(bucket, []):
                    kl = k.lower()
                    if any(t in kl for t in ("vasp", "wannier", "berkeleygw",
                                              "dft automation", "ab initio")):
                        tool_keywords.add(k)
                    elif kl == "epw":
                        tool_keywords.add("EPW electron-phonon coupling")
        for m in methods:
            ml = m.lower()
            if any(t in ml for t in ("vasp", "wannier", "berkeleygw",
                                      "dft automation", "ab initio")):
                tool_keywords.add(m)

    tool_qs = sorted(tool_keywords)[:3]
    if not tool_qs and not queries:
        tool_qs = _FALLBACK_QUERIES[:3]
    queries.extend(tool_qs)

    # 3. Cross-topic pairs from strong keywords
    strong = []
    if profile and isinstance(profile, dict):
        kw = profile.get("keywords", {})
        if isinstance(kw, dict):
            strong = kw.get("strong", [])
    for i in range(0, len(strong) - 1, 3):
        if len(queries) >= 12:
            break
        queries.append(f"{strong[i]} {strong[i+1]}")

    # Fallback if we have very few queries
    if len(queries) < 4:
        queries.extend(_FALLBACK_QUERIES)

    # Deduplicate while preserving order, cap at 12
    seen = set()
    unique = []
    for q in queries:
        ql = q.lower()
        if ql not in seen:
            seen.add(ql)
            unique.append(q)
    return unique[:12]
```

- [ ] **Step 4: Update test assertions for new query generation**

In `test_github.py`, update `TestGenerateQueries`:
- `test_max_ten` → rename to `test_max_twelve`: `assert len(qs) <= 12`
- **DELETE** `test_contains_ai_physics_terms` (hardcoded AI queries no longer exist)
- Add replacement test:

```python
def test_max_twelve(self):
    qs = generate_queries(SAMPLE_PROFILE)
    assert len(qs) <= 12

def test_contains_profile_tools(self):
    qs = generate_queries(SAMPLE_PROFILE)
    combined = " ".join(qs).lower()
    # Should contain tool keywords from profile
    assert any(t in combined for t in ("vasp", "wannier", "berkeleygw", "epw", "dft"))
```

- [ ] **Step 5: Run all GitHub tests**

Run: `pytest test_github.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add sources/github.py test_github.py
git commit -m "feat: dynamic GitHub query generation from profile + current_interests"
```

### Task 7: Add velocity calculation and trending badges

**Files:**
- Modify: `sources/github.py` (`fetch_github`, `search_github_repos`)
- Modify: `db.py` (add `velocity`, `trending_category` columns)
- Modify: `rendering.py` (`_render_github_section`)
- Test: `test_github.py`

- [ ] **Step 1: Write failing test for velocity**

Add to `test_github.py`:

```python
from sources.github import compute_velocity

class TestComputeVelocity:
    def test_basic_velocity(self):
        vel, cat = compute_velocity(100, "2026-01-01T00:00:00Z")
        # 100 stars over ~83 days ≈ 1.2 stars/day → rising
        assert vel > 0
        assert cat in ("hot", "rising", "established")

    def test_hot_repo(self):
        # Created yesterday with 10 stars
        from datetime import datetime, timezone, timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        vel, cat = compute_velocity(10, yesterday)
        assert cat == "hot"

    def test_established_repo(self):
        vel, cat = compute_velocity(50, "2020-01-01T00:00:00Z")
        assert cat == "established"

    def test_missing_created_at(self):
        vel, cat = compute_velocity(100, "")
        assert cat == "established"
        assert vel == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_github.py::TestComputeVelocity -v`
Expected: FAIL — `compute_velocity` doesn't exist

- [ ] **Step 3: Implement compute_velocity**

Add to `sources/github.py`:

```python
def compute_velocity(stars, created_at):
    """Compute stars/day velocity and trending category.

    Returns (velocity_float, category_string).
    Categories:
      hot: > 2 stars/day
      rising: > 0.5 stars/day
      established: everything else
    """
    if not created_at or not stars:
        return 0.0, "established"

    try:
        # Parse ISO datetime
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days < 1:
            age_days = 1
        velocity = stars / age_days
    except (ValueError, TypeError):
        return 0.0, "established"

    if velocity > 2:
        category = "hot"
    elif velocity > 0.5:
        category = "rising"
    else:
        category = "established"

    return round(velocity, 2), category
```

- [ ] **Step 4: Run velocity tests**

Run: `pytest test_github.py::TestComputeVelocity -v`
Expected: all PASS

- [ ] **Step 5: Add DB columns for velocity**

In `db.py`, add to the `new_columns` list:

```python
("velocity", "REAL"),
("trending_category", "TEXT"),
```

- [ ] **Step 6: Update fetch_github to compute and store velocity**

In `sources/github.py`, modify `fetch_github` — in the insert loop, compute velocity before inserting:

```python
        for repo in recent_repos + alltime_repos:
            if repo["stars"] < min_stars:
                continue
            url = repo["link"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            velocity, trending_cat = compute_velocity(
                repo["stars"], repo.get("created_at", ""))

            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, feed, folder, title, authors, summary, published,
                        fetched_at, source_type, stars, language, owner, repo_name,
                        velocity, trending_category)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (url, repo["feed"], repo["folder"], repo["title"],
                     repo["authors"], repo["summary"], repo["published"],
                     now, "github",
                     repo["stars"], repo["language"], repo["owner"], repo["repo_name"],
                     velocity, trending_cat),
                )
                if cur.rowcount > 0:
                    new_count += 1
            except Exception as e:
                log.warning("DB insert error for %s: %s", url, e)
```

- [ ] **Step 7: Add trending badge to GitHub HTML rendering**

In `rendering.py`, update `_render_github_section` to show trending badge:

Replace the title_html line with:

```python
            trending = a.get("trending_category", "")
            trend_badge = ""
            if trending == "hot":
                trend_badge = '<span class="trend-badge trend-hot" title="Trending fast">&#128293;</span>'
            elif trending == "rising":
                trend_badge = '<span class="trend-badge trend-rising" title="Rising">&#128200;</span>'

            html += f'<div class="gh-item">'
            html += f'<span class="score-badge s{score}">{score}</span>'
            html += f'<div class="gh-detail"><div class="gh-title">{trend_badge}{title_html}</div>'
```

Also add CSS for trending badges in `generate_html` tab_css section:

```css
.trend-badge { margin-right: 4px; font-size: 0.85em; }
```

- [ ] **Step 8: Update mock data in test_github.py**

Add `createdAt` to `MOCK_GH_OUTPUT`:

```python
MOCK_GH_OUTPUT = json.dumps([
    {
        "name": "awesome-dft",
        "owner": {"login": "researcher"},
        "description": "A DFT automation toolkit",
        "url": "https://github.com/researcher/awesome-dft",
        "stargazersCount": 120,
        "language": "Python",
        "updatedAt": "2026-01-15T10:00:00Z",
        "createdAt": "2025-06-01T00:00:00Z",
    },
    {
        "name": "tiny-repo",
        "owner": {"login": "someone"},
        "description": "Tiny experiment",
        "url": "https://github.com/someone/tiny-repo",
        "stargazersCount": 2,
        "language": "Python",
        "updatedAt": "2025-12-01T00:00:00Z",
        "createdAt": "2025-11-01T00:00:00Z",
    },
    {
        "name": "phonon-ml",
        "owner": {"login": "labgroup"},
        "description": "ML for phonon calculations",
        "url": "https://github.com/labgroup/phonon-ml",
        "stargazersCount": 45,
        "language": "Julia",
        "updatedAt": "2026-03-01T08:00:00Z",
        "createdAt": "2026-01-15T00:00:00Z",
    },
])
```

- [ ] **Step 9: Run all tests**

Run: `pytest -v`
Expected: all PASS

- [ ] **Step 10: Commit**

```bash
git add sources/github.py db.py rendering.py test_github.py
git commit -m "feat: GitHub velocity calculation and trending badges"
```

### Task 8: Update GitHub scoring prompt with current_interests context

**Files:**
- Modify: `scoring.py:48-79` (`_score_batch`)

- [ ] **Step 1: Update _score_batch to include trending info for GitHub**

In `scoring.py`, modify `_score_batch` to add trending context:

```python
def _score_batch(batch, profile_text):
    """Score a single batch of articles via Claude Code subprocess."""
    articles_text = ""
    for i, a in enumerate(batch):
        trending_note = ""
        if a.get("source_type") == "github":
            tc = a.get("trending_category", "")
            vel = a.get("velocity", 0)
            if tc in ("hot", "rising"):
                trending_note = f" [TRENDING: {tc}, {vel} stars/day]"

        articles_text += (
            f"\n[{i}] Feed: {a['feed']}\n"
            f"    Title: {a['title']}\n"
            f"    Authors: {a['authors'] or 'N/A'}\n"
            f"    Summary: {a['summary']}{trending_note}\n"
        )

    prompt = f"""You are a research assistant. Score EVERY article by relevance to this researcher's interests.

## Researcher Profile
{profile_text}

## Articles
{articles_text}

## Instructions
Score each article from 1 to 5:
  5 = directly related to researcher's core topics OR matches current interests
  4 = closely related or trending in a related area
  3 = somewhat related
  2 = tangentially related
  1 = not related

IMPORTANT: Articles matching "Current interests" topics should score at least 4.
Trending repos in relevant areas should get a +1 bonus.

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
```

- [ ] **Step 2: Run scoring tests**

Run: `pytest test_main.py::TestScoreBatch -v && pytest test_main.py::TestScoreArticles -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add scoring.py
git commit -m "feat: scoring prompt includes current_interests and trending context"
```

### Task 9: Sort GitHub by score then velocity

**Files:**
- Modify: `rendering.py:108-142` (`_render_github_section`)

- [ ] **Step 1: Update GitHub section to sort by velocity within score groups**

In `rendering.py`, update `_render_github_section`:

```python
def _render_github_section(articles):
    """Render GitHub repos grouped by score descending, velocity within groups."""
    by_score = {}
    for a in articles:
        s = a.get("score", 1)
        by_score.setdefault(s, []).append(a)

    html = ""
    for score in sorted(by_score.keys(), reverse=True):
        items = sorted(by_score[score], key=lambda a: a.get("velocity", 0), reverse=True)
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
```

- [ ] **Step 2: Add trending badge CSS**

In `rendering.py`, in the `tab_css` string, add:

```css
.trend-badge { margin-right: 4px; }
```

- [ ] **Step 3: Run rendering tests**

Run: `pytest test_main.py::TestGenerateHtml -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add rendering.py
git commit -m "feat: GitHub repos sorted by velocity within score, trending badges"
```

## Chunk 4: Integration and Final Verification

### Task 10: Final integration test and cleanup

- [ ] **Step 1: Run full test suite**

Run: `pytest -v`
Expected: all PASS

- [ ] **Step 3: Manual smoke test — dry run**

```bash
source .venv/bin/activate
python main.py --dry-run fetch --all
python main.py --dry-run curate
```

Expected: fetch runs without error, curate generates HTML

- [ ] **Step 4: Verify generated HTML**

```bash
grep "Recent Articles" html/$(date +%Y-%m-%d).html && echo "Literature labels OK"
grep "trend-badge" html/$(date +%Y-%m-%d).html && echo "Trending badges OK"
```

- [ ] **Step 5: Commit all remaining changes**

```bash
git add -A
git commit -m "feat: complete recommendation redesign — profile-driven queries, OpenAlex+haiku literature, GitHub velocity"
```

- [ ] **Step 6: Push**

```bash
git push
```
