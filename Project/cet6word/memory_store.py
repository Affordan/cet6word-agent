"""SQLite-backed long-term memory for CET-6 vocabulary lookups."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


Relation = dict[str, str]
MASTERY_LEVELS = {"陌生", "模糊", "掌握"}


class MemoryStore:
    """Persist vocabulary lookup results, review state, quizzes, and graph edges."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def save_lookup(
        self,
        word: str,
        markdown: str,
        relations: Iterable[Relation] | None = None,
    ) -> None:
        normalized = self._normalize_word(word)
        if not normalized:
            raise ValueError("word cannot be empty")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO words
                    (word, markdown, lookup_count, mastery_level, review_count,
                     next_review_at, created_at, updated_at)
                VALUES (?, ?, 1, '陌生', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(word) DO UPDATE SET
                    markdown = excluded.markdown,
                    lookup_count = words.lookup_count + 1,
                    next_review_at = COALESCE(words.next_review_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized, markdown),
            )
            conn.execute("DELETE FROM relations WHERE source = ?", (normalized,))

            for relation in relations or []:
                target = self._normalize_word(relation.get("target", ""))
                if not target or target == normalized:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO words
                        (word, markdown, lookup_count, mastery_level, review_count,
                         next_review_at, created_at, updated_at)
                    VALUES (?, '', 0, '陌生', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (target,),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO relations
                        (source, target, relation, label, created_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        normalized,
                        target,
                        relation.get("relation", "related"),
                        relation.get("label", "相关"),
                    ),
                )

    def get_word(self, word: str) -> dict | None:
        normalized = self._normalize_word(word)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT word, markdown, lookup_count, mastery_level, review_count,
                    next_review_at, last_reviewed_at, created_at, updated_at
                FROM words
                WHERE word = ?
                """,
                (normalized,),
            ).fetchone()

        return dict(row) if row else None

    def list_words(self, limit: int = 80, include_pending: bool = False) -> list[dict]:
        where_clause = "" if include_pending else "WHERE lookup_count > 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT word, markdown, lookup_count, mastery_level, review_count,
                    next_review_at, last_reviewed_at, created_at, updated_at
                FROM words
                {where_clause}
                ORDER BY updated_at DESC, word ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_mastery(self, word: str, mastery_level: str) -> dict:
        normalized = self._normalize_word(word)
        if mastery_level not in MASTERY_LEVELS:
            raise ValueError("mastery_level must be one of: 陌生, 模糊, 掌握")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO words
                    (word, markdown, lookup_count, mastery_level, review_count,
                     next_review_at, created_at, updated_at)
                VALUES (?, '', 0, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(word) DO UPDATE SET
                    mastery_level = excluded.mastery_level,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized, mastery_level),
            )
        return self.get_word(normalized) or {}

    def record_review(self, word: str, correct: bool) -> dict:
        normalized = self._normalize_word(word)
        mastery_level = "掌握" if correct else "模糊"
        interval = "+7 days" if correct else "+1 day"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO words
                    (word, markdown, lookup_count, mastery_level, review_count,
                     next_review_at, last_reviewed_at, created_at, updated_at)
                VALUES (?, '', 0, ?, 1, datetime('now', ?), CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(word) DO UPDATE SET
                    mastery_level = ?,
                    review_count = words.review_count + 1,
                    last_reviewed_at = CURRENT_TIMESTAMP,
                    next_review_at = datetime('now', ?),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized, mastery_level, interval, mastery_level, interval),
            )
        return self.get_word(normalized) or {}

    def list_due_reviews(self, limit: int = 40) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT word, markdown, lookup_count, mastery_level, review_count,
                    next_review_at, last_reviewed_at, created_at, updated_at
                FROM words
                WHERE next_review_at IS NULL OR next_review_at <= CURRENT_TIMESTAMP
                ORDER BY next_review_at ASC, updated_at DESC, word ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def import_words(self, words: Iterable[str]) -> list[str]:
        imported: list[str] = []
        seen: set[str] = set()
        with self._connect() as conn:
            for word in words:
                normalized = self._normalize_word(word)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                exists = conn.execute(
                    "SELECT 1 FROM words WHERE word = ?",
                    (normalized,),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO words
                        (word, markdown, lookup_count, mastery_level, review_count,
                         next_review_at, created_at, updated_at)
                    VALUES (?, '', 0, '陌生', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (normalized,),
                )
                imported.append(normalized)
        return imported

    def save_quiz_result(
        self,
        word: str,
        question_type: str,
        correct: bool,
        question: str,
    ) -> dict:
        normalized = self._normalize_word(word)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quiz_results (word, question_type, question, correct, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (normalized, question_type, question, int(correct)),
            )
        return self.record_review(normalized, correct)

    def list_quiz_results(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT word, question_type, question, correct, created_at
                FROM quiz_results
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_graph(self, relation: str | None = None, query: str | None = None) -> dict[str, list[dict]]:
        filters = []
        params: list[str] = []
        if relation:
            filters.append("relation = ?")
            params.append(relation)
        if query:
            filters.append("(source LIKE ? OR target LIKE ?)")
            like = f"%{self._normalize_word(query)}%"
            params.extend([like, like])
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with self._connect() as conn:
            relation_rows = conn.execute(
                f"""
                SELECT source, target, relation, label
                FROM relations
                {where_clause}
                ORDER BY created_at ASC, source ASC, target ASC
                """,
                params,
            ).fetchall()
            graph_words = sorted(
                {row["source"] for row in relation_rows} | {row["target"] for row in relation_rows}
            )
            if graph_words:
                placeholders = ",".join("?" for _ in graph_words)
                word_rows = conn.execute(
                    f"""
                    SELECT word, lookup_count, mastery_level
                    FROM words
                    WHERE word IN ({placeholders})
                    ORDER BY lookup_count DESC, word ASC
                    """,
                    graph_words,
                ).fetchall()
            else:
                word_rows = conn.execute(
                    """
                    SELECT word, lookup_count, mastery_level
                    FROM words
                    WHERE lookup_count > 0
                    ORDER BY lookup_count DESC, word ASC
                    """
                ).fetchall()

        nodes = [
            {
                "id": row["word"],
                "label": row["word"],
                "weight": max(1, int(row["lookup_count"] or 0)),
                "remembered": bool(row["lookup_count"]),
                "mastery_level": row["mastery_level"],
            }
            for row in word_rows
        ]
        links = [dict(row) for row in relation_rows]
        return {"nodes": nodes, "links": links}

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS words (
                    word TEXT PRIMARY KEY,
                    markdown TEXT NOT NULL DEFAULT '',
                    lookup_count INTEGER NOT NULL DEFAULT 0,
                    mastery_level TEXT NOT NULL DEFAULT '陌生',
                    review_count INTEGER NOT NULL DEFAULT 0,
                    next_review_at TEXT,
                    last_reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS relations (
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '相关',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, target, relation),
                    FOREIGN KEY (source) REFERENCES words(word) ON DELETE CASCADE,
                    FOREIGN KEY (target) REFERENCES words(word) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS quiz_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL,
                    question_type TEXT NOT NULL,
                    question TEXT NOT NULL,
                    correct INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (word) REFERENCES words(word) ON DELETE CASCADE
                );
                """
            )
            self._ensure_columns(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(words)").fetchall()
        columns = {row["name"] for row in rows}
        migrations = {
            "mastery_level": "ALTER TABLE words ADD COLUMN mastery_level TEXT NOT NULL DEFAULT '陌生'",
            "review_count": "ALTER TABLE words ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0",
            "next_review_at": "ALTER TABLE words ADD COLUMN next_review_at TEXT",
            "last_reviewed_at": "ALTER TABLE words ADD COLUMN last_reviewed_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    @staticmethod
    def _normalize_word(word: str) -> str:
        return word.strip().lower()
