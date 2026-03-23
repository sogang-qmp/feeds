"""Tests for sources.literature module."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from db import init_db
from sources.literature import generate_queries, search_semantic_scholar, fetch_literature


# --- Fixtures ---

@pytest.fixture
def sample_profile():
    return {
        "researcher": {
            "name": "Test User",
            "position": "Professor",
            "affiliation": "Test University",
        },
        "research_areas": {
            "primary": [
                "first-principles computational methods",
                "electron-phonon coupling and polaron physics",
                "superconductivity",
            ],
        },
        "keywords": {
            "strong": [
                "electron-phonon",
                "polaron",
                "DFT",
                "2D materials",
                "phonon",
                "TMD",
                "GW approximation",
                "graphene",
                "moiré",
                "flat band",
            ],
            "moderate": ["computational physics", "VASP"],
            "weak": ["physics"],
        },
    }


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def mock_s2_response():
    """Mock Semantic Scholar API response."""
    return {
        "total": 2,
        "data": [
            {
                "paperId": "abc123",
                "title": "Electron-phonon coupling in 2D materials",
                "abstract": "We study electron-phonon coupling...",
                "authors": [
                    {"name": "Alice Smith"},
                    {"name": "Bob Jones"},
                ],
                "year": 2025,
                "externalIds": {"DOI": "10.1234/test.2025", "ArXiv": "2501.00001"},
                "url": "https://www.semanticscholar.org/paper/abc123",
                "citationCount": 42,
                "venue": "Physical Review Letters",
            },
            {
                "paperId": "def456",
                "title": "Polaron formation in TMDs",
                "abstract": "A" * 600,  # long abstract to test truncation
                "authors": [
                    {"name": f"Author {i}"} for i in range(7)
                ],
                "year": 2024,
                "externalIds": {"ArXiv": "2401.99999"},
                "url": "https://www.semanticscholar.org/paper/def456",
                "citationCount": 5,
                "venue": "",
            },
        ],
    }


# --- generate_queries ---

class TestGenerateQueries:
    def test_returns_list_of_strings(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert isinstance(queries, list)
        assert all(isinstance(q, str) for q in queries)

    def test_length_within_bounds(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert 5 <= len(queries) <= 20

    def test_contains_strong_keywords(self, sample_profile):
        queries = generate_queries(sample_profile)
        # At least some strong keywords should appear
        strong = sample_profile["keywords"]["strong"]
        found = sum(1 for kw in strong[:8] if kw in queries)
        assert found >= 5

    def test_max_15_queries(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert len(queries) <= 15

    def test_empty_profile(self):
        queries = generate_queries({})
        assert isinstance(queries, list)

    def test_includes_research_areas(self, sample_profile):
        queries = generate_queries(sample_profile)
        # Should contain at least one research area term
        all_text = " ".join(queries)
        assert "first-principles" in all_text or "superconductivity" in all_text


# --- search_semantic_scholar ---

class TestSearchSemanticScholar:
    def test_parses_response_correctly(self, mock_s2_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("electron-phonon")

        assert len(results) == 2

        # First paper: has DOI, should use DOI link
        paper1 = results[0]
        assert paper1["title"] == "Electron-phonon coupling in 2D materials"
        assert paper1["doi"] == "10.1234/test.2025"
        assert paper1["link"] == "https://doi.org/10.1234/test.2025"
        assert paper1["source_type"] == "literature"
        assert paper1["citation_count"] == 42
        assert paper1["venue"] == "Physical Review Letters"
        assert paper1["feed"] == "Physical Review Letters"
        assert paper1["authors"] == "Alice Smith, Bob Jones"

    def test_arxiv_fallback_link(self, mock_s2_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("polaron")

        # Second paper: no DOI, should use arXiv link
        paper2 = results[1]
        assert paper2["link"] == "https://arxiv.org/abs/2401.99999"
        assert paper2["arxiv_id"] == "2401.99999"

    def test_truncates_long_abstract(self, mock_s2_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("polaron")

        paper2 = results[1]
        assert len(paper2["summary"]) == 503  # 500 + "..."

    def test_normalizes_many_authors(self, mock_s2_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("TMD")

        paper2 = results[1]
        assert "et al." in paper2["authors"]
        # Should have 5 names + et al.
        assert paper2["authors"].count(",") == 4  # 5 names separated by 4 commas

    def test_handles_api_error_gracefully(self):
        with patch("sources.literature.requests.get", side_effect=Exception("Connection error")):
            results = search_semantic_scholar("test query")

        assert results == []

    def test_handles_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("429 Too Many Requests")

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("test query")

        assert results == []

    def test_passes_api_key(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp) as mock_get:
            search_semantic_scholar("test", api_key="my-key")

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["x-api-key"] == "my-key"

    def test_empty_data_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"total": 0, "data": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_semantic_scholar("nonexistent topic xyz")

        assert results == []


# --- fetch_literature ---

class TestFetchLiterature:
    def test_inserts_papers_into_db(self, db, sample_profile):
        mock_s2_data = {
            "data": [
                {
                    "paperId": "abc",
                    "title": "Test Paper",
                    "abstract": "Abstract here",
                    "authors": [{"name": "Alice"}],
                    "year": 2025,
                    "externalIds": {"DOI": "10.1234/test"},
                    "url": "https://s2.org/abc",
                    "citationCount": 10,
                    "venue": "Nature",
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_data
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp), \
             patch("sources.literature.time.sleep"):
            count = fetch_literature(sample_profile, db)

        assert count > 0
        rows = db.execute("SELECT * FROM articles WHERE source_type='literature'").fetchall()
        assert len(rows) > 0
        row = rows[0]
        assert row["title"] == "Test Paper"
        assert row["doi"] == "10.1234/test"
        assert row["source_type"] == "literature"

    def test_deduplicates_by_link(self, db, sample_profile):
        """Same paper from different queries should only be inserted once."""
        mock_s2_data = {
            "data": [
                {
                    "paperId": "abc",
                    "title": "Test Paper",
                    "abstract": "Abstract",
                    "authors": [{"name": "Alice"}],
                    "year": 2025,
                    "externalIds": {"DOI": "10.1234/test"},
                    "url": "https://s2.org/abc",
                    "citationCount": 10,
                    "venue": "Nature",
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_s2_data
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp), \
             patch("sources.literature.time.sleep"):
            count = fetch_literature(sample_profile, db)

        # Multiple queries returning same paper => only 1 row
        rows = db.execute("SELECT * FROM articles WHERE doi='10.1234/test'").fetchall()
        assert len(rows) == 1

    def test_respects_config(self, db, sample_profile):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status.return_value = None

        config = {
            "literature": {
                "semantic_scholar_api_key": "test-key",
                "year_range": "2025-2026",
                "max_results_per_query": 5,
            }
        }

        with patch("sources.literature.requests.get", return_value=mock_resp) as mock_get, \
             patch("sources.literature.time.sleep"):
            fetch_literature(sample_profile, db, config=config)

        # Check that API key and params were passed
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["x-api-key"] == "test-key"
        assert kwargs["params"]["year"] == "2025-2026"
        assert kwargs["params"]["limit"] == 5

    def test_rate_limits_between_queries(self, db, sample_profile):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp), \
             patch("sources.literature.time.sleep") as mock_sleep:
            fetch_literature(sample_profile, db)

        # Should have called sleep between queries (n-1 times)
        queries = generate_queries(sample_profile)
        assert mock_sleep.call_count == len(queries) - 1
        mock_sleep.assert_called_with(1.5)
