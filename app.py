"""
Learning Tool — FastAPI entry point.
Local knowledge exploration tool with node-graph UI.
Serves API + static files on port 8100.
"""

import argparse
import asyncio
import json
import os
import uuid
import time
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response

from models import (
    QueryRequest, QueryResponse, JobStatusResponse,
    SessionCreate, SessionRename, SessionSummary, SessionFull,
    SessionSaveRequest, ProviderCreate, ProviderUpdate, DefaultProviderSet,
)
from llm_bridge import ProviderRegistry
from prompt_engineer import build_prompt, build_lineage_context
from session_manager import SessionManager
from settings_manager import SettingsManager
import exporter
import embedder
import ingestion
import logging

logger = logging.getLogger(__name__)

# ---- Paths ----
BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
SESSIONS_DIR = BASE_DIR / "learning_sessions"
SETTINGS_DIR = BASE_DIR / "settings"

# ---- Parse CLI args (before FastAPI setup) ----
_parser = argparse.ArgumentParser(description="Learning Tool server")
_parser.add_argument("--port", type=int, default=8100, help="Server port (default: 8100)")
_parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                      help="Bind address (default: 127.0.0.1, set to 0.0.0.0 for Docker)")
_parser.add_argument("--llm-url",
                      default=os.environ.get("LLM_URL", "http://localhost:11434/v1/chat/completions"),
                      help="LLM API endpoint (or set LLM_URL env var)")
_parser.add_argument("--llm-model", default="",
                      help="Model name (optional, server uses loaded model)")
_cli_args, _ = _parser.parse_known_args()

# ---- Shared state ----
settings_mgr = SettingsManager(SETTINGS_DIR, cli_url=_cli_args.llm_url, cli_model=_cli_args.llm_model)
provider_registry = ProviderRegistry(settings_mgr)
session_mgr = SessionManager(SESSIONS_DIR)

# MCP (Model Context Protocol) server configurations. Foundation only:
# this stores configs and tests connections. Wiring MCP tools into the LLM
# call loop is a follow-up.
from mcp_manager import MCPManager
mcp_mgr = MCPManager(SETTINGS_DIR)

# Optional embeddings service. If fastembed+sqlite-vec aren't installed,
# search/similar endpoints will 503 but everything else still works.
embedding_service = None
if embedder.is_available():
    try:
        from embedding_service import EmbeddingService
        embedding_service = EmbeddingService(
            sessions_dir=SESSIONS_DIR,
            db_path=SESSIONS_DIR.parent / "embeddings.db",
            backend=embedder.make_default_backend(),
        )
        # Wire SessionManager hooks. The on_save hook fires from a sync code
        # path so we schedule the async indexer ourselves via a thread-safe
        # call into the running event loop.
        def _on_save(session_id: str, data: dict):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        embedding_service.on_session_save_async(session_id, data), loop
                    )
                else:
                    embedding_service.on_session_save_sync(session_id, data)
            except RuntimeError:
                # No event loop (rare); fall back to sync
                embedding_service.on_session_save_sync(session_id, data)
        session_mgr.on_save = _on_save
        session_mgr.on_purge = embedding_service.on_session_purge
        logger.info("Embeddings enabled.")
    except Exception:
        logger.exception("Failed to initialize embedding service; continuing without semantic search.")
        embedding_service = None
else:
    logger.info("Embeddings disabled (install fastembed + sqlite-vec to enable).")

# Track running jobs: job_id -> dict
jobs: dict[str, dict] = {}


def _get_provider(provider_id: str | None = None):
    """Get provider by ID or return default."""
    if provider_id:
        try:
            return provider_registry.get(provider_id)
        except ValueError:
            pass
    return provider_registry.get_default()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # Clean up expired trash on startup
    purged = session_mgr.cleanup_trash()
    if purged:
        print(f"Trash cleanup: permanently removed {purged} expired session(s)")
    # One-shot reindex if embeddings are enabled and the DB looks empty.
    if embedding_service is not None:
        try:
            indexed = embedding_service.status()["indexed_node_count"]
            if indexed == 0:
                # Run reindex in a background thread so startup isn't blocked
                # by the first model download.
                def _bg_reindex():
                    try:
                        stats = embedding_service.reindex_all()
                        logger.info(f"[embeddings] initial reindex: {stats}")
                    except Exception:
                        logger.exception("[embeddings] initial reindex failed")
                asyncio.get_event_loop().run_in_executor(None, _bg_reindex)
            else:
                logger.info(f"[embeddings] {indexed} nodes already indexed.")
        except Exception:
            logger.exception("[embeddings] startup check failed")
    yield


