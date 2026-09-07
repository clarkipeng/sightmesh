"""Disposable contentless FTS projection over native source locators."""
from __future__ import annotations
import sqlite3
from pathlib import Path

class EvidenceIndex:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS postings USING fts5(text, content='')")
            db.execute("CREATE TABLE IF NOT EXISTS locators (rowid INTEGER PRIMARY KEY, source_id TEXT NOT NULL, raw_start INTEGER NOT NULL, raw_end INTEGER NOT NULL, frame_start INTEGER NOT NULL, UNIQUE(source_id, frame_start, raw_start, raw_end))")
            db.execute("CREATE TABLE IF NOT EXISTS cursors (execution_id TEXT PRIMARY KEY, cursor TEXT, confirmed INTEGER NOT NULL DEFAULT 0)")
    def ingest(self, execution_id: str, source_id: str, frame_start: int, raw_start: int, text: str, *, cursor: str | None, confirmed: bool) -> None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("INSERT OR IGNORE INTO locators(source_id,raw_start,raw_end,frame_start) VALUES (?,?,?,?)", (source_id, raw_start, raw_start + len(text.encode()), frame_start))
            if row.rowcount: db.execute("INSERT INTO postings(rowid,text) VALUES(last_insert_rowid(),?)", (text,))
            if confirmed: db.execute("INSERT INTO cursors(execution_id,cursor,confirmed) VALUES(?,?,1) ON CONFLICT(execution_id) DO UPDATE SET cursor=excluded.cursor,confirmed=1", (execution_id, cursor))
    def search(self, query: str):
        # Treat caller text as terms, not FTS syntax: identifiers such as
        # ``job.id`` and punctuation must not silently become invalid queries.
        terms = " ".join('"' + term.replace('"', '""') + '"' for term in query.split() if term)
        if not terms: return []
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT l.source_id,l.raw_start,l.raw_end,l.frame_start FROM postings p JOIN locators l ON l.rowid=p.rowid WHERE postings MATCH ?", (terms,)).fetchall()
