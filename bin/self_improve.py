#!/home/ywchoi/apps/feeds/.venv/bin/python
"""
Feeds Self-Improvement Loop: analyze quality, discover sources, suggest improvements.

Track A: Feed Quality Analysis (daily, ~30s, Python only)
Track B: Source Discovery (weekly Mon, ~5min, subagents + WebSearch)
Track C: System Improvement Ideas (weekly Thu, ~5min, subagents + WebSearch)

Usage:
    python bin/self_improve.py              # full run
    python bin/self_improve.py --dry-run    # print, no issues/slack
    python bin/self_improve.py --track-a    # Track A only
    python bin/self_improve.py --track-b    # Track B only (force)
    python bin/self_improve.py --track-c    # Track C only (force)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import yaml
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from llm import run_claude as _run_claude_text, extract_json

# ─── Constants ────────────────────────────────────────────────────────────────

FEEDS_DIR = Path(__file__).parent.parent
STATE_DIR = FEEDS_DIR / "state"
LOG_DIR = FEEDS_DIR / "logs"

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
GH_BIN = os.path.expanduser("~/.local/bin/gh")
GITHUB_REPO = "sogang-qmp/feeds"
MAX_ISSUES_PER_RUN = 3
IDEA_THRESHOLD = 20  # out of 25

# ─── Utilities ────────────────────────────────────────────────────────────────


def run_cmd(cmd, timeout=30, shell=False):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=shell, cwd=str(FEEDS_DIR),
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def run_claude(prompt, timeout=600, model="sonnet"):
    """Run Claude Code in pipe mode, return parsed JSON or None."""
    try:
        text = _run_claude_text(prompt, model=model, timeout=timeout)
        return extract_json(text)
    except Exception as e:
        print(f"[self-improve] Claude/parse error: {e}", file=sys.stderr)
        return None


def load_config():
    """Load feeds config.yaml."""
    with open(FEEDS_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def load_profile():
    """Load research_profile.yaml."""
    with open(FEEDS_DIR / "research_profile.yaml") as f:
        return yaml.safe_load(f)


def send_slack(text, channel=None):
    """Send message to Slack."""
    try:
        from slack_sdk import WebClient
        config = load_config()
        client = WebClient(token=config["slack"]["bot_token"])
        ch = channel or "C0AP10V9132"
        client.chat_postMessage(channel=ch, text=text)
        return True
    except Exception as e:
        print(f"[self-improve] Slack error: {e}", file=sys.stderr)
        return False


def create_github_issue(title, body, labels):
    """Create a GitHub issue, return issue URL."""
    for label in labels:
        subprocess.run(
            [GH_BIN, "label", "create", label, "--repo", GITHUB_REPO,
             "--description", f"Feeds {label}", "--color", "7057ff"],
            capture_output=True, timeout=15,
        )
    cmd = [GH_BIN, "issue", "create", "--repo", GITHUB_REPO, "--title", title, "--body", body]
    for label in labels:
        cmd.extend(["--label", label])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"[self-improve] gh issue create failed: {result.stderr}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[self-improve] gh error: {e}", file=sys.stderr)
        return None


def get_open_issues():
    """Fetch open self-improve issues to avoid duplicates."""
    try:
        result = subprocess.run(
            [GH_BIN, "issue", "list", "--repo", GITHUB_REPO,
             "--label", "self-improve", "--state", "open", "--json", "title,url"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return []
    except Exception:
        return []


def save_state(name, data):
    """Save state to JSON file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{name}.json", "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_state(name):
    """Load state from JSON file."""
    path = STATE_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


# ═════════════════════════════════════════════════════════════════════════════
#  TRACK A: Feed Quality Analysis (Python only, daily)
# ═════════════════════════════════════════════════════════════════════════════