app = FastAPI(title="Learning Tool", lifespan=lifespan)


# ---- API: Query endpoints ----

@app.post("/api/query", response_model=QueryResponse)
async def submit_query(req: QueryRequest):
    """Submit a prompt (initial or follow-up). Returns job_id immediately."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    # Load session to build lineage context if needed
    session_data = None
    if req.session_id:
        session_data = session_mgr.load(req.session_id)

    # Build the engineered prompt
    engineered = build_prompt(
        mode=req.mode,
        prompt_text=req.prompt_text,
        highlighted_text=req.highlighted_text,
        user_question=req.user_question,
        session_data=session_data,
        parent_node_id=req.parent_node_id,
    )

    # Record job
    jobs[job_id] = {
        "status": "queued",
        "engineered_prompt": engineered,
        "start_time": time.time(),
        "result": None,
        "error": None,
        "original_request": req.dict(),
        "provider_id": req.provider_id,
    }

    # Fire and forget — the queue serializes execution
    asyncio.ensure_future(_run_job(job_id, engineered, req.provider_id))

    return QueryResponse(
        job_id=job_id,
        status="queued",
        engineered_prompt=engineered,
    )


async def _run_job(job_id: str, prompt: str, provider_id: str | None = None):
    """Background task: run LLM query and store result."""
    job = jobs[job_id]
    provider = _get_provider(provider_id)
    try:
        job["status"] = "running"
        result = await provider.submit(prompt)
        job["status"] = "complete"
        job["result"] = result
    except Exception as e:
        # Try fallback
        fallback = provider_registry.get_fallback()
        if fallback and fallback.provider_id != provider.provider_id:
            try:
                result = await fallback.submit(prompt)
                job["status"] = "complete"
                job["result"] = result
                return
            except Exception:
                pass
        job["status"] = "error"
        job["error"] = str(e)


@app.get("/api/query/{job_id}/status", response_model=JobStatusResponse)
async def query_status(job_id: str):
    """Poll job status."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    elapsed = time.time() - job["start_time"]
    resp = JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        elapsed_seconds=round(elapsed, 1),
    )
    if job["status"] == "complete" and job["result"]:
        resp.response_html = job["result"].get("html", "")
        resp.response_text = job["result"].get("text", "")
    if job["status"] == "error":
        resp.error_message = job["error"]
    return resp


