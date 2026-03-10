"""Comprehensive tests for feeds main.py."""

import json
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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
    conn = main.init_db(tmp_dir / "test.db")
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
            "user_id": "U0TEST",
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
def sample_topics():
    return {
        "topics": [
            "DFT and electronic structure",
            "Phonon calculations",
        ],
        "summary_threshold": 4,
    }


def _make_articles(n=3, feed="Feed A", folder="Science", published=None):
    """Helper to create article dicts."""
    if published is None:
        published = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": i + 1,
            "feed": feed,
            "folder": folder,
            "title": f"Article {i}",
            "link": f"http://example.com/{i}",
            "authors": f"Author {i}",
            "summary": f"Summary {i}",
            "published": published,
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
        feeds = main.parse_opml(sample_opml)
        assert len(feeds) == 3
        assert feeds[0]["title"] == "Feed A"
        assert feeds[0]["folder"] == "Science"
        assert feeds[1]["title"] == "Feed B"
        assert feeds[2]["title"] == "Feed C"
        assert feeds[2]["folder"] == "Tech"

    def test_preserves_order(self, sample_opml):
        feeds = main.parse_opml(sample_opml)
        titles = [f["title"] for f in feeds]
        assert titles == ["Feed A", "Feed B", "Feed C"]

    def test_flat_feed_no_folder(self, sample_opml_flat):
        feeds = main.parse_opml(sample_opml_flat)
        assert len(feeds) == 2
        assert feeds[0]["title"] == "Standalone"
        assert feeds[0]["folder"] == ""
        assert feeds[1]["title"] == "Inside"
        assert feeds[1]["folder"] == "Folder"

    def test_extracts_urls(self, sample_opml):
        feeds = main.parse_opml(sample_opml)
        assert feeds[0]["url"] == "http://a.com/rss"


# --- init_db ---