def track_a():
    """Analyze feed quality: scoring distribution, coverage, stale feeds."""
    print("[Track A] Feed Quality Analysis...")
    t0 = time.time()
    findings = []
    report = {}

    config = load_config()
    db_path = FEEDS_DIR / config.get("feeds", {}).get("db", "feeds.db")

    if not db_path.exists():
        findings.append({"severity": "CRITICAL", "message": "feeds.db not found"})
        return {"findings": findings, "report": report, "elapsed": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # --- 1. Scoring distribution (last 7 days) ---
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT score, source_type, COUNT(*) as cnt FROM articles "
        "WHERE fetched_at > ? AND score IS NOT NULL GROUP BY score, source_type",
        (week_ago,),
    ).fetchall()

    score_dist = defaultdict(lambda: defaultdict(int))
    total_scored = 0
    for r in rows:
        score_dist[r["source_type"] or "rss"][r["score"]] = r["cnt"]
        total_scored += r["cnt"]

    report["score_distribution"] = {
        src: dict(scores) for src, scores in score_dist.items()
    }
    report["total_scored_7d"] = total_scored

    # Check if scoring is too generous or harsh
    if total_scored > 20:
        all_scores = []
        for r in rows:
            all_scores.extend([r["score"]] * r["cnt"])
        avg = sum(all_scores) / len(all_scores)
        report["avg_score_7d"] = round(avg, 2)

        high_pct = sum(1 for s in all_scores if s >= 4) / len(all_scores)
        low_pct = sum(1 for s in all_scores if s <= 2) / len(all_scores)

        if high_pct > 0.5:
            findings.append({
                "severity": "MEDIUM",
                "message": f"Scoring too generous: {high_pct:.0%} articles scored 4-5 (avg={avg:.1f}). "
                           "Consider tightening scoring criteria.",
            })
        if low_pct > 0.8:
            findings.append({
                "severity": "MEDIUM",
                "message": f"Scoring too harsh: {low_pct:.0%} articles scored 1-2. "
                           "RSS feeds may not align with profile. Consider updating subscriptions.",
            })

    # --- 2. Coverage gap analysis ---
    profile = load_profile()
    primary_areas = profile.get("research_areas", {}).get("primary", [])
    current_interests = [ci["topic"] for ci in profile.get("current_interests", [])]

    # Get recent high-score article titles
    high_score_rows = conn.execute(
        "SELECT title, score FROM articles WHERE fetched_at > ? AND score >= 4",
        (week_ago,),
    ).fetchall()
    high_titles = " ".join(r["title"] or "" for r in high_score_rows).lower()

    covered = []
    gaps = []
    for area in primary_areas + current_interests:
        # Simple keyword check
        keywords = area.lower().split()
        if any(kw in high_titles for kw in keywords if len(kw) > 3):
            covered.append(area)
        else:
            gaps.append(area)

    report["coverage"] = {"covered": covered, "gaps": gaps}
    if gaps:
        findings.append({
            "severity": "LOW",
            "message": f"No high-scoring articles (4-5) in past week for: {', '.join(gaps[:5])}",
        })

    # --- 3. Stale/dead RSS feeds ---
    feeds_rows = conn.execute(
        "SELECT feed, MAX(fetched_at) as last_fetched, COUNT(*) as cnt "
        "FROM articles WHERE source_type IS NULL OR source_type='rss' "
        "GROUP BY feed",
    ).fetchall()

    month_ago = (datetime.now() - timedelta(days=30)).isoformat()
    stale_feeds = []
    for r in feeds_rows:
        if r["last_fetched"] and r["last_fetched"] < month_ago:
            stale_feeds.append(r["feed"])

    report["stale_feeds"] = stale_feeds
    if stale_feeds:
        findings.append({
            "severity": "LOW",
            "message": f"Stale feeds (no articles in 30d): {', '.join(stale_feeds[:5])}",
        })

    # --- 4. Source type breakdown ---
    source_counts = conn.execute(
        "SELECT source_type, COUNT(*) as cnt FROM articles "
        "WHERE fetched_at > ? GROUP BY source_type", (week_ago,),
    ).fetchall()
    report["source_counts_7d"] = {
        (r["source_type"] or "rss"): r["cnt"] for r in source_counts
    }

    # --- 5. Recommendation dedup check ---
    rec_hist_path = FEEDS_DIR / "recommendations_history.json"
    if rec_hist_path.exists():
        with open(rec_hist_path) as f:
            rec_hist = json.load(f)
        report["recommendation_history_size"] = len(rec_hist.get("urls", []))

    # --- 6. Disk / log health ---
    for name, path in [("logs", LOG_DIR), ("tmp", FEEDS_DIR / "tmp")]:
        if path.exists():
            rc, stdout, _ = run_cmd(["du", "-sm", str(path)])
            if rc == 0:
                mb = int(stdout.split()[0])
                report[f"{name}_disk_mb"] = mb
                if mb > 500:
                    findings.append({
                        "severity": "HIGH",
                        "message": f"{name}/ using {mb}MB (>500MB)",
                    })

    conn.close()
    elapsed = round(time.time() - t0, 1)
    report["elapsed_sec"] = elapsed
    print(f"[Track A] Done in {elapsed}s, {len(findings)} findings.")

    return {"findings": findings, "report": report, "elapsed": elapsed}