@app.post("/api/query/{job_id}/retry", response_model=QueryResponse)
async def retry_query(job_id: str):
    """Re-run the same prompt. Returns a new job_id."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    old = jobs[job_id]
    prompt = old["engineered_prompt"]
    provider_id = old.get("provider_id")
    new_job_id = f"job_{uuid.uuid4().hex[:12]}"
    jobs[new_job_id] = {
        "status": "queued",
        "engineered_prompt": prompt,
        "start_time": time.time(),
        "result": None,
        "error": None,
        "original_request": old["original_request"],
        "provider_id": provider_id,
    }
    asyncio.ensure_future(_run_job(new_job_id, prompt, provider_id))
    return QueryResponse(job_id=new_job_id, status="queued", engineered_prompt=prompt)


# ---- API: Streaming query endpoint ----

@app.post("/api/query/stream")
async def stream_query(req: QueryRequest):
    """Stream LLM response via Server-Sent Events.

    Events: prompt, thinking, token, done, error, fallback
    """
    session_data = None
    if req.session_id:
        session_data = session_mgr.load(req.session_id)

    engineered = build_prompt(
        mode=req.mode,
        prompt_text=req.prompt_text,
        highlighted_text=req.highlighted_text,
        user_question=req.user_question,
        session_data=session_data,
        parent_node_id=req.parent_node_id,
    )

    provider = _get_provider(req.provider_id)

    async def event_stream():
        # Send engineered prompt first so frontend can store it
        yield f"event: prompt\ndata: {json.dumps({'engineered_prompt': engineered})}\n\n"

        try:
            async for event_type, data in provider.stream(engineered):
                if event_type == "thinking":
                    yield f"event: thinking\ndata: {{}}\n\n"
                elif event_type == "token":
                    yield f"event: token\ndata: {json.dumps({'text': data})}\n\n"
                elif event_type == "done":
                    yield f"event: done\ndata: {json.dumps({'text': data})}\n\n"
                elif event_type == "error":
                    # Try fallback on error
                    fallback = provider_registry.get_fallback()
                    if fallback and fallback.provider_id != provider.provider_id:
                        yield f"event: fallback\ndata: {json.dumps({'from': provider.provider_id, 'to': fallback.provider_id})}\n\n"
                        async for fb_type, fb_data in fallback.stream(engineered):
                            if fb_type == "thinking":
                                yield f"event: thinking\ndata: {{}}\n\n"
                            elif fb_type == "token":
                                yield f"event: token\ndata: {json.dumps({'text': fb_data})}\n\n"
                            elif fb_type == "done":
                                yield f"event: done\ndata: {json.dumps({'text': fb_data})}\n\n"
                            elif fb_type == "error":
                                yield f"event: error\ndata: {json.dumps({'error': fb_data})}\n\n"
                        return
                    yield f"event: error\ndata: {json.dumps({'error': data})}\n\n"
        except Exception as e:
            # Try fallback on exception
            fallback = provider_registry.get_fallback()
            if fallback and fallback.provider_id != provider.provider_id:
                yield f"event: fallback\ndata: {json.dumps({'from': provider.provider_id, 'to': fallback.provider_id})}\n\n"
                try:
                    async for fb_type, fb_data in fallback.stream(engineered):
                        if fb_type == "thinking":
                            yield f"event: thinking\ndata: {{}}\n\n"
                        elif fb_type == "token":
                            yield f"event: token\ndata: {json.dumps({'text': fb_data})}\n\n"
                        elif fb_type == "done":
                            yield f"event: done\ndata: {json.dumps({'text': fb_data})}\n\n"
                        elif fb_type == "error":
                            yield f"event: error\ndata: {json.dumps({'error': fb_data})}\n\n"
                except Exception as fb_e:
                    yield f"event: error\ndata: {json.dumps({'error': str(fb_e)})}\n\n"
                return
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- API: Title generation (uses default provider) ----

@app.post("/api/generate-title")
async def generate_title(req: QueryRequest):
    """Generate a short session title from the first prompt via LLM."""
    try:
        provider = provider_registry.get_default()
        result = await provider.submit(
            f'Generate a short title (max 6 words, no quotes, no punctuation at the end) '
            f'for a research session about this question:\n"{req.prompt_text}"\n'
            f'Reply with ONLY the title, nothing else.',
            timeout=30,
            thinking=False,  # Skip reasoning for utility requests (saves 10-20s)
        )
        title = result.get("text", "").strip().strip('"\'').strip()
        # Take first line only, limit length
        title = title.split('\n')[0][:60]
        if not title:
            title = "Untitled Session"
        return {"title": title}
    except Exception as e:
        return {"title": "Untitled Session", "error": str(e)}


# ---- API: Settings endpoints ----

@app.get("/api/settings/providers")
async def list_providers():
    """List all providers with masked API keys."""
    providers = settings_mgr.get_all_providers()
    return {
        "providers": providers,
        "default_provider_id": settings_mgr.get_default_id(),
        "fallback_provider_id": settings_mgr.get_fallback_id(),
    }


@app.get("/api/settings/provider-list")
async def provider_list():
    """Lightweight provider list for dropdown."""
    return {
        "providers": settings_mgr.get_provider_list(),
        "default_provider_id": settings_mgr.get_default_id(),
    }


@app.post("/api/settings/providers")
async def add_provider(req: ProviderCreate):
    """Add a new LLM provider."""
    provider = settings_mgr.add_provider(req.dict())
    provider_registry.refresh()
    return provider


@app.put("/api/settings/providers/{provider_id}")
async def update_provider(provider_id: str, req: ProviderUpdate):
    """Update an existing provider."""
    updates = {k: v for k, v in req.dict().items() if v is not None}
    result = settings_mgr.update_provider(provider_id, updates)
    if not result:
        raise HTTPException(404, "Provider not found")
    provider_registry.refresh()
    return result


@app.delete("/api/settings/providers/{provider_id}")
async def delete_provider(provider_id: str):
    """Delete a provider."""
    ok = settings_mgr.delete_provider(provider_id)
    if not ok:
        raise HTTPException(400, "Cannot delete provider (not found or last remaining)")
    provider_registry.refresh()
    return {"status": "deleted"}


@app.post("/api/settings/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    """Test connectivity to a provider."""
    raw = settings_mgr.get_provider_raw(provider_id)
    if not raw:
        raise HTTPException(404, "Provider not found")

    # Create a temporary provider instance for testing
    from llm_bridge import ProviderRegistry as PR
    temp_provider = PR._create(raw)
    result = await temp_provider.test()
    return result


@app.put("/api/settings/default-provider")
async def set_default_provider(req: DefaultProviderSet):
    """Set the default provider."""
    if not settings_mgr.set_default(req.provider_id):
        raise HTTPException(400, "Provider not found")
    return {"status": "ok", "default_provider_id": req.provider_id}


@app.put("/api/settings/fallback-provider")
async def set_fallback_provider(req: DefaultProviderSet):
    """Set the fallback provider (or None to clear)."""
    if not settings_mgr.set_fallback(req.provider_id):
        raise HTTPException(400, "Provider not found")
    return {"status": "ok", "fallback_provider_id": req.provider_id}


# ---- API: Ollama ----

@app.get("/api/ollama/models")
async def list_ollama_models(url: str = "http://localhost:11434"):
    """List models available on an Ollama instance."""
    import urllib.request
    import urllib.error
    tags_url = url.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(tags_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = []
            for m in data.get("models", []):
                models.append({
                    "name": m["name"],
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                })
            return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ---- API: Session endpoints ----

@app.get("/api/sessions", response_model=list[SessionSummary])
async def list_sessions():
    return session_mgr.list_all()


@app.post("/api/sessions", response_model=SessionSummary)
async def create_session(req: SessionCreate):
    return session_mgr.create(req.name)


@app.get("/api/sessions/{session_id}", response_model=SessionFull)
async def get_session(session_id: str):
    data = session_mgr.load(session_id)
    if not data:
        raise HTTPException(404, "Session not found")
    return data


@app.api_route("/api/sessions/{session_id}", methods=["PUT", "POST"])
async def save_session(session_id: str, req: SessionSaveRequest):
    session_mgr.save(session_id, req.dict())
    return {"status": "saved"}


@app.put("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: SessionRename):
    session_mgr.rename(session_id, req.name)
    return {"status": "renamed"}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    session_mgr.delete(session_id)
    return {"status": "deleted"}


# ---- API: Export ----

@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "obsidian"):
    """Download a session as Markdown.

    format=obsidian → zip of linked .md files (Obsidian-flavoured wikilinks)
    format=single   → one .md document with depth-based heading nesting
    """
    data = session_mgr.load(session_id)
    if not data:
        raise HTTPException(404, "Session not found")

    name_slug = exporter.slugify(data.get("name", "")) or "session"

    if format == "obsidian":
        content = exporter.export_obsidian(data)
        filename = f"{name_slug}.zip"
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    if format == "single":
        content = exporter.export_single_markdown(data)
        filename = f"{name_slug}.md"
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    raise HTTPException(400, f"Unknown format: {format!r}. Use 'obsidian' or 'single'.")


# ---- API: Ingestion (PDF / URL / YouTube) ----

def _ingestion_to_session(result: ingestion.IngestionResult) -> dict:
    """Create a session whose root node IS the ingested document."""
    info = session_mgr.create(name=result.title[:80] or "Document")
    sid = info["id"]
    data = session_mgr.load(sid)

    node_id = f"node_{uuid.uuid4().hex[:12]}"
    data["nodes"][node_id] = {
        "id": node_id,
        "parent_id": None,
        "highlight_id": None,
        "x": 80,
        "y": 80,
        "width": 520,
        "height": None,
        "prompt_text": f"[Imported {result.source_type}: {result.title}]",
        "prompt_mode": "document",
        "response_html": "",            # frontend will render from response_text
        "response_text": result.text,
        "highlighted_text": None,
        "status": "complete",
        "created_at": data["created_at"],
        "source_type": result.source_type,
        "source_meta": result.source_meta,
    }
    session_mgr.save(sid, data)
    return {"session_id": sid, "node_id": node_id, "title": result.title,
            "source_type": result.source_type, "source_meta": result.source_meta}


@app.post("/api/sessions/ingest/file")
async def ingest_file(file: UploadFile = File(...)):
    """Ingest a PDF upload. Returns the new session's ID and root node."""
    try:
        contents = await file.read()
        if file.filename and file.filename.lower().endswith(".pdf"):
            result = ingestion.extract_pdf(contents, filename=file.filename)
        else:
            raise HTTPException(400, "Only PDF files are supported.")
    except ingestion.IngestionError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Ingestion failed")
        raise HTTPException(500, f"Ingestion failed: {e}")
    return _ingestion_to_session(result)


