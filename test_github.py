"""Tests for sources/github.py — GitHub repo search."""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest import mock

import pytest

from db import init_db
from sources.github import fetch_github, generate_queries, search_github_repos, compute_velocity

# ---------------------------------------------------------------------------
# Sample profile (minimal subset of research_profile.yaml)
# ---------------------------------------------------------------------------
SAMPLE_PROFILE = {
    "research_areas": {
        "primary": [
            "first-principles computational methods",
            "electron-phonon coupling and polaron physics",
        ],
        "methods": [
            "density functional theory (DFT, VASP, Quantum ESPRESSO, SIESTA)",
            "electron-phonon coupling calculations (EPW)",
            "GW approximation and BerkeleyGW",
            "Wannier function interpolation",
            "machine learning for materials science and DFT automation",
        ],
    },
    "keywords": {
        "strong": ["electron-phonon", "DFT", "Wannier", "EPW", "BerkeleyGW"],
        "moderate": ["VASP", "ab initio automation", "DFT automation"],
    },
    "current_interests": [
        {"topic": "AI agents for physics", "weight": "high",
         "examples": ["vibe physics", "autonomous simulation"]},
        {"topic": "moiré phonons", "weight": "medium"},
    ],
}

# ---------------------------------------------------------------------------
# Mock gh output
# ---------------------------------------------------------------------------
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
        "stargazersCount": 2,  # below threshold
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


# ===========================================================================
# Tests for generate_queries
# ===========================================================================

class TestGenerateQueries:
    def test_returns_list_of_strings(self):
        qs = generate_queries(SAMPLE_PROFILE)
        assert isinstance(qs, list)
        assert all(isinstance(q, str) for q in qs)

    def test_length_at_least_three(self):
        qs = generate_queries(SAMPLE_PROFILE)
        assert len(qs) >= 3

    def test_max_twelve(self):
        qs = generate_queries(SAMPLE_PROFILE)
        assert len(qs) <= 12

    def test_uses_current_interests(self):
        qs = generate_queries(SAMPLE_PROFILE)
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

    def test_contains_profile_tools(self):
        qs = generate_queries(SAMPLE_PROFILE)
        combined = " ".join(qs).lower()
        assert any(t in combined for t in ("vasp", "wannier", "berkeleygw", "epw", "dft"))

    def test_handles_empty_profile(self):
        qs = generate_queries({})
        assert isinstance(qs, list)
        assert len(qs) >= 3

    def test_handles_none_profile(self):
        qs = generate_queries(None)
        assert isinstance(qs, list)
        assert len(qs) >= 3


# ===========================================================================
# Tests for compute_velocity
# ===========================================================================

class TestComputeVelocity:
    def test_basic_velocity(self):
        vel, cat = compute_velocity(100, "2026-01-01T00:00:00Z")
        assert vel > 0
        assert cat in ("hot", "rising", "established")

    def test_hot_repo(self):
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


# ===========================================================================
# Tests for search_github_repos
# ===========================================================================

class TestSearchGithubRepos:
    @mock.patch("sources.github.subprocess.run")
    def test_parses_mock_output(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=MOCK_GH_OUTPUT, stderr=""
        )
        results = search_github_repos("machine learning DFT")

        # tiny-repo (<5 stars) should be filtered out
        assert len(results) == 2

        first = results[0]
        assert first["title"] == "researcher/awesome-dft"
        assert first["stars"] == 120
        assert first["source_type"] == "github"
        assert first["link"] == "https://github.com/researcher/awesome-dft"
        assert first["language"] == "Python"
        assert first["owner"] == "researcher"
        assert first["repo_name"] == "awesome-dft"
        assert first["created_at"] == "2025-06-01T00:00:00Z"

    @mock.patch("sources.github.subprocess.run")
    def test_handles_gh_error(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="gh: not logged in"
        )
        results = search_github_repos("something")
        assert results == []

    @mock.patch("sources.github.subprocess.run")
    def test_handles_timeout(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="gh", timeout=30)
        results = search_github_repos("something")
        assert results == []

    @mock.patch("sources.github.subprocess.run")
    def test_handles_bad_json(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=0, stdout="not json", stderr=""
        )
        results = search_github_repos("something")
        assert results == []


# ===========================================================================
# Tests for fetch_github
# ===========================================================================

class TestFetchGithub:
    @mock.patch("sources.github.subprocess.run")
    def test_inserts_repos_into_db(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=MOCK_GH_OUTPUT, stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = init_db(db_path)

            count = fetch_github(SAMPLE_PROFILE, conn)

            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            assert len(rows) == 2
            assert count == 2

            row = conn.execute(
                "SELECT * FROM articles WHERE link=?",
                ("https://github.com/researcher/awesome-dft",),
            ).fetchone()
            assert row is not None
            assert row["stars"] == 120
            assert row["owner"] == "researcher"
            assert row["repo_name"] == "awesome-dft"
            assert row["velocity"] is not None
            assert row["trending_category"] in ("hot", "rising", "established")

            conn.close()

    @mock.patch("sources.github.subprocess.run")
    def test_dedup_within_run(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=MOCK_GH_OUTPUT, stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = init_db(db_path)

            count = fetch_github(SAMPLE_PROFILE, conn)

            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            assert len(rows) == 2
            assert count == 2
            conn.close()

    @mock.patch("sources.github.subprocess.run")
    def test_respects_min_stars_config(self, mock_run):
        single_repo = json.dumps([{
            "name": "mid-repo",
            "owner": {"login": "user"},
            "description": "Medium stars",
            "url": "https://github.com/user/mid-repo",
            "stargazersCount": 15,
            "language": "Python",
            "updatedAt": "2026-01-01T00:00:00Z",
            "createdAt": "2025-06-01T00:00:00Z",
        }])
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=single_repo, stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = init_db(db_path)

            count = fetch_github(SAMPLE_PROFILE, conn, config={"github": {"min_stars": 20}})
            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            assert len(rows) == 0
            assert count == 0
            conn.close()