# ═════════════════════════════════════════════════════════════════════════════
#  TRACK B: Source Discovery (subagents, weekly Monday)
# ═════════════════════════════════════════════════════════════════════════════

TRACK_B_RSS_SCOUT = """You are an RSS feed discovery agent for a condensed matter physics researcher.

RESEARCHER PROFILE:
{profile_summary}

CURRENT RSS FEEDS:
{current_feeds}

COVERAGE GAPS (no high-scoring articles recently):
{coverage_gaps}

TASK: Search the web for NEW RSS feeds, blogs, and news sources that would help this researcher
stay current. Focus on:
1. Research blogs by active condensed matter / computational physics groups
2. ArXiv RSS feeds for relevant categories (cond-mat.*, physics.comp-ph, etc.)
3. News/blog aggregators for AI + science (especially AI agents for research)
4. Conference/workshop announcement feeds

For each suggestion, provide:
- name: feed/blog name
- url: feed URL (RSS/Atom)
- why: one sentence on relevance
- category: "physics" | "ai_science" | "tools" | "news"

Return JSON:
```json
{{
  "ideas": [
    {{"name": "...", "url": "...", "why": "...", "category": "...",
      "scores": {{"practicality": 1-5, "impact": 1-5, "effort": 1-5, "relevance": 1-5, "novelty": 1-5}},
      "total": <sum of scores>}}
  ]
}}
```
Only include ideas scoring 20+ total. Max 5 ideas."""

TRACK_B_ACADEMIC_SCOUT = """You are an academic source discovery agent for a physics researcher.

RESEARCHER PROFILE:
{profile_summary}

CURRENT LITERATURE CONFIG:
- Using OpenAlex API for paper search
- Year range: 2024-2026
- 15 queries generated from profile keywords

TASK: Search the web for ways to improve academic literature discovery:
1. New preprint servers or databases beyond arXiv/OpenAlex
2. Conference proceedings feeds (APS March Meeting, MRS, etc.)
3. Researcher alert services (Google Scholar alerts, ResearchGate, etc.)
4. Specialized APIs for condensed matter / computational physics papers

Return JSON:
```json
{{
  "ideas": [
    {{"name": "...", "description": "...", "why": "...", "integration_effort": "low|medium|high",
      "scores": {{"practicality": 1-5, "impact": 1-5, "effort": 1-5, "relevance": 1-5, "novelty": 1-5}},
      "total": <sum of scores>}}
  ]
}}
```
Only include ideas scoring 20+ total. Max 5 ideas."""

TRACK_B_GITHUB_SCOUT = """You are a GitHub search optimization agent for a physics research feed system.

RESEARCHER PROFILE:
{profile_summary}

CURRENT GITHUB QUERIES (12 queries, 2 passes each):
{github_info}

CURRENT INTERESTS (HIGH WEIGHT):
- AI agents for scientific research (vibe physics)
- Agentic AI and autonomous research workflows
- NOT interested in: MLP / ML materials prediction / force fields

TASK: Search the web and evaluate current GitHub query strategy:
1. Are current queries finding relevant repos? Suggest better query terms.
2. New GitHub topics/tags to monitor
3. Alternative discovery methods (GitHub Explore, awesome-lists, etc.)
4. Trending tools in computational physics / AI-for-science space

Return JSON:
```json
{{
  "ideas": [
    {{"name": "...", "description": "...", "why": "...",
      "scores": {{"practicality": 1-5, "impact": 1-5, "effort": 1-5, "relevance": 1-5, "novelty": 1-5}},
      "total": <sum of scores>}}
  ]
}}
```
Only include ideas scoring 20+ total. Max 5 ideas."""


