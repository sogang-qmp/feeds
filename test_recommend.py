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
             "examples": ["LLM computational physics"]},
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

    def test_excludes_recent_urls(self, sample_profile, mock_candidates):
        recent = [mock_candidates[0]["link"], mock_candidates[1]["link"]]
        haiku_response = json.dumps([
            {"index": i, "tier": "recent", "why": f"reason {i}"}
            for i in range(12)
        ])
        with patch("recommend.run_claude", return_value=haiku_response):
            recs = _curate_candidates(mock_candidates, sample_profile, recent)
        # Should not include the excluded URLs
        rec_urls = [r["url"] for r in recs]
        for url in recent:
            assert url not in rec_urls
