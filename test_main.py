"""Comprehensive tests for feeds modules."""

import json
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from db import init_db
from notify import send_link_to_slack, send_error_to_slack
from rendering import generate_html, deploy_html, update_index
from scoring import build_profile_text, _score_batch, score_articles, sort_by_opml, BATCH_SIZE
from sources.rss import parse_opml, fetch_articles
import main


# --- Fixtures ---

@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def sample_opml(tmp_dir):
    content = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <opml version="1.0">
    <head><title>Test</title></head>
    <body>
        <outline text="Science" title="Science">
            <outline type="rss" text="Feed A" title="Feed A" xmlUrl="http://a.com/rss" htmlUrl="http://a.com"/>
            <outline type="rss" text="Feed B" title="Feed B" xmlUrl="http://b.com/rss" htmlUrl="http://b.com"/>
        </outline>
        <outline text="Tech" title="Tech">
            <outline type="rss" text="Feed C" title="Feed C" xmlUrl="http://c.com/rss" htmlUrl="http://c.com"/>
        </outline>
    </body>
    </opml>
    """)
    path = tmp_dir / "feeds.opml"
    path.write_text(content)
    return path


@pytest.fixture
def sample_opml_flat(tmp_dir):
    """OPML with a top-level feed (no folder)."""
    content = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <opml version="1.0">
    <head><title>Test</title></head>
    <body>
        <outline type="rss" text="Standalone" title="Standalone" xmlUrl="http://standalone.com/rss"/>
        <outline text="Folder" title="Folder">
            <outline type="rss" text="Inside" title="Inside" xmlUrl="http://inside.com/rss"/>
        </outline>
    </body>
    </opml>
    """)
    path = tmp_dir / "flat.opml"
    path.write_text(content)
    return path