def build_profile_summary(profile):
    """Build a one-paragraph researcher summary."""
    r = profile.get("researcher", {})
    areas = profile.get("research_areas", {})
    interests = profile.get("current_interests", [])
    return (
        f"{r.get('name', 'Researcher')}, {r.get('position', '')} at {r.get('affiliation', '')}. "
        f"Primary areas: {', '.join(areas.get('primary', [])[:5])}. "
        f"Methods: {', '.join(areas.get('methods', [])[:5])}. "
        f"Current interests: {', '.join(ci['topic'] for ci in interests)}."
    )


def get_current_feeds_summary():
    """Get list of current RSS feed names from OPML."""
    opml_path = FEEDS_DIR / "feeds.opml"
    if not opml_path.exists():
        return "No OPML file found"
    import xml.etree.ElementTree as ET
    tree = ET.parse(opml_path)
    feeds = []
    for outline in tree.iter("outline"):
        if outline.get("type") == "rss":
            feeds.append(f"- {outline.get('title', outline.get('text', 'unknown'))}")
    return "\n".join(feeds) if feeds else "No feeds found"


def get_github_query_summary():
    """Summarize current GitHub search approach."""
    github_path = FEEDS_DIR / "sources" / "github.py"
    if not github_path.exists():
        return "GitHub module not found"
    with open(github_path) as f:
        content = f.read()
    # Extract the query generation logic (first 50 lines of generate_queries)
    match = re.search(r"def generate_queries.*?(?=\ndef |\Z)", content, re.DOTALL)
    if match:
        return match.group(0)[:2000]
    return "Could not parse generate_queries"