@app.post("/api/sessions/ingest/url")
async def ingest_url(payload: dict):
    """Ingest a URL (web article) or YouTube video."""
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(400, "Missing 'url' in body.")
    try:
        result = ingestion.extract_url(url)
    except ingestion.IngestionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("URL ingestion failed")
        raise HTTPException(500, f"Ingestion failed: {e}")
    return _ingestion_to_session(result)


# ---- API: Summarize subtree ----

def _walk_subtree(nodes: dict, root_id: str) -> list[str]:
    """Return root_id followed by all descendant IDs in BFS order."""
    children_of: dict[str, list[str]] = {}
    for nid, n in nodes.items():
        pid = n.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(nid)
    # Stable order: by created_at
    for pid, kids in children_of.items():
        kids.sort(key=lambda cid: nodes[cid].get("created_at", ""))

    out = []
    stack = [root_id]
    while stack:
        nid = stack.pop(0)
        if nid in out or nid not in nodes:
            continue
        out.append(nid)
        stack.extend(children_of.get(nid, []))
    return out


def _build_summary_prompt(nodes: dict, ids: list[str]) -> str:
    """Build a prompt asking the LLM to summarize a subtree."""
    parts = ["You are summarizing a branch of exploratory learning notes."]
    parts.append("Below is a tree of related questions and AI responses, in order of exploration.")
    parts.append("Produce a single coherent summary that captures the key concepts, findings, and "
                 "open questions. Use markdown. Aim for ~400 words. Do NOT introduce new information "
                 "or speculation — stick to what's in the source notes.")
    parts.append("")
    parts.append("---")
    parts.append("")
    for nid in ids:
        n = nodes.get(nid, {})
        mode = n.get("prompt_mode", "initial")
        prompt_text = (n.get("prompt_text") or "").strip()
        response_text = (n.get("response_text") or "").strip()
        hl = (n.get("highlighted_text") or "").strip()
        header_bits = [f"[{mode}]"]
        if hl:
            header_bits.append(f'highlight="{hl[:80]}"')
        parts.append(f"### Node {' '.join(header_bits)}")
        if prompt_text:
            parts.append(f"**Question:** {prompt_text}")
        if response_text:
            parts.append(response_text[:3000])
        parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("Now write the summary:")
    return "\n".join(parts)