@pytest.fixture
def db(tmp_dir):
    conn = init_db(tmp_dir / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def sample_config():
    return {
        "anthropic": {
            "api_key": "test-key",
            "scoring_model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
        },
        "slack": {
            "bot_token": "xoxb-test",
            "channel": "#test",
            "log_channel": "#log",
        },
        "feeds": {
            "opml_file": "feeds.opml",
            "db": "test.db",
        },
        "deploy": {
            "base_url": "https://example.com/feeds",
        },
    }


@pytest.fixture
def sample_profile():
    return {
        "researcher": {
            "name": "Test User",
            "position": "Professor",
            "affiliation": "Test University",
        },
        "research_areas": {
            "primary": ["condensed matter", "DFT"],
            "methods": ["DFT", "machine learning"],
        },
        "keywords": {
            "strong": ["phonon", "graphene"],
            "moderate": ["VASP"],
            "weak": ["physics"],
        },
        "current_interests": [
            {"topic": "AI agents for physics", "weight": "high",
             "examples": ["LLM computational physics", "autonomous DFT workflow"]},
            {"topic": "moiré phonons", "weight": "medium"},
        ],
    }


def _make_articles(n=3, feed="Feed A", folder="Science"):
    """Helper to create article dicts."""
    return [
        {
            "id": i + 1,
            "feed": feed,
            "folder": folder,
            "title": f"Article {i}",
            "link": f"http://example.com/{i}",
            "authors": f"Author {i}",
            "summary": f"Summary {i}",
            "published": f"2026-03-07T00:00:00+00:00",
        }
        for i in range(n)
    ]


def _insert_articles(conn, articles, curated=0):
    """Helper to insert articles into test DB."""
    for a in articles:
        conn.execute(
            """INSERT OR IGNORE INTO articles
               (link, feed, folder, title, authors, summary, published, fetched_at, curated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (a["link"], a["feed"], a["folder"], a["title"], a["authors"],
             a["summary"], a["published"], "2026-03-07T00:00:00+00:00", curated),
        )
    conn.commit()


# --- parse_opml ---

class TestParseOpml:
    def test_basic(self, sample_opml):
        feeds = parse_opml(sample_opml)
        assert len(feeds) == 3
        assert feeds[0]["title"] == "Feed A"
        assert feeds[0]["folder"] == "Science"
        assert feeds[1]["title"] == "Feed B"
        assert feeds[2]["title"] == "Feed C"
        assert feeds[2]["folder"] == "Tech"

    def test_preserves_order(self, sample_opml):
        feeds = parse_opml(sample_opml)
        titles = [f["title"] for f in feeds]
        assert titles == ["Feed A", "Feed B", "Feed C"]

    def test_flat_feed_no_folder(self, sample_opml_flat):
        feeds = parse_opml(sample_opml_flat)
        assert len(feeds) == 2
        assert feeds[0]["title"] == "Standalone"
        assert feeds[0]["folder"] == ""
        assert feeds[1]["title"] == "Inside"
        assert feeds[1]["folder"] == "Folder"

    def test_extracts_urls(self, sample_opml):
        feeds = parse_opml(sample_opml)
        assert feeds[0]["url"] == "http://a.com/rss"


# --- init_db ---

class TestInitDb:
    def test_creates_table(self, tmp_dir):
        conn = init_db(tmp_dir / "new.db")
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cursor.fetchall()]
        assert "articles" in tables
        conn.close()

    def test_idempotent(self, tmp_dir):
        db_path = tmp_dir / "idem.db"
        conn1 = init_db(db_path)
        conn1.close()
        conn2 = init_db(db_path)
        cursor = conn2.execute("SELECT COUNT(*) FROM articles")
        assert cursor.fetchone()[0] == 0
        conn2.close()

    def test_unique_link_constraint(self, db):
        db.execute(
            "INSERT INTO articles (link, feed, folder, title, published, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("http://x.com/1", "F", "Fo", "T", "2026-01-01", "2026-01-01"),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO articles (link, feed, folder, title, published, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("http://x.com/1", "F2", "Fo2", "T2", "2026-01-02", "2026-01-02"),
            )


# --- fetch_articles ---

def _make_feed_entry(title, link, published_parsed, authors=None, summary=""):
    """Create a mock feedparser entry."""
    entry = SimpleNamespace(
        title=title,
        link=link,
        published_parsed=published_parsed,
        updated_parsed=None,
        summary=summary,
    )
    if authors:
        entry.authors = [{"name": a} for a in authors]
    return entry


class TestFetchArticles:
    def test_inserts_new_articles(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": "Science"}
        entry = _make_feed_entry("Paper 1", "http://test.com/1", (2026, 3, 7, 0, 0, 0, 0, 0, 0))

        mock_parsed = SimpleNamespace(entries=[entry])
        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        rows = db.execute("SELECT * FROM articles").fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Paper 1"
        assert rows[0]["feed"] == "Test Feed"
        assert rows[0]["folder"] == "Science"

    def test_dedup_by_link(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Paper 1", "http://test.com/1", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)
            fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 1

    def test_skips_no_date(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("No Date", "http://test.com/nodate", None)
        entry.updated_parsed = None
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_skips_no_link(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("No Link", "", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_truncates_long_summary(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        long_summary = "x" * 600
        entry = _make_feed_entry("Long", "http://test.com/long", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        entry.summary = long_summary
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        row = db.execute("SELECT summary FROM articles").fetchone()
        assert len(row["summary"]) == 503  # 500 + "..."

    def test_extracts_authors(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Auth", "http://test.com/auth", (2026, 3, 7, 0, 0, 0, 0, 0, 0),
                                 authors=["Alice", "Bob"])
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        row = db.execute("SELECT authors FROM articles").fetchone()
        assert row["authors"] == "Alice, Bob"

    def test_handles_fetch_error(self, db):
        feed = {"title": "Bad Feed", "url": "http://bad.com/rss", "folder": ""}
        with patch("sources.rss.feedparser.parse", side_effect=Exception("Network error")):
            fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_new_articles_default_uncurated(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Paper", "http://test.com/p", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("sources.rss.feedparser.parse", return_value=mock_parsed):
            fetch_articles([feed], db)

        row = db.execute("SELECT curated FROM articles").fetchone()
        assert row["curated"] == 0


# --- build_profile_text ---

class TestBuildProfileText:
    def test_includes_fields(self, sample_profile):
        text = build_profile_text(sample_profile)
        assert "Test User" in text
        assert "Professor" in text
        assert "condensed matter" in text
        assert "phonon" in text

    def test_with_scoring_prompt(self, sample_profile):
        sample_profile["scoring_prompt"] = "Focus on materials"
        text = build_profile_text(sample_profile)
        assert "Focus on materials" in text

    def test_empty_profile(self):
        text = build_profile_text({})
        assert "N/A" in text

    def test_includes_current_interests(self, sample_profile):
        text = build_profile_text(sample_profile)
        assert "AI agents for physics" in text
        assert "HIGH PRIORITY" in text
        assert "moiré phonons" in text

    def test_no_current_interests(self):
        profile = {"researcher": {"name": "X"}, "research_areas": {}, "keywords": {}}
        text = build_profile_text(profile)
        assert "Current interests" not in text
        assert "N/A" in text or "X" in text


# --- _score_batch ---

class TestScoreBatch:
    def test_parses_response(self, sample_profile):
        batch = _make_articles(2)
        profile_text = build_profile_text(sample_profile)
        scores_json = json.dumps([{"index": 0, "score": 4}, {"index": 1, "score": 2}])

        with patch("scoring.run_claude", return_value=scores_json):
            score_map = _score_batch(batch, profile_text)
        assert score_map == {0: 4, 1: 2}

    def test_strips_markdown_fences(self, sample_profile):
        batch = _make_articles(1)
        profile_text = build_profile_text(sample_profile)
        response = '```json\n[{"index": 0, "score": 5}]\n```'

        with patch("scoring.run_claude", return_value=response):
            score_map = _score_batch(batch, profile_text)
        assert score_map == {0: 5}


# --- score_articles ---

class TestScoreArticles:
    def test_empty_articles(self, sample_profile, sample_config):
        result = score_articles([], sample_profile, sample_config)
        assert result == []

    def test_batching(self, sample_profile, sample_config):
        articles = _make_articles(5)
        call_count = 0

        def mock_run_claude(prompt, model="haiku", timeout=300):
            nonlocal call_count
            call_count += 1
            import re
            indices = re.findall(r'\[(\d+)\]', prompt)
            n = len(indices)
            return json.dumps([{"index": i, "score": 3} for i in range(n)])

        with patch("scoring.BATCH_SIZE", 3):
            with patch("scoring.run_claude", side_effect=mock_run_claude):
                result = score_articles(articles, sample_profile, sample_config)

        assert len(result) == 5
        assert call_count == 2  # 3 + 2
        assert all(a["score"] == 3 for a in result)

    def test_missing_score_defaults_to_1(self, sample_profile, sample_config):
        articles = _make_articles(2)
        scores_json = json.dumps([{"index": 0, "score": 5}])

        with patch("scoring.run_claude", return_value=scores_json):
            result = score_articles(articles, sample_profile, sample_config)

        assert result[0]["score"] == 5
        assert result[1]["score"] == 1  # default


# --- sort_by_opml ---

class TestSortByOpml:
    def test_respects_opml_order(self, sample_opml):
        feeds = parse_opml(sample_opml)
        scored = [
            {"feed": "Feed C", "folder": "Tech", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
            {"feed": "Feed B", "folder": "Science", "score": 4},
        ]
        result = sort_by_opml(scored, feeds)
        assert [r["feed"] for r in result] == ["Feed A", "Feed B", "Feed C"]

    def test_score_desc_within_feed(self, sample_opml):
        feeds = parse_opml(sample_opml)
        scored = [
            {"feed": "Feed A", "folder": "Science", "score": 2},
            {"feed": "Feed A", "folder": "Science", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
        ]
        result = sort_by_opml(scored, feeds)
        assert [r["score"] for r in result] == [5, 3, 2]

    def test_unknown_feed_goes_last(self, sample_opml):
        feeds = parse_opml(sample_opml)
        scored = [
            {"feed": "Unknown", "folder": "???", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
        ]
        result = sort_by_opml(scored, feeds)
        assert result[0]["feed"] == "Feed A"
        assert result[1]["feed"] == "Unknown"


# --- generate_html ---

class TestGenerateHtml:
    def test_contains_title(self):
        html = generate_html([], "2026-03-07")
        assert "2026-03-07" in html

    def test_contains_articles(self):
        articles = [
            {"feed": "F", "folder": "Fo", "title": "My Paper", "link": "http://x.com",
             "authors": "Alice", "score": 4},
        ]
        html = generate_html(articles, "2026-03-07")
        assert "My Paper" in html
        assert "http://x.com" in html
        assert "Alice" in html
        assert 'class="score s4"' in html

    def test_folder_headers(self):
        articles = [
            {"feed": "F", "folder": "Science", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = generate_html(articles, "2026-03-07")
        assert "Science" in html
        assert 'class="folder"' in html

    def test_no_folder_header_when_empty(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = generate_html(articles, "2026-03-07")
        assert 'class="folder"' not in html

    def test_article_count_in_feed_header(self):
        articles = [
            {"feed": "Feed A", "folder": "S", "title": f"P{i}", "link": f"http://x.com/{i}",
             "authors": "", "score": 3}
            for i in range(5)
        ]
        html = generate_html(articles, "2026-03-07")
        assert "(5)" in html

    def test_links_open_in_new_tab(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = generate_html(articles, "2026-03-07")
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html

    def test_score_css_classes(self):
        articles = [
            {"feed": "F", "folder": "", "title": f"P{s}", "link": f"http://x.com/{s}",
             "authors": "", "score": s}
            for s in range(1, 6)
        ]
        html = generate_html(articles, "2026-03-07")
        for s in range(1, 6):
            assert f"s{s}" in html

    def test_no_author_span_when_empty(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = generate_html(articles, "2026-03-07")
        assert 'class="authors"' not in html


# --- deploy_html / update_index ---

class TestDeployHtml:
    def test_writes_file(self, tmp_dir):
        path = deploy_html("<html>test</html>", "2026-03-07", tmp_dir)
        assert path.exists()
        assert path.read_text() == "<html>test</html>"

    def test_creates_html_dir(self, tmp_dir):
        deploy_html("<html>test</html>", "2026-03-07", tmp_dir)
        assert (tmp_dir / "html").is_dir()

    def test_updates_index(self, tmp_dir):
        deploy_html("<html>a</html>", "2026-03-06", tmp_dir)
        deploy_html("<html>b</html>", "2026-03-07", tmp_dir)
        index = (tmp_dir / "html" / "index.html").read_text()
        assert "2026-03-07" in index
        assert "2026-03-06" in index
        # Reverse order: 03-07 should come before 03-06
        assert index.index("2026-03-07") < index.index("2026-03-06")


class TestUpdateIndex:
    def test_lists_date_files(self, tmp_dir):
        html_dir = tmp_dir / "html"
        html_dir.mkdir()
        (html_dir / "2026-03-05.html").write_text("a")
        (html_dir / "2026-03-06.html").write_text("b")
        update_index(html_dir)

        index = (html_dir / "index.html").read_text()
        assert "2026-03-05" in index
        assert "2026-03-06" in index

    def test_ignores_index_html(self, tmp_dir):
        html_dir = tmp_dir / "html"
        html_dir.mkdir()
        (html_dir / "2026-03-05.html").write_text("a")
        (html_dir / "index.html").write_text("old")
        update_index(html_dir)

        index = (html_dir / "index.html").read_text()
        assert "index.html" not in index.replace("<title>", "")  # not listed as a link

    def test_empty_dir(self, tmp_dir):
        html_dir = tmp_dir / "html"
        html_dir.mkdir()
        update_index(html_dir)
        index = (html_dir / "index.html").read_text()
        assert "<ul></ul>" in index or "<ul>\n</ul>" in index or "<li>" not in index


# --- send_link_to_slack ---

class TestSendLinkToSlack:
    def test_posts_message(self, sample_config):
        with patch("notify.WebClient") as MockWC:
            mock_client = MockWC.return_value
            send_link_to_slack(sample_config, "2026-03-07")

            mock_client.chat_postMessage.assert_called_once()
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert "2026-03-07" in call_kwargs["text"]
            assert "example.com/feeds/2026-03-07.html" in call_kwargs["text"]
            assert call_kwargs["unfurl_links"] is False


# --- send_error_to_slack ---

class TestSendErrorToSlack:
    def test_sends_error(self, sample_config):
        with patch("notify.WebClient") as MockWC:
            mock_client = MockWC.return_value
            send_error_to_slack(sample_config, "something broke")

            mock_client.chat_postMessage.assert_called_once()
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert "something broke" in call_kwargs["text"]
            assert call_kwargs["channel"] == "#log"

    def test_does_not_raise_on_failure(self, sample_config):
        with patch("notify.WebClient", side_effect=Exception("fail")):
            send_error_to_slack(sample_config, "error")  # should not raise


# --- cmd_curate (integration-ish) ---

class TestCmdCurate:
    def test_marks_articles_as_curated(self, tmp_dir, sample_config, sample_profile):
        # Setup DB with uncurated articles
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        sample_config["feeds"]["opml_file"] = "feeds.opml"
        conn = init_db(db_path)
        articles = _make_articles(3)
        _insert_articles(conn, articles, curated=0)
        conn.close()

        # Write OPML
        opml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0"><head/><body>
        <outline text="Science" title="Science">
            <outline type="rss" text="Feed A" title="Feed A" xmlUrl="http://a.com/rss"/>
        </outline>
        </body></opml>
        """)
        (tmp_dir / "feeds.opml").write_text(opml)

        # Write profile
        import yaml
        (tmp_dir / "research_profile.yaml").write_text(yaml.dump(sample_profile))

        # Mock scoring and recommendations
        scores_json = json.dumps([{"index": i, "score": 3} for i in range(3)])

        args = SimpleNamespace(dry_run=True, profile="research_profile.yaml")

        with patch("scoring.run_claude", return_value=scores_json), \
             patch("recommend.run_claude", return_value='[]'):
            main.cmd_curate(args, tmp_dir, sample_config)

        # Check all marked as curated
        conn = init_db(db_path)
        pending = conn.execute("SELECT COUNT(*) FROM articles WHERE curated=0").fetchone()[0]
        assert pending == 0
        conn.close()

        # Check HTML was generated
        html_files = list((tmp_dir / "html").glob("2*.html"))
        assert len(html_files) == 1

    def test_nothing_to_curate(self, tmp_dir, sample_config):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        conn = init_db(db_path)
        conn.close()

        args = SimpleNamespace(dry_run=True, profile="research_profile.yaml")
        # Should not raise
        main.cmd_curate(args, tmp_dir, sample_config)
        # No HTML generated
        assert not (tmp_dir / "html").exists()