def track_b(quality_report=None):
    """Source discovery via 3 parallel Claude subagents."""
    print("[Track B] Source Discovery (3 subagents)...")
    t0 = time.time()

    profile = load_profile()
    profile_summary = build_profile_summary(profile)
    current_feeds = get_current_feeds_summary()
    github_info = get_github_query_summary()
    coverage_gaps = ", ".join(
        (quality_report or {}).get("report", {}).get("coverage", {}).get("gaps", ["unknown"])
    )

    prompts = [
        ("rss_scout", TRACK_B_RSS_SCOUT.format(
            profile_summary=profile_summary,
            current_feeds=current_feeds,
            coverage_gaps=coverage_gaps,
        )),
        ("academic_scout", TRACK_B_ACADEMIC_SCOUT.format(
            profile_summary=profile_summary,
        )),
        ("github_scout", TRACK_B_GITHUB_SCOUT.format(
            profile_summary=profile_summary,
            github_info=github_info,
        )),
    ]

    # Run subagents in parallel via subprocess
    all_ideas = []
    processes = []

    for name, prompt in prompts:
        print(f"  Spawning {name}...")
        tmp_dir = FEEDS_DIR / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(tmp_dir))
        os.close(fd)

        cmd = [
            CLAUDE_BIN, "-p", "-",
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--model", "sonnet",
        ]
        stdout_f = open(tmp_path, "w")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=stdout_f,
            stderr=subprocess.PIPE, cwd=str(FEEDS_DIR),
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        processes.append((name, proc, stdout_f, tmp_path))

    for name, proc, stdout_f, tmp_path in processes:
        try:
            proc.wait(timeout=600)
            stdout_f.close()
            with open(tmp_path) as f:
                raw = f.read()

            try:
                claude_out = json.loads(raw)
                result_text = claude_out.get("result", "")
            except json.JSONDecodeError:
                result_text = raw

            json_match = re.search(r"```json\s*(.*?)\s*```", result_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                data = json.loads(result_text)

            ideas = data.get("ideas", [])
            for idea in ideas:
                idea["source_agent"] = name
            all_ideas.extend(ideas)
            print(f"  {name}: {len(ideas)} ideas")

        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_f.close()
            print(f"  {name}: TIMEOUT", file=sys.stderr)
        except Exception as e:
            if not stdout_f.closed:
                stdout_f.close()
            print(f"  {name}: ERROR {e}", file=sys.stderr)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Filter and sort by score
    qualified = [i for i in all_ideas if i.get("total", 0) >= IDEA_THRESHOLD]
    qualified.sort(key=lambda x: x.get("total", 0), reverse=True)

    elapsed = round(time.time() - t0, 1)
    print(f"[Track B] Done in {elapsed}s. {len(qualified)}/{len(all_ideas)} ideas qualified.")

    return {"ideas": qualified, "all_count": len(all_ideas), "elapsed": elapsed}


# ═════════════════════════════════════════════════════════════════════════════
#  TRACK C: System Improvement Ideas (subagents, weekly Thursday)
# ═════════════════════════════════════════════════════════════════════════════

TRACK_C_TOOLS_SCOUT = """You are a tools and techniques scout for an RSS feed curation system.

SYSTEM DESCRIPTION:
- Python app that fetches RSS feeds, OpenAlex papers, and GitHub repos
- Scores articles 1-5 using Claude Haiku based on researcher profile
- Generates static HTML pages with tabbed UI (RSS, Literature, GitHub tabs)
- Claude Sonnet generates literature recommendations with web search
- Daily cron job: update_profile → fetch → curate → Slack notification
- Stack: Python, SQLite, feedparser, Claude CLI, slack-sdk

CURRENT WORKFLOW:
{workflow_summary}

TASK: Search the web for tools, techniques, and ideas to improve this feed system:
1. Better LLM scoring strategies (few-shot, calibration, multi-model)
2. Feed discovery and curation tools (Feedly API, Inoreader, etc.)
3. Content deduplication techniques (semantic similarity, etc.)
4. Better HTML rendering or delivery methods (email digest, RSS-of-RSS)
5. New Claude Code features (MCP servers, hooks) that could help

Return JSON:
```json
{{
  "ideas": [
    {{"name": "...", "description": "...", "why": "...", "effort": "low|medium|high",
      "scores": {{"practicality": 1-5, "impact": 1-5, "effort": 1-5, "relevance": 1-5, "novelty": 1-5}},
      "total": <sum of scores>}}
  ]
}}
```
Only include ideas scoring 20+ total. Max 5 ideas."""

TRACK_C_UX_SCOUT = """You are a UX and delivery improvement scout for a research feed system.

SYSTEM DESCRIPTION:
- Generates static HTML pages served at vesper.sogang.ac.kr/feeds/
- Tabbed UI: RSS (table by folder), Literature (tiered recommendations), GitHub (by score)
- Scores color-coded (5=red, 4=orange, 3=yellow, 2=gray, 1=light)
- Slack notification with daily link
- KaTeX for math rendering, mobile-responsive CSS
- User is a physics professor who checks feeds daily

TASK: Search the web for ideas to improve the feed reading experience:
1. Better visualization of article relevance and trends over time
2. Personalized email digests vs web page
3. Interactive filtering/sorting in HTML
4. Reading list / bookmark features
5. Integration with reference managers (Zotero, Mendeley)
6. Progressive summarization (AI summary on hover/click)

Return JSON:
```json
{{
  "ideas": [
    {{"name": "...", "description": "...", "why": "...", "effort": "low|medium|high",
      "scores": {{"practicality": 1-5, "impact": 1-5, "effort": 1-5, "relevance": 1-5, "novelty": 1-5}},
      "total": <sum of scores>}}
  ]
}}
```
Only include ideas scoring 20+ total. Max 5 ideas."""


def get_workflow_summary():
    """Summarize the current feeds workflow from logs."""
    log_path = LOG_DIR / "feeds.log"
    if not log_path.exists():
        return "No recent logs found"

    # Read last 100 lines
    try:
        rc, stdout, _ = run_cmd(["tail", "-100", str(log_path)])
        return stdout[-2000:] if stdout else "Empty log"
    except Exception:
        return "Could not read logs"


def track_c():
    """System improvement ideas via 2 parallel Claude subagents."""
    print("[Track C] System Improvement Ideas (2 subagents)...")
    t0 = time.time()

    workflow_summary = get_workflow_summary()

    prompts = [
        ("tools_scout", TRACK_C_TOOLS_SCOUT.format(
            workflow_summary=workflow_summary,
        )),
        ("ux_scout", TRACK_C_UX_SCOUT),
    ]

    all_ideas = []
    processes = []

    for name, prompt in prompts:
        print(f"  Spawning {name}...")
        tmp_dir = FEEDS_DIR / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(tmp_dir))
        os.close(fd)

        cmd = [
            CLAUDE_BIN, "-p", "-",
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--model", "sonnet",
        ]
        stdout_f = open(tmp_path, "w")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=stdout_f,
            stderr=subprocess.PIPE, cwd=str(FEEDS_DIR),
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        processes.append((name, proc, stdout_f, tmp_path))

    for name, proc, stdout_f, tmp_path in processes:
        try:
            proc.wait(timeout=600)
            stdout_f.close()
            with open(tmp_path) as f:
                raw = f.read()

            try:
                claude_out = json.loads(raw)
                result_text = claude_out.get("result", "")
            except json.JSONDecodeError:
                result_text = raw

            json_match = re.search(r"```json\s*(.*?)\s*```", result_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                data = json.loads(result_text)

            ideas = data.get("ideas", [])
            for idea in ideas:
                idea["source_agent"] = name
            all_ideas.extend(ideas)
            print(f"  {name}: {len(ideas)} ideas")

        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_f.close()
            print(f"  {name}: TIMEOUT", file=sys.stderr)
        except Exception as e:
            if not stdout_f.closed:
                stdout_f.close()
            print(f"  {name}: ERROR {e}", file=sys.stderr)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    qualified = [i for i in all_ideas if i.get("total", 0) >= IDEA_THRESHOLD]
    qualified.sort(key=lambda x: x.get("total", 0), reverse=True)

    elapsed = round(time.time() - t0, 1)
    print(f"[Track C] Done in {elapsed}s. {len(qualified)}/{len(all_ideas)} ideas qualified.")

    return {"ideas": qualified, "all_count": len(all_ideas), "elapsed": elapsed}


# ═════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════


def build_slack_report(results, issues_created):
    """Build a Slack report summarizing the run."""
    lines = ["*[feeds] Self-Improve Report*\n"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"_Run: {now}_\n")

    if "track_a" in results:
        ta = results["track_a"]
        findings = ta.get("findings", [])
        report = ta.get("report", {})
        lines.append(f"*Track A: Quality Analysis* ({ta.get('elapsed', '?')}s)")
        lines.append(f"  Scored articles (7d): {report.get('total_scored_7d', '?')}")
        if report.get("avg_score_7d"):
            lines.append(f"  Avg score: {report['avg_score_7d']}")
        gaps = report.get("coverage", {}).get("gaps", [])
        if gaps:
            lines.append(f"  Coverage gaps: {', '.join(gaps[:3])}")
        stale = report.get("stale_feeds", [])
        if stale:
            lines.append(f"  Stale feeds: {', '.join(stale[:3])}")
        if findings:
            lines.append(f"  Findings: {len(findings)}")
        lines.append("")

    for track_name, track_key in [("Track B: Source Discovery", "track_b"),
                                   ("Track C: System Improvement", "track_c")]:
        if track_key in results:
            t = results[track_key]
            lines.append(f"*{track_name}* ({t.get('elapsed', '?')}s)")
            lines.append(f"  Ideas: {len(t.get('ideas', []))} qualified / {t.get('all_count', '?')} total")
            for idea in t.get("ideas", [])[:3]:
                lines.append(f"  - {idea.get('name', '?')} ({idea.get('total', '?')}/25)")
            lines.append("")

    if issues_created:
        lines.append(f"*GitHub Issues Created: {len(issues_created)}*")
        for url in issues_created:
            lines.append(f"  {url}")
    else:
        lines.append("_No GitHub issues created._")

    return "\n".join(lines)


def create_issues_from_ideas(ideas, existing_titles, dry_run=False):
    """Create GitHub issues for top ideas, avoiding duplicates."""
    issues_created = []
    existing_lower = {t.get("title", "").lower() for t in existing_titles}

    for idea in ideas[:MAX_ISSUES_PER_RUN]:
        name = idea.get("name", "Untitled")
        title = f"[self-improve] {name}"

        # Skip if similar issue exists
        if any(name.lower() in t for t in existing_lower):
            print(f"  Skipping duplicate: {title}")
            continue

        body_parts = [
            f"## {name}",
            "",
            idea.get("description", idea.get("why", "")),
            "",
            f"**Source agent:** {idea.get('source_agent', 'unknown')}",
            f"**Score:** {idea.get('total', '?')}/25",
        ]
        if idea.get("scores"):
            scores = idea["scores"]
            body_parts.append(
                f"**Breakdown:** practicality={scores.get('practicality', '?')}, "
                f"impact={scores.get('impact', '?')}, effort={scores.get('effort', '?')}, "
                f"relevance={scores.get('relevance', '?')}, novelty={scores.get('novelty', '?')}"
            )
        if idea.get("url"):
            body_parts.append(f"\n**URL:** {idea['url']}")
        if idea.get("effort"):
            body_parts.append(f"**Integration effort:** {idea['effort']}")

        body_parts.append(f"\n---\n_Auto-generated by feeds self-improvement loop on {datetime.now().strftime('%Y-%m-%d')}_")
        body = "\n".join(body_parts)

        if dry_run:
            print(f"  [DRY RUN] Would create: {title}")
            print(f"  Score: {idea.get('total', '?')}/25")
            issues_created.append(f"(dry-run) {title}")
        else:
            url = create_github_issue(title, body, ["self-improve"])
            if url:
                print(f"  Created: {url}")
                issues_created.append(url)

    return issues_created


def main():
    parser = argparse.ArgumentParser(description="Feeds Self-Improvement Loop")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no issues or Slack")
    parser.add_argument("--track-a", action="store_true", help="Run Track A only")
    parser.add_argument("--track-b", action="store_true", help="Run Track B only (force)")
    parser.add_argument("--track-c", action="store_true", help="Run Track C only (force)")
    args = parser.parse_args()

    single_track = args.track_a or args.track_b or args.track_c
    today = datetime.now()
    is_monday = today.weekday() == 0
    is_thursday = today.weekday() == 3

    print(f"[self-improve] Starting at {today.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Day: {today.strftime('%A')} | Dry run: {args.dry_run}")

    results = {}
    all_ideas = []

    # Track A: always runs (or if --track-a)
    if not single_track or args.track_a:
        results["track_a"] = track_a()
        save_state("quality_report", results["track_a"])

    # Track B: Mondays only (or if --track-b to force)
    if args.track_b or (not single_track and is_monday):
        quality_report = results.get("track_a") or load_state("quality_report")
        results["track_b"] = track_b(quality_report)
        save_state("source_discovery", results["track_b"])
        all_ideas.extend(results["track_b"].get("ideas", []))

    # Track C: Thursdays only (or if --track-c to force)
    if args.track_c or (not single_track and is_thursday):
        results["track_c"] = track_c()
        save_state("system_improvement", results["track_c"])
        all_ideas.extend(results["track_c"].get("ideas", []))

    # Create GitHub issues from top ideas
    issues_created = []
    if all_ideas:
        all_ideas.sort(key=lambda x: x.get("total", 0), reverse=True)
        existing = get_open_issues()
        issues_created = create_issues_from_ideas(all_ideas, existing, dry_run=args.dry_run)

    # Slack report
    report_text = build_slack_report(results, issues_created)
    print("\n" + report_text)

    if not args.dry_run:
        send_slack(report_text)

    print(f"\n[self-improve] Complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            send_slack(f"*[feeds] Self-Improve Error*\n```{tb[-2000:]}```")
        except Exception:
            pass
        sys.exit(1)