class TestInitDb:
    def test_creates_table(self, tmp_dir):
        conn = main.init_db(tmp_dir / "new.db")
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cursor.fetchall()]
        assert "articles" in tables
        conn.close()

    def test_idempotent(self, tmp_dir):
        db_path = tmp_dir / "idem.db"
        conn1 = main.init_db(db_path)
        conn1.close()
        conn2 = main.init_db(db_path)
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
        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        rows = db.execute("SELECT * FROM articles").fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Paper 1"
        assert rows[0]["feed"] == "Test Feed"
        assert rows[0]["folder"] == "Science"

    def test_dedup_by_link(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Paper 1", "http://test.com/1", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)
            main.fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 1

    def test_skips_no_date(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("No Date", "http://test.com/nodate", None)
        entry.updated_parsed = None
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_skips_no_link(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("No Link", "", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_truncates_long_summary(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        long_summary = "x" * 2500
        entry = _make_feed_entry("Long", "http://test.com/long", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        entry.summary = long_summary
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        row = db.execute("SELECT summary FROM articles").fetchone()
        assert len(row["summary"]) == 2003  # 2000 + "..."

    def test_extracts_authors(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Auth", "http://test.com/auth", (2026, 3, 7, 0, 0, 0, 0, 0, 0),
                                 authors=["Alice", "Bob"])
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        row = db.execute("SELECT authors FROM articles").fetchone()
        assert row["authors"] == "Alice, Bob"

    def test_handles_fetch_error(self, db):
        feed = {"title": "Bad Feed", "url": "http://bad.com/rss", "folder": ""}
        with patch("feedparser.parse", side_effect=Exception("Network error")):
            main.fetch_articles([feed], db)

        count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 0

    def test_new_articles_default_uncurated(self, db):
        feed = {"title": "Test Feed", "url": "http://test.com/rss", "folder": ""}
        entry = _make_feed_entry("Paper", "http://test.com/p", (2026, 3, 7, 0, 0, 0, 0, 0, 0))
        mock_parsed = SimpleNamespace(entries=[entry])

        with patch("feedparser.parse", return_value=mock_parsed):
            main.fetch_articles([feed], db)

        row = db.execute("SELECT curated FROM articles").fetchone()
        assert row["curated"] == 0


# --- _build_topics_text ---

class TestBuildTopicsText:
    def test_includes_topics(self, sample_topics):
        text = main._build_topics_text(sample_topics)
        assert "DFT and electronic structure" in text
        assert "Phonon calculations" in text

    def test_numbered_list(self, sample_topics):
        text = main._build_topics_text(sample_topics)
        assert "1." in text
        assert "2." in text

    def test_with_scoring_prompt(self, sample_topics):
        sample_topics["scoring_prompt"] = "Focus on methods"
        text = main._build_topics_text(sample_topics)
        assert "Focus on methods" in text

    def test_empty_topics(self):
        text = main._build_topics_text({})
        assert "Topics of interest" in text


# --- _score_batch ---

def _mock_llm_message(text, input_tokens=100, output_tokens=50):
    """Helper to create a mock Anthropic message with usage."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=text)]
    mock_msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return mock_msg


class TestScoreBatch:
    def test_parses_response(self, sample_topics):
        batch = _make_articles(2)
        topics_text = main._build_topics_text(sample_topics)
        scores_json = json.dumps([{"index": 0, "score": 4}, {"index": 1, "score": 2}])

        mock_msg = _mock_llm_message(scores_json)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        score_map, inp, out = main._score_batch(batch, topics_text, mock_client, "test-model", 4096)
        assert score_map == {0: 4, 1: 2}
        assert inp == 100
        assert out == 50

    def test_strips_markdown_fences(self, sample_topics):
        batch = _make_articles(1)
        topics_text = main._build_topics_text(sample_topics)
        response = '```json\n[{"index": 0, "score": 5}]\n```'

        mock_msg = _mock_llm_message(response)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        score_map, _, _ = main._score_batch(batch, topics_text, mock_client, "test-model", 4096)
        assert score_map == {0: 5}


# --- score_articles ---

class TestScoreArticles:
    def test_empty_articles(self, sample_topics, sample_config):
        result = main.score_articles([], sample_topics, sample_config)
        assert result == []

    def test_batching(self, sample_topics, sample_config):
        articles = _make_articles(5)
        scores_json = lambda n: json.dumps([{"index": i, "score": 3} for i in range(n)])

        call_count = 0
        def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            prompt = kwargs["messages"][0]["content"]
            import re
            indices = re.findall(r'\[(\d+)\]', prompt)
            n = len(indices)
            return _mock_llm_message(scores_json(n))

        with patch("main.BATCH_SIZE", 3):
            with patch("anthropic.Anthropic") as MockClient:
                MockClient.return_value.messages.create = mock_create
                result = main.score_articles(articles, sample_topics, sample_config)

        assert len(result) == 5
        assert call_count == 2  # 3 + 2
        assert all(a["score"] == 3 for a in result)

    def test_missing_score_defaults_to_1(self, sample_topics, sample_config):
        articles = _make_articles(2)
        scores_json = json.dumps([{"index": 0, "score": 5}])

        mock_msg = _mock_llm_message(scores_json)

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_msg
            result = main.score_articles(articles, sample_topics, sample_config)

        assert result[0]["score"] == 5
        assert result[1]["score"] == 1  # default


# --- _summarize_batch ---

class TestSummarizeBatch:
    def test_parses_response(self):
        batch = _make_articles(2)
        summaries_json = json.dumps([
            {"index": 0, "summary": "Summary of article 0", "key_points": ["Point A"]},
            {"index": 1, "summary": "Summary of article 1", "key_points": ["Point B", "Point C"]},
        ])

        mock_msg = _mock_llm_message(summaries_json)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        smap, inp, out = main._summarize_batch(batch, mock_client, "test-model", 4096)
        assert 0 in smap
        assert smap[0]["summary"] == "Summary of article 0"
        assert smap[1]["key_points"] == ["Point B", "Point C"]

    def test_strips_markdown_fences(self):
        batch = _make_articles(1)
        response = '```json\n[{"index": 0, "summary": "Test", "key_points": []}]\n```'

        mock_msg = _mock_llm_message(response)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        smap, _, _ = main._summarize_batch(batch, mock_client, "test-model", 4096)
        assert smap[0]["summary"] == "Test"


# --- summarize_articles ---

class TestSummarizeArticles:
    def test_no_high_score_articles(self, sample_config):
        scored = [
            {**a, "score": 2}
            for a in _make_articles(3)
        ]
        result = main.summarize_articles(scored, sample_config, threshold=4)
        assert len(result) == 3
        assert all(a["summary_info"] == {} for a in result)

    def test_attaches_summary_info(self, sample_config):
        articles = _make_articles(3)
        scored = [
            {**articles[0], "score": 5},
            {**articles[1], "score": 4},
            {**articles[2], "score": 2},
        ]

        summaries_json = json.dumps([
            {"index": 0, "summary": "High relevance", "key_points": ["A"]},
            {"index": 1, "summary": "Also relevant", "key_points": ["B"]},
        ])
        mock_msg = _mock_llm_message(summaries_json)

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_msg
            result = main.summarize_articles(scored, sample_config, threshold=4)

        assert result[0]["summary_info"]["summary"] == "High relevance"
        assert result[1]["summary_info"]["summary"] == "Also relevant"
        assert result[2]["summary_info"] == {}

    def test_batching(self, sample_config):
        articles = _make_articles(5)
        scored = [{**a, "score": 5} for a in articles]

        summaries_json = lambda n: json.dumps([
            {"index": i, "summary": f"S{i}", "key_points": []}
            for i in range(n)
        ])

        call_count = 0
        def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            prompt = kwargs["messages"][0]["content"]
            import re
            indices = re.findall(r'\[(\d+)\]', prompt)
            return _mock_llm_message(summaries_json(len(indices)))

        with patch("main.SUMMARY_BATCH_SIZE", 3):
            with patch("anthropic.Anthropic") as MockClient:
                MockClient.return_value.messages.create = mock_create
                result = main.summarize_articles(scored, sample_config, threshold=4)

        assert call_count == 2  # 3 + 2
        assert all(a.get("summary_info") for a in result)


# --- generate_summary_md ---

class TestGenerateSummaryMd:
    def test_empty_when_no_high_scores(self):
        scored = [{**a, "score": 2, "summary_info": {}} for a in _make_articles(3)]
        md = main.generate_summary_md(scored, "2026-03-07", threshold=4)
        assert md == ""

    def test_contains_article_info(self):
        articles = _make_articles(1)
        scored = [{
            **articles[0],
            "score": 5,
            "summary_info": {
                "summary": "Important finding about DFT.",
                "key_points": ["Point 1", "Point 2"],
            },
        }]
        md = main.generate_summary_md(scored, "2026-03-07", threshold=4)
        assert "Article 0" in md
        assert "Important finding about DFT." in md
        assert "Point 1" in md
        assert "Score:** 5" in md

    def test_includes_header(self):
        scored = [{
            **_make_articles(1)[0],
            "score": 4,
            "summary_info": {"summary": "Test", "key_points": []},
        }]
        md = main.generate_summary_md(scored, "2026-03-07", threshold=4)
        assert "# Feeds Summary" in md
        assert "2026-03-07" in md
        assert "1 articles with score >= 4" in md


# --- deploy_summary ---

class TestRenderSummaryHtml:
    def test_contains_title(self):
        html = main.render_summary_html("# Test", "2026-03-07")
        assert "Summary — 2026-03-07" in html

    def test_renders_markdown(self):
        html = main.render_summary_html("**bold text**", "2026-03-07")
        assert "<strong>bold text</strong>" in html

    def test_includes_nav_link(self):
        html = main.render_summary_html("# Test", "2026-03-07")
        assert "All feeds" in html

    def test_includes_katex(self):
        html = main.render_summary_html("# Test", "2026-03-07")
        assert "katex" in html


class TestDeploySummary:
    def test_writes_md_and_html(self, tmp_dir):
        path = main.deploy_summary("# Test", "2026-03-07", tmp_dir)
        assert path.exists()
        assert path.read_text() == "# Test"
        html_path = tmp_dir / "summaries" / "2026-03-07.html"
        assert html_path.exists()
        assert "<strong>" not in path.read_text()  # md is raw
        assert "<!DOCTYPE html>" in html_path.read_text()

    def test_creates_summaries_dir(self, tmp_dir):
        main.deploy_summary("# Test", "2026-03-07", tmp_dir)
        assert (tmp_dir / "summaries").is_dir()


# --- sort_by_opml ---

class TestSortByOpml:
    def test_respects_opml_order(self, sample_opml):
        feeds = main.parse_opml(sample_opml)
        scored = [
            {"feed": "Feed C", "folder": "Tech", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
            {"feed": "Feed B", "folder": "Science", "score": 4},
        ]
        result = main.sort_by_opml(scored, feeds)
        assert [r["feed"] for r in result] == ["Feed A", "Feed B", "Feed C"]

    def test_score_desc_within_feed(self, sample_opml):
        feeds = main.parse_opml(sample_opml)
        scored = [
            {"feed": "Feed A", "folder": "Science", "score": 2},
            {"feed": "Feed A", "folder": "Science", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
        ]
        result = main.sort_by_opml(scored, feeds)
        assert [r["score"] for r in result] == [5, 3, 2]

    def test_unknown_feed_goes_last(self, sample_opml):
        feeds = main.parse_opml(sample_opml)
        scored = [
            {"feed": "Unknown", "folder": "???", "score": 5},
            {"feed": "Feed A", "folder": "Science", "score": 3},
        ]
        result = main.sort_by_opml(scored, feeds)
        assert result[0]["feed"] == "Feed A"
        assert result[1]["feed"] == "Unknown"


# --- generate_html ---

class TestGenerateHtml:
    def test_contains_title(self):
        html = main.generate_html([], "2026-03-07")
        assert "2026-03-07" in html

    def test_contains_articles(self):
        articles = [
            {"feed": "F", "folder": "Fo", "title": "My Paper", "link": "http://x.com",
             "authors": "Alice", "score": 4},
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert "My Paper" in html
        assert "http://x.com" in html
        assert "Alice" in html
        assert 'class="score s4"' in html

    def test_folder_headers(self):
        articles = [
            {"feed": "F", "folder": "Science", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert "Science" in html
        assert 'class="folder"' in html

    def test_no_folder_header_when_empty(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert 'class="folder"' not in html

    def test_article_count_in_feed_header(self):
        articles = [
            {"feed": "Feed A", "folder": "S", "title": f"P{i}", "link": f"http://x.com/{i}",
             "authors": "", "score": 3}
            for i in range(5)
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert "(5)" in html

    def test_links_open_in_new_tab(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html

    def test_score_css_classes(self):
        articles = [
            {"feed": "F", "folder": "", "title": f"P{s}", "link": f"http://x.com/{s}",
             "authors": "", "score": s}
            for s in range(1, 6)
        ]
        html = main.generate_html(articles, "2026-03-07")
        for s in range(1, 6):
            assert f"s{s}" in html

    def test_no_author_span_when_empty(self):
        articles = [
            {"feed": "F", "folder": "", "title": "P", "link": "http://x.com",
             "authors": "", "score": 3},
        ]
        html = main.generate_html(articles, "2026-03-07")
        assert 'class="authors"' not in html

    def test_summary_link_shown(self):
        html = main.generate_html([], "2026-03-07", summary_count=5)
        assert "Summary (5 articles)" in html
        assert "summaries/2026-03-07.html" in html

    def test_no_summary_link_when_zero(self):
        html = main.generate_html([], "2026-03-07", summary_count=0)
        assert "Summary (" not in html


# --- deploy_html / update_index ---

class TestDeployHtml:
    def test_writes_file(self, tmp_dir):
        path = main.deploy_html("<html>test</html>", "2026-03-07", tmp_dir)
        assert path.exists()
        assert path.read_text() == "<html>test</html>"

    def test_creates_html_dir(self, tmp_dir):
        main.deploy_html("<html>test</html>", "2026-03-07", tmp_dir)
        assert (tmp_dir / "html").is_dir()

    def test_updates_index(self, tmp_dir):
        main.deploy_html("<html>a</html>", "2026-03-06", tmp_dir)
        main.deploy_html("<html>b</html>", "2026-03-07", tmp_dir)
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
        main.update_index(html_dir)

        index = (html_dir / "index.html").read_text()
        assert "2026-03-05" in index
        assert "2026-03-06" in index

    def test_ignores_index_html(self, tmp_dir):
        html_dir = tmp_dir / "html"
        html_dir.mkdir()
        (html_dir / "2026-03-05.html").write_text("a")
        (html_dir / "index.html").write_text("old")
        main.update_index(html_dir)

        index = (html_dir / "index.html").read_text()
        assert "index.html" not in index.replace("<title>", "")  # not listed as a link

    def test_empty_dir(self, tmp_dir):
        html_dir = tmp_dir / "html"
        html_dir.mkdir()
        main.update_index(html_dir)
        index = (html_dir / "index.html").read_text()
        assert "<ul></ul>" in index or "<ul>\n</ul>" in index or "<li>" not in index


# --- send_link_to_slack ---

class TestSendLinkToSlack:
    def test_sends_dm(self, sample_config):
        with patch("main.WebClient") as MockWC:
            mock_client = MockWC.return_value
            main.send_link_to_slack(sample_config, "2026-03-07")

            mock_client.chat_postMessage.assert_called_once()
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert call_kwargs["channel"] == "U0TEST"
            assert "2026-03-07" in call_kwargs["text"]
            assert "example.com/feeds/2026-03-07.html" in call_kwargs["text"]
            assert call_kwargs["unfurl_links"] is False

    def test_includes_summary_link(self, sample_config):
        with patch("main.WebClient") as MockWC:
            mock_client = MockWC.return_value
            main.send_link_to_slack(sample_config, "2026-03-07", has_summary=True)

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert "summaries/2026-03-07.html" in call_kwargs["text"]

    def test_no_summary_link_by_default(self, sample_config):
        with patch("main.WebClient") as MockWC:
            mock_client = MockWC.return_value
            main.send_link_to_slack(sample_config, "2026-03-07")

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert "Summary" not in call_kwargs["text"]


# --- send_error_to_slack ---

class TestSendErrorToSlack:
    def test_sends_error(self, sample_config):
        with patch("main.WebClient") as MockWC:
            mock_client = MockWC.return_value
            main.send_error_to_slack(sample_config, "something broke")

            mock_client.chat_postMessage.assert_called_once()
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert "something broke" in call_kwargs["text"]
            assert call_kwargs["channel"] == "#log"

    def test_does_not_raise_on_failure(self, sample_config):
        with patch("main.WebClient", side_effect=Exception("fail")):
            main.send_error_to_slack(sample_config, "error")  # should not raise


# --- cmd_curate (integration-ish) ---

def _curate_opml(tmp_dir):
    """Helper: write a minimal OPML file for curate tests."""
    opml = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <opml version="1.0"><head/><body>
    <outline text="Science" title="Science">
        <outline type="rss" text="Feed A" title="Feed A" xmlUrl="http://a.com/rss"/>
    </outline>
    </body></opml>
    """)
    (tmp_dir / "feeds.opml").write_text(opml)


def _curate_topics(tmp_dir, sample_topics):
    """Helper: write topics.yaml for curate tests."""
    import yaml
    (tmp_dir / "topics.yaml").write_text(yaml.dump(sample_topics))


class TestCmdCurate:
    def test_scores_and_generates_html(self, tmp_dir, sample_config, sample_topics):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        sample_config["feeds"]["opml_file"] = "feeds.opml"
        conn = main.init_db(db_path)
        _insert_articles(conn, _make_articles(3), curated=0)
        conn.close()

        _curate_opml(tmp_dir)
        _curate_topics(tmp_dir, sample_topics)

        scores_json = json.dumps([{"index": i, "score": 3} for i in range(3)])
        mock_msg = _mock_llm_message(scores_json)
        args = SimpleNamespace(dry_run=True, topics="topics.yaml")

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_msg
            main.cmd_curate(args, tmp_dir, sample_config)

        # Scores saved to DB
        conn = main.init_db(db_path)
        scored = conn.execute("SELECT COUNT(*) FROM articles WHERE score IS NOT NULL").fetchone()[0]
        assert scored == 3
        conn.close()

        # HTML generated
        html_files = list((tmp_dir / "html").glob("2*.html"))
        assert len(html_files) == 1

    def test_reuses_cached_scores(self, tmp_dir, sample_config, sample_topics):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        sample_config["feeds"]["opml_file"] = "feeds.opml"
        conn = main.init_db(db_path)
        articles = _make_articles(3)
        _insert_articles(conn, articles, curated=0)
        # Pre-set scores for 2 articles
        conn.execute("UPDATE articles SET score=4 WHERE id=1")
        conn.execute("UPDATE articles SET score=2 WHERE id=2")
        conn.commit()
        conn.close()

        _curate_opml(tmp_dir)
        _curate_topics(tmp_dir, sample_topics)

        # Only 1 article needs scoring (id=3)
        scores_json = json.dumps([{"index": 0, "score": 5}])
        mock_msg = _mock_llm_message(scores_json)
        args = SimpleNamespace(dry_run=True, topics="topics.yaml")

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_msg
            main.cmd_curate(args, tmp_dir, sample_config)

            # Scoring API should be called only once (for 1 unscored article)
            score_calls = [
                c for c in MockClient.return_value.messages.create.call_args_list
                if "Score each article" in str(c)
            ]
            assert len(score_calls) == 1

    def test_generates_summary_for_high_scores(self, tmp_dir, sample_config, sample_topics):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        sample_config["feeds"]["opml_file"] = "feeds.opml"
        conn = main.init_db(db_path)
        _insert_articles(conn, _make_articles(3), curated=0)
        conn.close()

        _curate_opml(tmp_dir)
        _curate_topics(tmp_dir, sample_topics)

        def mock_create(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            import re
            indices = re.findall(r'\[(\d+)\]', prompt)
            n = len(indices)
            if "Score each article" in prompt:
                return _mock_llm_message(
                    json.dumps([{"index": i, "score": 5} for i in range(n)])
                )
            else:
                return _mock_llm_message(
                    json.dumps([{"index": i, "summary": f"S{i}", "key_points": ["P"]} for i in range(n)])
                )

        args = SimpleNamespace(dry_run=True, topics="topics.yaml")

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create = mock_create
            main.cmd_curate(args, tmp_dir, sample_config)

        summary_files = list((tmp_dir / "summaries").glob("*.md"))
        assert len(summary_files) == 1

    def test_only_includes_recent_articles(self, tmp_dir, sample_config, sample_topics):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        sample_config["feeds"]["opml_file"] = "feeds.opml"
        conn = main.init_db(db_path)

        old_date = (datetime.now(timezone.utc) - main.timedelta(days=5)).isoformat()
        old_articles = _make_articles(3, published=old_date)
        fresh_articles = [
            {**a, "id": a["id"] + 10, "link": f"http://example.com/fresh/{a['id']}"}
            for a in _make_articles(2)
        ]
        _insert_articles(conn, old_articles, curated=0)
        _insert_articles(conn, fresh_articles, curated=0)
        conn.close()

        _curate_opml(tmp_dir)
        _curate_topics(tmp_dir, sample_topics)

        # Only 2 fresh articles should be scored
        scores_json = json.dumps([{"index": i, "score": 3} for i in range(2)])
        mock_msg = _mock_llm_message(scores_json)
        args = SimpleNamespace(dry_run=True, topics="topics.yaml")

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_msg
            main.cmd_curate(args, tmp_dir, sample_config)

        # Old articles should NOT have scores (not processed)
        conn = main.init_db(db_path)
        old_scored = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE published < ? AND score IS NOT NULL",
            (old_date,),
        ).fetchone()[0]
        assert old_scored == 0
        # Fresh articles should have scores
        fresh_scored = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE score IS NOT NULL"
        ).fetchone()[0]
        assert fresh_scored == 2
        conn.close()

    def test_nothing_to_curate(self, tmp_dir, sample_config):
        db_path = tmp_dir / "test.db"
        sample_config["feeds"]["db"] = "test.db"
        conn = main.init_db(db_path)
        conn.close()

        args = SimpleNamespace(dry_run=True, topics="topics.yaml")
        main.cmd_curate(args, tmp_dir, sample_config)
        assert not (tmp_dir / "html").exists()
