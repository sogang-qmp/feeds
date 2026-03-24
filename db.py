"""SQLite database initialization and schema management."""

import sqlite3


def init_db(db_path):
    """Initialize SQLite database and return connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            link TEXT UNIQUE,
            feed TEXT,
            folder TEXT,
            title TEXT,
            authors TEXT,
            summary TEXT,
            published TEXT,
            fetched_at TEXT,
            curated INTEGER DEFAULT 0
        )
    """)
    conn.commit()

    # Schema migration: add columns if they don't exist
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    new_columns = [
        ("source_type", "TEXT"),
        ("score", "INTEGER"),
        ("doi", "TEXT"),
        ("arxiv_id", "TEXT"),
        ("citation_count", "INTEGER"),
        ("venue", "TEXT"),
        ("year", "INTEGER"),
        ("stars", "INTEGER"),
        ("language", "TEXT"),
        ("owner", "TEXT"),
        ("repo_name", "TEXT"),
        ("velocity", "REAL"),
        ("trending_category", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col_name} {col_type}")
    conn.commit()

    return conn