@app.post("/api/nodes/{session_id}/{node_id}/summarize")
async def summarize_subtree(session_id: str, node_id: str, payload: dict | None = None):
    """Generate a summary node covering this node and all its descendants."""
    data = session_mgr.load(session_id)
    if not data:
        raise HTTPException(404, "Session not found")
    nodes = data.get("nodes", {})
    if node_id not in nodes:
        raise HTTPException(404, "Node not found")

    subtree_ids = _walk_subtree(nodes, node_id)
    if len(subtree_ids) < 2:
        raise HTTPException(400, "Nothing to summarize — node has no descendants.")

    provider_id = (payload or {}).get("provider_id")
    provider = _get_provider(provider_id)
    prompt = _build_summary_prompt(nodes, subtree_ids)

    try:
        result = await provider.submit(prompt)
        response_text = (result or {}).get("content", "")
    except Exception as e:
        logger.exception("Summarization failed")
        raise HTTPException(500, f"LLM call failed: {e}")

    if not response_text or not response_text.strip():
        raise HTTPException(500, "LLM returned empty response.")

    # Place the summary node visually offset from the root of the subtree
    root_node = nodes[node_id]
    new_id = f"node_{uuid.uuid4().hex[:12]}"
    summary_node = {
        "id": new_id,
        "parent_id": None,           # standalone — not part of the original tree
        "highlight_id": None,
        "x": (root_node.get("x", 0) or 0) + 600,
        "y": root_node.get("y", 0) or 0,
        "width": 500,
        "height": None,
        "prompt_text": f"Summary of {len(subtree_ids)} nodes",
        "prompt_mode": "summary",
        "response_html": "",
        "response_text": response_text,
        "highlighted_text": None,
        "status": "complete",
        "created_at": datetime.now().isoformat(),
        "summarized_nodes": subtree_ids,
    }
    data["nodes"][new_id] = summary_node
    session_mgr.save(session_id, data)
    return {"node_id": new_id, "summarized_count": len(subtree_ids), "node": summary_node}


# ---- API: Semantic search ----

def _require_embeddings():
    if embedding_service is None:
        raise HTTPException(
            503,
            "Semantic search is not available. Install fastembed and sqlite-vec, "
            "then restart: pip install fastembed sqlite-vec",
        )
    return embedding_service


