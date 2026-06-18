"""
High-level embedding service. Wires the embedder backend, the vector store,
and the SessionManager hooks together.

Indexing happens on session save:
  - SessionManager.save() fires an on_save callback
  - EmbeddingService.on_session_save_async() schedules a background task
  - The task computes content hashes, embeds only changed/new nodes, upserts

A node is indexable when:
  - status == "complete"   (not pending or running)
  - response_text is non-empty
  - the (prompt + response + mode) hash differs from the last embedded version

The first-run reindex walks all session.json files. Trashed sessions are NOT
indexed (their files live in learning_sessions_trash and aren't passed in).
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from embedder import EmbedderBackend
from vector_store import VectorStore


logger = logging.getLogger(__name__)

# Cap the text we feed the embedder. MiniLM has a 512-token context; ~2000
# chars is a safe upper bound that fits comfortably. Tail content beyond this
# isn't reflected in the embedding — acceptable v1 tradeoff.
EMBED_TEXT_CHAR_CAP = 2000


def content_hash(node: dict) -> str:
    """Stable hash over the fields that affect the embedding."""
    h = hashlib.sha256()
    h.update((node.get("prompt_text") or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((node.get("response_text") or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((node.get("prompt_mode") or "initial").encode("utf-8"))
    return h.hexdigest()


def _embed_text_for(node: dict) -> str:
    """The text we actually embed: prompt + truncated response."""
    prompt = (node.get("prompt_text") or "").strip()
    response = (node.get("response_text") or "").strip()[:EMBED_TEXT_CHAR_CAP]
    if prompt and response:
        return f"{prompt}\n\n{response}"
    return prompt or response


def _is_indexable(node: dict) -> bool:
    if node.get("status") != "complete":
        return False
    return bool((node.get("response_text") or "").strip())


class EmbeddingService:
    def __init__(
        self,
        sessions_dir: Path,
        db_path: Path,
        backend: EmbedderBackend,
    ):
        self._sessions_dir = sessions_dir
        self._backend = backend
        self._store = VectorStore(db_path, dim=backend.dimension())
        # Coalesce simultaneous indexing requests for the same session
        self._inflight: dict[str, asyncio.Task] = {}
        self._inflight_lock = asyncio.Lock()

    # ---- Hooks called by SessionManager / app ----

    def on_session_save_sync(self, session_id: str, data: dict):
        """Synchronous indexing pass — used by the lifespan reindex and by
        callers without an event loop. Most app-time saves should use the
        async variant."""
        try:
            self._index_session(session_id, data)
        except Exception:
            logger.exception(f"[embeddings] sync index failed for {session_id}")

    async def on_session_save_async(self, session_id: str, data: dict):
        """Schedule background indexing. Coalesces with any inflight task
        for the same session so rapid successive saves don't pile up."""
        async with self._inflight_lock:
            existing = self._inflight.get(session_id)
            if existing and not existing.done():
                # A task is already in flight; let it finish — it will pick up
                # whatever the latest session.json on disk says when it runs,
                # so we don't lose this update.
                return existing
            task = asyncio.create_task(self._index_session_async(session_id, data))
            self._inflight[session_id] = task
            return task

    def on_session_delete(self, session_id: str):
        """Soft-delete: we KEEP embeddings so search across history still
        finds them. (When the user permanently deletes from trash, we'll
        purge — that goes through on_session_purge.)"""
        # No-op for soft delete.
        return

    def on_session_purge(self, session_id: str):
        """Hard-delete: purge all embeddings for this session."""
        try:
            self._store.delete_session(session_id)
            logger.info(f"[embeddings] purged session {session_id}")
        except Exception:
            logger.exception(f"[embeddings] purge failed for {session_id}")

    # ---- Indexing ----

    async def _index_session_async(self, session_id: str, data: dict):
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._index_session, session_id, data
            )
        except Exception:
            logger.exception(f"[embeddings] async index failed for {session_id}")
        finally:
            async with self._inflight_lock:
                self._inflight.pop(session_id, None)

    def _index_session(self, session_id: str, data: dict):
        nodes = data.get("nodes") or {}
        session_name = data.get("name") or ""

        # 1. Decide which nodes need re-embedding
        to_embed: list[tuple[str, dict, str]] = []  # (node_id, node, new_hash)
        for nid, node in nodes.items():
            if not _is_indexable(node):
                continue
            new_hash = content_hash(node)
            if self._store.get_hash(nid) == new_hash:
                continue
            to_embed.append((nid, node, new_hash))

        # 2. Drop any indexed-but-no-longer-present node IDs for this session
        indexed_ids = self._store.list_session_node_ids(session_id)
        current_ids = set(nodes.keys())
        for stale_id in indexed_ids - current_ids:
            self._store.delete_node(stale_id)

        if not to_embed:
            return

        # 3. Batch embed
        texts = [_embed_text_for(n[1]) for n in to_embed]
        try:
            vectors = self._backend.embed_batch(texts)
        except Exception:
            logger.exception(f"[embeddings] embed failed for {session_id}")
            return

        # 4. Upsert
        for (nid, node, h), vec in zip(to_embed, vectors):
            self._store.upsert(
                node_id=nid,
                session_id=session_id,
                session_name=session_name,
                prompt_text=node.get("prompt_text") or "",
                response_text=node.get("response_text") or "",
                prompt_mode=node.get("prompt_mode") or "initial",
                created_at=node.get("created_at") or "",
                embedding=vec,
                content_hash=h,
            )
        logger.info(f"[embeddings] indexed {len(to_embed)} nodes for {session_id}")

    # ---- Reindex (first run / manual) ----

    def reindex_all(self) -> dict:
        """Re-embed everything from session files on disk. Returns a stats dict.
        Idempotent thanks to content_hash; cheap to re-run."""
        stats = {"sessions": 0, "nodes_indexed": 0, "nodes_skipped": 0, "errors": 0}

        if not self._sessions_dir.exists():
            return stats

        valid_session_ids = set()
        for folder in sorted(self._sessions_dir.iterdir()):
            session_file = folder / "session.json"
            if not session_file.exists():
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                stats["errors"] += 1
                continue
            sid = data.get("id") or folder.name
            valid_session_ids.add(sid)

            before = self._store.count()
            self._index_session(sid, data)
            after = self._store.count()
            stats["sessions"] += 1
            stats["nodes_indexed"] += max(after - before, 0)
            stats["nodes_skipped"] += sum(
                1 for n in (data.get("nodes") or {}).values() if not _is_indexable(n)
            )

        # Purge sessions that no longer exist on disk
        purged = self._store.delete_orphans(valid_session_ids)
        stats["orphans_purged"] = purged
        return stats

    # ---- Public query API ----

    def search(
        self,
        query: str,
        k: int = 10,
        exclude_session_id: Optional[str] = None,
    ) -> list[dict]:
        if not query.strip():
            return []
        vec = self._backend.embed(query)
        return self._store.search(
            query_embedding=vec, k=k, exclude_session_id=exclude_session_id
        )

    def similar_to(
        self,
        node_id: str,
        k: int = 5,
        exclude_session_id: Optional[str] = None,
    ) -> list[dict]:
        vec = self._store.get_embedding(node_id)
        if vec is None:
            return []
        return self._store.search(
            query_embedding=vec,
            k=k,
            exclude_node_id=node_id,
            exclude_session_id=exclude_session_id,
        )

    # ---- Introspection ----

    def status(self) -> dict:
        return {
            "backend": self._backend.name(),
            "dimension": self._backend.dimension(),
            "indexed_node_count": self._store.count(),
        }
