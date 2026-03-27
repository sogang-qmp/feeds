"""Tests for sources.literature module (OpenAlex API)."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from db import init_db
from sources.literature import (
    generate_queries, search_openalex, fetch_literature, _reconstruct_abstract,
)


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
def mock_openalex_response():
    """Mock OpenAlex API response."""
    return {
        "meta": {"count": 2},
        "results": [
            {
                "id": "https://openalex.org/W111",
                "title": "Electron-phonon coupling in 2D materials",
                "doi": "https://doi.org/10.1234/test.2025",
                "publication_year": 2025,
                "publication_date": "2025-03-15",
                "cited_by_count": 42,
                "abstract_inverted_index": {
                    "We": [0],
                    "study": [1],
                    "electron-phonon": [2],
                    "coupling": [3],
                    "in": [4],
                    "2D": [5],
                    "materials": [6],
                },
                "authorships": [
                    {"author": {"display_name": "Alice Smith"}},
                    {"author": {"display_name": "Bob Jones"}},
                ],
                "primary_location": {
                    "source": {"display_name": "Physical Review Letters"},
                },
                "locations": [
                    {"landing_page_url": "https://doi.org/10.1234/test.2025"},
                ],
            },
            {
                "id": "https://openalex.org/W222",
                "title": "Polaron formation in TMDs",
                "doi": None,
                "publication_year": 2024,
                "publication_date": "2024-06-01",
                "cited_by_count": 5,
                "abstract_inverted_index": {f"word{i}": [i] for i in range(600)},
                "authorships": [
                    {"author": {"display_name": f"Author {i}"}} for i in range(7)
                ],
                "primary_location": {"source": None},
                "locations": [
                    {"landing_page_url": "https://arxiv.org/abs/2401.99999"},
                ],
            },
        ],
    }


# --- _reconstruct_abstract ---

class TestReconstructAbstract:
    def test_basic(self):
        idx = {"Hello": [0], "world": [1]}
        assert _reconstruct_abstract(idx) == "Hello world"

    def test_empty(self):
        assert _reconstruct_abstract(None) == ""
        assert _reconstruct_abstract({}) == ""

    def test_multiple_positions(self):
        idx = {"the": [0, 2], "cat": [1], "sat": [3]}
        assert _reconstruct_abstract(idx) == "the cat the sat"


# --- generate_queries ---

class TestGenerateQueries:
    def test_returns_list_of_strings(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert isinstance(queries, list)
        assert all(isinstance(q, str) for q in queries)

    def test_length_within_bounds(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert 5 <= len(queries) <= 18

    def test_contains_strong_keywords(self, sample_profile):
        queries = generate_queries(sample_profile)
        strong = sample_profile["keywords"]["strong"]
        found = sum(1 for kw in strong[:6] if kw in queries)
        assert found >= 4

    def test_max_18_queries(self, sample_profile):
        queries = generate_queries(sample_profile)
        assert len(queries) <= 18

    def test_includes_current_interests_queries(self, sample_profile):
        sample_profile["current_interests"] = [
            {"topic": "AI agents for physics", "weight": "high",
             "examples": ["LLM computational physics", "autonomous DFT workflow"]},
        ]
        queries = generate_queries(sample_profile)
        combined = " ".join(queries).lower()
        assert "ai agents" in combined or "llm computational physics" in combined or "autonomous" in combined

    def test_empty_profile(self):
        queries = generate_queries({})
        assert isinstance(queries, list)

    def test_includes_research_areas(self, sample_profile):
        queries = generate_queries(sample_profile)
        all_text = " ".join(queries)
        assert "first-principles" in all_text or "superconductivity" in all_text


# --- search_openalex ---

class TestSearchOpenalex:
    def test_parses_response_correctly(self, mock_openalex_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_openalex_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_openalex("electron-phonon")

        assert len(results) == 2

        paper1 = results[0]
        assert paper1["title"] == "Electron-phonon coupling in 2D materials"
        assert paper1["doi"] == "10.1234/test.2025"
        assert paper1["link"] == "https://doi.org/10.1234/test.2025"
        assert paper1["source_type"] == "literature"
        assert paper1["citation_count"] == 42
        assert paper1["venue"] == "Physical Review Letters"
        assert paper1["authors"] == "Alice Smith, Bob Jones"
        assert paper1["year"] == 2025

    def test_arxiv_fallback_link(self, mock_openalex_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_openalex_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_openalex("polaron")

        paper2 = results[1]
        assert paper2["link"] == "https://arxiv.org/abs/2401.99999"
        assert paper2["arxiv_id"] == "2401.99999"

    def test_truncates_long_abstract(self, mock_openalex_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_openalex_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_openalex("polaron")

        paper2 = results[1]
        assert len(paper2["summary"]) <= 503

    def test_normalizes_many_authors(self, mock_openalex_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_openalex_response
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_openalex("TMD")

        paper2 = results[1]
        assert "et al." in paper2["authors"]
        assert paper2["authors"].count(",") == 4

    def test_handles_api_error_gracefully(self):
        with patch("sources.literature.requests.get", side_effect=Exception("Connection error")):
            results = search_openalex("test query")
        assert results == []

    def test_sends_mailto(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp) as mock_get:
            search_openalex("test")

        _, kwargs = mock_get.call_args
        assert "mailto" in kwargs["params"]

    def test_empty_results(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"meta": {"count": 0}, "results": []}
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            results = search_openalex("nonexistent topic xyz")
        assert results == []


# --- fetch_literature ---

class TestFetchLiterature:
    def test_inserts_papers_into_db(self, db, sample_profile):
        mock_data = {
            "results": [
                {
                    "id": "https://openalex.org/W111",
                    "title": "Test Paper",
                    "doi": "https://doi.org/10.1234/test",
                    "publication_year": 2025,
                    "publication_date": "2025-01-01",
                    "cited_by_count": 10,
                    "abstract_inverted_index": {"Abstract": [0], "here": [1]},
                    "authorships": [{"author": {"display_name": "Alice"}}],
                    "primary_location": {"source": {"display_name": "Nature"}},
                    "locations": [],
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            count = fetch_literature(sample_profile, db)

        assert count > 0
        rows = db.execute("SELECT * FROM articles WHERE source_type='literature'").fetchall()
        assert len(rows) > 0
        assert rows[0]["title"] == "Test Paper"
        assert rows[0]["source_type"] == "literature"

    def test_deduplicates_by_link(self, db, sample_profile):
        mock_data = {
            "results": [
                {
                    "id": "https://openalex.org/W111",
                    "title": "Test Paper",
                    "doi": "https://doi.org/10.1234/test",
                    "publication_year": 2025,
                    "publication_date": "2025-01-01",
                    "cited_by_count": 10,
                    "abstract_inverted_index": None,
                    "authorships": [{"author": {"display_name": "Alice"}}],
                    "primary_location": {"source": {"display_name": "Nature"}},
                    "locations": [],
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None

        with patch("sources.literature.requests.get", return_value=mock_resp):
            fetch_literature(sample_profile, db)

        rows = db.execute("SELECT * FROM articles WHERE doi='10.1234/test'").fetchall()
        assert len(rows) == 1

    def test_respects_year_config(self, db, sample_profile):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status.return_value = None

        config = {"literature": {"year_range": "2025-2026", "max_results_per_query": 5}}

        with patch("sources.literature.requests.get", return_value=mock_resp) as mock_get:
            fetch_literature(sample_profile, db, config=config)

        _, kwargs = mock_get.call_args
        # year_range "2025-2026" → filter "publication_year:>2024"
        assert "publication_year:>2024" in kwargs["params"].get("filter", "")
        assert kwargs["params"]["per_page"] == 5