@app.get("/api/embeddings/status")
async def embeddings_status():
    """Whether semantic search is available, and how many nodes are indexed."""
    if embedding_service is None:
        return {"available": False, "reason": "fastembed or sqlite-vec not installed"}
    return {"available": True, **embedding_service.status()}


@app.get("/api/search")
async def semantic_search(q: str, k: int = 10, exclude_session_id: str | None = None):
    """Semantic search across all indexed nodes.

    Returns ordered results (closest first) with node + session metadata.
    Each result includes a `snippet` derived from the response text.
    """
    svc = _require_embeddings()
    k = max(1, min(k, 50))
    try:
        results = svc.search(q, k=k, exclude_session_id=exclude_session_id)
    except Exception as e:
        logger.exception("[embeddings] search failed")
        raise HTTPException(500, f"Search failed: {e}")
    # Add a short snippet for the UI
    for r in results:
        text = (r.get("response_text") or "").strip()
        r["snippet"] = (text[:240] + "…") if len(text) > 240 else text
    return {"query": q, "k": k, "results": results}


@app.get("/api/nodes/{node_id}/similar")
async def similar_nodes(node_id: str, k: int = 5, exclude_session_id: str | None = None):
    """Find nodes semantically similar to a given node."""
    svc = _require_embeddings()
    k = max(1, min(k, 25))
    results = svc.similar_to(node_id, k=k, exclude_session_id=exclude_session_id)
    for r in results:
        text = (r.get("response_text") or "").strip()
        r["snippet"] = (text[:240] + "…") if len(text) > 240 else text
    return {"node_id": node_id, "k": k, "results": results}


@app.post("/api/embeddings/reindex")
async def reindex_embeddings():
    """Re-embed every session on disk. Idempotent thanks to content hashing —
    only nodes whose (prompt+response+mode) changed will actually re-embed."""
    svc = _require_embeddings()
    try:
        stats = svc.reindex_all()
    except Exception as e:
        logger.exception("[embeddings] reindex failed")
        raise HTTPException(500, f"Reindex failed: {e}")
    return {"status": "ok", "stats": stats}


# ---- API: MCP (Model Context Protocol) server config ----

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    """List configured MCP servers (auth headers are returned but the UI
    should never persist sensitive ones in plaintext on disk in real use)."""
    return {"servers": mcp_mgr.list_servers()}


@app.post("/api/mcp/servers")
async def add_mcp_server(payload: dict):
    """Add a new MCP server. Required fields: name, transport, url|command."""
    try:
        cfg = mcp_mgr.add(
            name=payload.get("name", ""),
            transport=payload.get("transport", "http"),
            url=payload.get("url", ""),
            command=payload.get("command", ""),
            args=payload.get("args", []),
            headers=payload.get("headers", {}),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return cfg


@app.put("/api/mcp/servers/{server_id}")
async def update_mcp_server(server_id: str, payload: dict):
    try:
        return mcp_mgr.update(server_id, payload)
    except KeyError:
        raise HTTPException(404, "Server not found")


@app.delete("/api/mcp/servers/{server_id}")
async def remove_mcp_server(server_id: str):
    mcp_mgr.remove(server_id)
    return {"status": "ok"}


@app.post("/api/mcp/servers/{server_id}/test")
async def test_mcp_server(server_id: str):
    """Try to connect and list tools. Useful for validating config from the UI."""
    cfg = mcp_mgr.get(server_id)
    if not cfg:
        raise HTTPException(404, "Server not found")
    from mcp_manager import test_connection
    result = await test_connection(cfg)
    return result


# ---- API: Trash endpoints ----

@app.get("/api/trash")
async def list_trash():
    return session_mgr.list_trash()


@app.post("/api/trash/{session_id}/restore")
async def restore_session(session_id: str):
    ok = session_mgr.restore(session_id)
    if not ok:
        raise HTTPException(404, "Session not found in trash or restore conflict")
    return {"status": "restored"}


@app.delete("/api/trash/{session_id}")
async def permanent_delete_session(session_id: str):
    session_mgr.permanent_delete(session_id)
    return {"status": "permanently_deleted"}


# ---- Static files (must be last) ----

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    port = _cli_args.port
    print(f"Learning Tool starting at http://localhost:{port}")
    default_prov = settings_mgr.get_provider(settings_mgr.get_default_id())
    if default_prov:
        print(f"Default provider: {default_prov['alias']} ({default_prov['type']})")
    uvicorn.run(app, host=_cli_args.host, port=port, log_level="info")
