"""GitHub repository search via gh CLI."""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger("feeds")

_GH_BIN = "/home/ywchoi/.local/bin/gh"


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

    # 1. Current interests — prefer github_queries, fall back to examples
    interests = profile.get("current_interests", []) if profile else []
    high = [ci for ci in interests if ci.get("weight") == "high"]
    medium = [ci for ci in interests if ci.get("weight") != "high"]
    for ci in high + medium:
        gh_qs = ci.get("github_queries", [])
        if gh_qs:
            queries.extend(gh_qs[:2])
        else:
            topic = ci.get("topic", "")
            if topic:
                queries.append(topic)
            for ex in ci.get("examples", [])[:2]:
                queries.append(ex)
        if len(queries) >= 8:
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
        _GH_BIN, "search", "repos", full_query,
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
    1. Recent repos (created in last 6 months, sorted by stars)
    2. All-time top repos (sorted by stars)

    Computes velocity (stars/age) for trending detection.
    Returns count of newly inserted repos.
    """
    config = config or {}
    min_stars = config.get("github", {}).get("min_stars", 5)
    recent_months = config.get("github", {}).get("recent_months", 6)

    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_months * 30)).strftime("%Y-%m-%d")

    queries = generate_queries(profile)
    now = datetime.now(timezone.utc).isoformat()
    seen_urls = set()
    new_count = 0

    for q in queries:
        log.info("  GitHub search (recent): %s", q)
        recent_repos = search_github_repos(q, limit=10, sort="stars", created_after=recent_cutoff)
        log.info("  GitHub search (all-time): %s", q)
        alltime_repos = search_github_repos(q, limit=5, sort="stars")

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

    conn.commit()
    return new_count
