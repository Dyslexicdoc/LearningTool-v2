"""
SQLite + sqlite-vec backed vector store for node embeddings.

Layout:
  - node_meta: regular table with node_id PK, session_id, content snapshot,
               content_hash (for change detection), session/node metadata
  - node_embeddings: vec0 virtual table with the same node_id PK + float[384]

Why two tables: vec0 doesn't let us filter on arbitrary columns inside the
vector search, so the metadata lives alongside it and we JOIN on node_id.
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)


class VectorStore:
    """Thread-safe wrapper. Uses a single connection guarded by a lock —
    fine for the moderate write rate of session saves."""

    def __init__(self, db_path: Path, dim: int = 384):
        self._dim = dim
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._open()
        self._init_schema()

    def _open(self) -> sqlite3.Connection:
        try:
            import sqlite_vec
        except ImportError as e:
            raise RuntimeError(
                "sqlite-vec is not installed. Install with: pip install sqlite-vec"
            ) from e
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS node_meta (
                    node_id        TEXT PRIMARY KEY,
                    session_id     TEXT NOT NULL,
                    session_name   TEXT NOT NULL DEFAULT '',
                    prompt_text    TEXT NOT NULL DEFAULT '',
                    response_text  TEXT NOT NULL DEFAULT '',
                    prompt_mode    TEXT NOT NULL DEFAULT 'initial',
                    created_at     TEXT NOT NULL DEFAULT '',
                    content_hash   TEXT NOT NULL,
                    embedded_at    TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_meta_session ON node_meta(session_id);
            """)
            # vec0 virtual table needs separate execute() (CREATE VIRTUAL TABLE)
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS node_embeddings USING vec0(
                    node_id TEXT PRIMARY KEY,
                    embedding float[{self._dim}]
                )
            """)
            self._conn.commit()

    # ---- Upsert ----

    def upsert(
        self,
        node_id: str,
        session_id: str,
        session_name: str,
        prompt_text: str,
        response_text: str,
        prompt_mode: str,
        created_at: str,
        embedding: list[float],
        content_hash: str,
    ):
        if len(embedding) != self._dim:
            raise ValueError(f"Embedding dim mismatch: got {len(embedding)}, expected {self._dim}")
        vec_json = json.dumps(embedding)
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO node_meta
                    (node_id, session_id, session_name, prompt_text, response_text,
                     prompt_mode, created_at, content_hash, embedded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(node_id) DO UPDATE SET
                    session_id    = excluded.session_id,
                    session_name  = excluded.session_name,
                    prompt_text   = excluded.prompt_text,
                    response_text = excluded.response_text,
                    prompt_mode   = excluded.prompt_mode,
                    created_at    = excluded.created_at,
                    content_hash  = excluded.content_hash,
                    embedded_at   = datetime('now')
            """, (node_id, session_id, session_name, prompt_text, response_text,
                  prompt_mode, created_at, content_hash))
            # vec0 doesn't support ON CONFLICT — delete then insert
            self._conn.execute(
                "DELETE FROM node_embeddings WHERE node_id = ?", (node_id,)
            )
            self._conn.execute(
                "INSERT INTO node_embeddings(node_id, embedding) VALUES (?, vec_f32(?))",
                (node_id, vec_json),
            )

    # ---- Lookup helpers ----

    def get_hash(self, node_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash FROM node_meta WHERE node_id = ?", (node_id,)
            ).fetchone()
            return row["content_hash"] if row else None

    def get_embedding(self, node_id: str) -> list[float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding FROM node_embeddings WHERE node_id = ?", (node_id,)
            ).fetchone()
            if not row:
                return None
            # sqlite-vec returns raw bytes; unpack via vec_to_json
            row2 = self._conn.execute(
                "SELECT vec_to_json(embedding) AS j FROM node_embeddings WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            return json.loads(row2["j"]) if row2 else None

    def list_session_node_ids(self, session_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_id FROM node_meta WHERE session_id = ?", (session_id,)
            ).fetchall()
            return {r["node_id"] for r in rows}

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM node_meta"
            ).fetchone()["c"]

    # ---- Delete ----

    def delete_node(self, node_id: str):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM node_meta WHERE node_id = ?", (node_id,))
            self._conn.execute(
                "DELETE FROM node_embeddings WHERE node_id = ?", (node_id,)
            )

    def delete_session(self, session_id: str):
        with self._lock, self._conn:
            ids = [r["node_id"] for r in self._conn.execute(
                "SELECT node_id FROM node_meta WHERE session_id = ?", (session_id,)
            ).fetchall()]
            self._conn.execute("DELETE FROM node_meta WHERE session_id = ?", (session_id,))
            for nid in ids:
                self._conn.execute(
                    "DELETE FROM node_embeddings WHERE node_id = ?", (nid,)
                )

    def delete_orphans(self, valid_session_ids: Iterable[str]) -> int:
        """Remove any indexed sessions not in the provided set. Returns rows deleted."""
        valid = set(valid_session_ids)
        with self._lock:
            existing = {r["session_id"] for r in self._conn.execute(
                "SELECT DISTINCT session_id FROM node_meta"
            ).fetchall()}
        orphan = existing - valid
        total = 0
        for sid in orphan:
            with self._lock:
                ids = [r["node_id"] for r in self._conn.execute(
                    "SELECT node_id FROM node_meta WHERE session_id = ?", (sid,)
                ).fetchall()]
                total += len(ids)
            self.delete_session(sid)
        return total

    # ---- Search ----

    def search(
        self,
        query_embedding: list[float],
        k: int = 10,
        exclude_node_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict]:
        """Return top-k similar nodes ordered by distance ascending (closer = better).

        Each result: {node_id, session_id, session_name, prompt_text, response_text,
                      prompt_mode, created_at, distance}
        """
        if len(query_embedding) != self._dim:
            raise ValueError(f"Query dim mismatch: {len(query_embedding)} vs {self._dim}")

        # Over-fetch when filtering so we can still return k after exclusions.
        fetch_k = k + (5 if (exclude_node_id or exclude_session_id) else 0)
        vec_json = json.dumps(query_embedding)
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT v.node_id      AS node_id,
                       v.distance     AS distance,
                       m.session_id   AS session_id,
                       m.session_name AS session_name,
                       m.prompt_text  AS prompt_text,
                       m.response_text AS response_text,
                       m.prompt_mode  AS prompt_mode,
                       m.created_at   AS created_at
                FROM node_embeddings v
                JOIN node_meta m USING (node_id)
                WHERE v.embedding MATCH vec_f32(?) AND k = ?
                ORDER BY v.distance
            """, (vec_json, fetch_k)).fetchall()

        results = []
        for r in rows:
            if exclude_node_id and r["node_id"] == exclude_node_id:
                continue
            if exclude_session_id and r["session_id"] == exclude_session_id:
                continue
            results.append({
                "node_id": r["node_id"],
                "session_id": r["session_id"],
                "session_name": r["session_name"],
                "prompt_text": r["prompt_text"],
                "response_text": r["response_text"],
                "prompt_mode": r["prompt_mode"],
                "created_at": r["created_at"],
                "distance": float(r["distance"]),
            })
            if len(results) >= k:
                break
        return results

    def close(self):
        with self._lock:
            self._conn.close()
