"""Tests for sources/github.py — GitHub repo search."""

import json
import os
import tempfile
from unittest import mock

import pytest

from db import init_db
from sources.github import fetch_github, generate_queries, search_github_repos

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
    },
    {
        "name": "tiny-repo",
        "owner": {"login": "someone"},
        "description": "Tiny experiment",
        "url": "https://github.com/someone/tiny-repo",
        "stargazersCount": 2,  # below threshold
        "language": "Python",
        "updatedAt": "2025-12-01T00:00:00Z",
    },
    {
        "name": "phonon-ml",
        "owner": {"login": "labgroup"},
        "description": "ML for phonon calculations",
        "url": "https://github.com/labgroup/phonon-ml",
        "stargazersCount": 45,
        "language": "Julia",
        "updatedAt": "2026-03-01T08:00:00Z",
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

    def test_max_ten(self):
        qs = generate_queries(SAMPLE_PROFILE)
        assert len(qs) <= 10

    def test_contains_ai_physics_terms(self):
        qs = generate_queries(SAMPLE_PROFILE)
        combined = " ".join(qs).lower()
        # Should contain at least one AI-ish term and one physics-ish term
        assert any(t in combined for t in ("machine learning", "neural network", "deep learning", "ai"))
        assert any(t in combined for t in ("dft", "phonon", "materials", "electron-phonon", "ab initio"))

    def test_handles_empty_profile(self):
        qs = generate_queries({})
        assert isinstance(qs, list)
        assert len(qs) >= 3

    def test_handles_none_profile(self):
        qs = generate_queries(None)
        assert isinstance(qs, list)
        assert len(qs) >= 3


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

            # Should have inserted the 2 repos with >=5 stars
            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            assert len(rows) == 2
            assert count == 2

            # Check fields on first row
            row = conn.execute(
                "SELECT * FROM articles WHERE link=?",
                ("https://github.com/researcher/awesome-dft",),
            ).fetchone()
            assert row is not None
            assert row["stars"] == 120
            assert row["owner"] == "researcher"
            assert row["repo_name"] == "awesome-dft"

            conn.close()

    @mock.patch("sources.github.subprocess.run")
    def test_dedup_within_run(self, mock_run):
        """Same URL returned by multiple queries should be inserted only once."""
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=MOCK_GH_OUTPUT, stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = init_db(db_path)

            count = fetch_github(SAMPLE_PROFILE, conn)

            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            # Even though generate_queries returns multiple queries, dedup ensures
            # each URL appears only once
            assert len(rows) == 2
            assert count == 2
            conn.close()

    @mock.patch("sources.github.subprocess.run")
    def test_respects_min_stars_config(self, mock_run):
        """Config github.min_stars should filter repos."""
        single_repo = json.dumps([{
            "name": "mid-repo",
            "owner": {"login": "user"},
            "description": "Medium stars",
            "url": "https://github.com/user/mid-repo",
            "stargazersCount": 15,
            "language": "Python",
            "updatedAt": "2026-01-01T00:00:00Z",
        }])
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=single_repo, stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = init_db(db_path)

            # min_stars=20 should exclude the repo with 15 stars
            count = fetch_github(SAMPLE_PROFILE, conn, config={"github": {"min_stars": 20}})
            rows = conn.execute("SELECT * FROM articles WHERE source_type='github'").fetchall()
            assert len(rows) == 0
            assert count == 0
            conn.close()
