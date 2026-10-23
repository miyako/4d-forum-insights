import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "forum.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    slug TEXT,
    name TEXT,
    parent_id INTEGER,
    topic_count INTEGER
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    slug TEXT,
    url TEXT,
    title TEXT,
    category_id INTEGER,
    tags TEXT,                -- JSON array of tag names
    created_at TEXT,
    bumped_at TEXT,
    last_posted_at TEXT,
    posts_count INTEGER,
    views INTEGER,
    like_count INTEGER,
    vote_count INTEGER,
    closed INTEGER,
    archived INTEGER,
    has_accepted_answer INTEGER,
    listed_at TEXT,
    -- raw content
    op_username TEXT,
    first_post_raw TEXT,
    thread_raw TEXT,
    raw_bumped_at TEXT,       -- bumped_at value when thread_raw was fetched
    raw_fetched_at TEXT,
    fetch_error TEXT,
    -- enrichment
    lang TEXT,
    title_en TEXT,
    first_post_en TEXT,
    summary_line TEXT,
    summary_paragraph TEXT,
    enrich_model TEXT,
    enriched_at TEXT,
    enrich_error TEXT
);
CREATE INDEX IF NOT EXISTS topics_category ON topics(category_id);

CREATE TABLE IF NOT EXISTS classifications (
    topic_id INTEGER,
    model TEXT,
    prompt_version TEXT,
    fr_score REAL,            -- expected level 0..4 (score question)
    fr_probs TEXT,            -- JSON {level: p}
    fr_confidence REAL,
    gap_p REAL,               -- noul: thread reveals a missing capability
    request_p REAL,           -- noul: author asks for a change to 4D
    kind TEXT,                -- choice: feature_request / bug_report / question / announcement / discussion
    kind_fr_p REAL,           -- P(kind = feature_request)
    answers TEXT,             -- full systemone answers JSON
    input_tokens INTEGER,
    created_at TEXT,
    PRIMARY KEY (topic_id, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(
    title, title_en, summary_line, summary_paragraph, first_post_en, thread_raw,
    content='topics', content_rowid='id', tokenize='unicode61'
);
"""

FEATURE_REQUEST_CATEGORY = 27


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=600)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(classifications)")}
    for col, typ in (("kind", "TEXT"), ("kind_fr_p", "REAL"), ("answers", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE classifications ADD COLUMN {col} {typ}")
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    con.commit()


def rebuild_fts(con):
    con.execute("INSERT INTO topics_fts(topics_fts) VALUES('rebuild')")
    con.commit()
