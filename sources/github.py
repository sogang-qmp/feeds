"""GitHub repository search via gh CLI."""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger("feeds")

# AI agents for scientific research (highest weight — user's core interest)
# Model: "vibe physics" — AI agents autonomously doing physics computation
# NOTE: excludes MLP/force-field/materials-prediction — user not interested
_AI_PHYSICS_QUERIES = [
    "AI agent scientific simulation",
    "LLM agent computational physics",
    "agentic workflow scientific computing",
    "autonomous research agent physics",
    "Claude code scientific research",
    "LLM long-running agent simulation",
    "AI agent ab initio calculation",
    "agentic AI research automation",
    "LLM scientific computation pipeline",
    "AI agent DFT workflow",
]

# Cross-domain queries: ML/AI + physics/materials (legacy pairs)
_ML_PHYSICS_PAIRS = [
    ("machine learning", "DFT"),
    ("neural network", "phonon"),
    ("deep learning", "materials science"),
    ("graph neural network", "crystal"),
    ("machine learning", "electron-phonon"),
    ("AI", "ab initio"),
    ("machine learning", "molecular dynamics"),
]

# Tool/code specific queries
_TOOL_QUERIES = [
    "VASP workflow automation",
    "Wannier90",
    "EPW electron-phonon",
    "BerkeleyGW",
    "DFT automation python",
    "ab initio phonon",
]

# Research method queries
_METHOD_QUERIES = [
    "GW approximation code",
    "Bethe-Salpeter equation",
    "polaron first-principles",
    "moire superlattice simulation",
]


def generate_queries(profile):
    """Generate GitHub search queries from researcher profile.

    Prioritises AI/ML + physics intersection, then tool/code queries,
    and research methods. Returns max 12 queries.
    """
    queries = []

    # 1. AI-focused queries first (higher weight — 6 slots)
    queries.extend(_AI_PHYSICS_QUERIES[:6])

    # 2. ML + physics cross-queries (pick up to 2)
    for ml_term, phys_term in _ML_PHYSICS_PAIRS[:2]:
        queries.append(f"{ml_term} {phys_term}")

    # 3. Tool/code specific queries from profile keywords
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
                                      "dft automation", "ab initio", "machine learning")):
                tool_keywords.add(m)

    tool_qs = [f"{tk}" for tk in sorted(tool_keywords)][:3]
    if not tool_qs:
        tool_qs = _TOOL_QUERIES[:3]
    queries.extend(tool_qs)

    # 4. Add 2 research method queries
    queries.extend(_METHOD_QUERIES[:2])

    # Deduplicate while preserving order, cap at 12
    seen = set()
    unique = []
    for q in queries:
        ql = q.lower()
        if ql not in seen:
            seen.add(ql)
            unique.append(q)
    return unique[:12]


def search_github_repos(query, limit=10, sort="stars", created_after=None):
    """Search GitHub repos using gh CLI.

    Returns list of dicts with standardised article fields plus GitHub-specific
    fields (stars, language, owner, repo_name).
    """
    qualifiers = []
    if created_after:
        qualifiers.append(f"created:>={created_after}")
    qualifier_str = " ".join(qualifiers)
    full_query = f"{query} {qualifier_str}".strip() if qualifier_str else query

    cmd = [
        "gh", "search", "repos", full_query,
        "--limit", str(limit),
        "--sort", sort,
        "--order", "desc",
        "--json", "name,owner,description,url,stargazersCount,language,updatedAt,createdAt",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning("gh search failed for %r: %s", query, result.stderr.strip())
            return []
        repos = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log.warning("gh search error for %r: %s", query, e)
        return []

    items = []
    for r in repos:
        stars = r.get("stargazersCount", 0) or 0
        if stars < 5:
            continue
        owner_login = ""
        if isinstance(r.get("owner"), dict):
            owner_login = r["owner"].get("login", "")
        elif isinstance(r.get("owner"), str):
            owner_login = r["owner"]
        name = r.get("name", "")
        items.append({
            "link": r.get("url", ""),
            "title": f"{owner_login}/{name}" if owner_login else name,
            "authors": owner_login,
            "summary": r.get("description") or "",
            "published": r.get("updatedAt", ""),
            "created_at": r.get("createdAt", ""),
            "source_type": "github",
            "feed": "GitHub",
            "folder": "",
            "stars": stars,
            "language": r.get("language") or "",
            "owner": owner_login,
            "repo_name": name,
        })
    return items


def fetch_github(profile, conn, config=None):
    """Fetch GitHub repos matching profile and insert into DB.

    Runs two passes per query:
    1. Recent repos (created in last 6 months, sorted by stars) — surfaces trending new projects
    2. All-time top repos (sorted by stars) — catches established projects

    Returns count of newly inserted repos.
    """
    config = config or {}
    min_stars = config.get("github", {}).get("min_stars", 5)
    recent_months = config.get("github", {}).get("recent_months", 6)

    # Date threshold for "recent" repos
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_months * 30)).strftime("%Y-%m-%d")

    queries = generate_queries(profile)
    now = datetime.now(timezone.utc).isoformat()
    seen_urls = set()
    new_count = 0

    for q in queries:
        # Pass 1: recent trending repos (lower star threshold)
        log.info("  GitHub search (recent): %s", q)
        recent_repos = search_github_repos(q, limit=10, sort="stars", created_after=recent_cutoff)
        # Pass 2: all-time top repos
        log.info("  GitHub search (all-time): %s", q)
        alltime_repos = search_github_repos(q, limit=5, sort="stars")

        for repo in recent_repos + alltime_repos:
            if repo["stars"] < min_stars:
                continue
            url = repo["link"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (link, feed, folder, title, authors, summary, published,
                        fetched_at, source_type, stars, language, owner, repo_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (url, repo["feed"], repo["folder"], repo["title"],
                     repo["authors"], repo["summary"], repo["published"],
                     now, "github",
                     repo["stars"], repo["language"], repo["owner"], repo["repo_name"]),
                )
                if cur.rowcount > 0:
                    new_count += 1
            except Exception as e:
                log.warning("DB insert error for %s: %s", url, e)

    conn.commit()
    return new_count
