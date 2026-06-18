"""
Content ingestion: turn a PDF, URL, or YouTube link into text that can become
the root node of a new learning session.

All three sources are optional dependencies (see requirements-ingestion.txt).
If a dep is missing, the corresponding extractor raises a clear error.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs


logger = logging.getLogger(__name__)


# ---- Result type ----

@dataclass
class IngestionResult:
    """The extracted content + metadata describing where it came from."""
    text: str
    title: str
    source_type: str           # "pdf" | "url" | "youtube"
    source_meta: dict          # filename, url, page count, etc.


class IngestionError(Exception):
    """Raised when extraction fails for any reason. Message is user-facing."""


# ---- Limits ----

MAX_PDF_BYTES = 25 * 1024 * 1024        # 25 MB
MAX_TEXT_CHARS = 200_000                # ~50k tokens — cap for any single doc
MAX_URL_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


# ---- PDF ----

def extract_pdf(file_bytes: bytes, filename: str = "document.pdf") -> IngestionResult:
    """Extract text from a PDF using pypdf."""
    if len(file_bytes) > MAX_PDF_BYTES:
        raise IngestionError(
            f"PDF too large ({len(file_bytes) // (1024*1024)} MB). "
            f"Limit is {MAX_PDF_BYTES // (1024*1024)} MB."
        )
    try:
        from pypdf import PdfReader
    except ImportError:
        raise IngestionError(
            "PDF support not installed. Run: pip install -r requirements-ingestion.txt"
        )

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as e:
        raise IngestionError(f"Could not read PDF: {e}")

    if reader.is_encrypted:
        # Try empty-password unlock first
        try:
            if reader.decrypt("") == 0:
                raise IngestionError("PDF is password-protected.")
        except Exception:
            raise IngestionError("PDF is password-protected.")

    pages = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:
            logger.warning(f"PDF page {i+1} extraction failed: {e}")
            t = ""
        if t.strip():
            pages.append(t.strip())

    if not pages:
        raise IngestionError(
            "Could not extract any text from this PDF. "
            "It may be a scanned image — OCR isn't supported yet."
        )

    text = "\n\n".join(f"## Page {i+1}\n\n{p}" for i, p in enumerate(pages))
    text = _truncate(text)

    # Try to get a title
    title = ""
    try:
        meta = reader.metadata
        if meta and meta.title:
            title = str(meta.title).strip()
    except Exception:
        pass
    if not title:
        title = filename.rsplit("/", 1)[-1]
        if title.lower().endswith(".pdf"):
            title = title[:-4]

    return IngestionResult(
        text=text,
        title=title or "PDF document",
        source_type="pdf",
        source_meta={
            "filename": filename,
            "page_count": len(reader.pages),
            "char_count": len(text),
            "extracted_pages": len(pages),
        },
    )


# ---- URL (web article) ----

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def is_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return host.lower() in _YOUTUBE_HOSTS


def extract_url(url: str) -> IngestionResult:
    """Dispatch URL → YouTube transcript or article text."""
    if not url.startswith(("http://", "https://")):
        raise IngestionError("URL must start with http:// or https://")
    if is_youtube_url(url):
        return extract_youtube(url)
    return _extract_article(url)


def _extract_article(url: str) -> IngestionResult:
    """Fetch an HTML page and extract the main article content."""
    try:
        import httpx
    except ImportError:
        raise IngestionError("httpx not installed (this should never happen — it ships with FastAPI).")
    try:
        import trafilatura
    except ImportError:
        raise IngestionError(
            "URL extraction not installed. Run: pip install -r requirements-ingestion.txt"
        )

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (LearningTool ingest)"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            # Cap response size
            content = resp.content[:MAX_URL_RESPONSE_BYTES]
    except httpx.HTTPStatusError as e:
        raise IngestionError(f"HTTP {e.response.status_code} fetching URL")
    except httpx.TimeoutException:
        raise IngestionError("Timed out fetching URL (20s limit).")
    except Exception as e:
        raise IngestionError(f"Could not fetch URL: {e}")

    # Trafilatura wants a string; decode best-effort
    try:
        html = content.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        html = content.decode("utf-8", errors="replace")

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not extracted or not extracted.strip():
        raise IngestionError(
            "Could not extract readable content from this page. "
            "It may be JS-rendered, paywalled, or have an unusual layout."
        )

    # Get title separately
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception:
        pass
    if not title:
        # Fall back to <title> tag
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = (m.group(1).strip() if m else url)

    text = _truncate(extracted.strip())

    return IngestionResult(
        text=text,
        title=title,
        source_type="url",
        source_meta={
            "url": url,
            "final_url": str(resp.url),
            "char_count": len(text),
            "content_type": resp.headers.get("content-type", ""),
        },
    )


# ---- YouTube ----

def _youtube_video_id(url: str) -> Optional[str]:
    """Extract the 11-char video ID from any common YouTube URL form."""
    try:
        u = urlparse(url)
    except Exception:
        return None
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        vid = u.path.lstrip("/")
        return vid[:11] if len(vid) >= 11 else None
    if host.endswith("youtube.com"):
        if u.path == "/watch":
            qs = parse_qs(u.query)
            v = qs.get("v", [None])[0]
            return v[:11] if v and len(v) >= 11 else None
        if u.path.startswith("/embed/") or u.path.startswith("/v/"):
            vid = u.path.split("/", 2)[2].split("/", 1)[0]
            return vid[:11] if len(vid) >= 11 else None
        if u.path.startswith("/shorts/"):
            vid = u.path.split("/", 2)[2].split("/", 1)[0]
            return vid[:11] if len(vid) >= 11 else None
    return None


def extract_youtube(url: str) -> IngestionResult:
    """Fetch YouTube transcript via youtube-transcript-api."""
    video_id = _youtube_video_id(url)
    if not video_id:
        raise IngestionError("Could not parse a YouTube video ID from that URL.")

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )
    except ImportError:
        raise IngestionError(
            "YouTube support not installed. Run: pip install -r requirements-ingestion.txt"
        )

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        entries = list(fetched)  # iterable of FetchedTranscriptSnippet
    except TranscriptsDisabled:
        raise IngestionError("Transcripts are disabled for this video.")
    except NoTranscriptFound:
        raise IngestionError("No transcript available in any language.")
    except VideoUnavailable:
        raise IngestionError("Video is unavailable.")
    except Exception as e:
        # Older versions of the lib used a different API; fall back to get_transcript
        try:
            entries = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception:
            raise IngestionError(f"Could not fetch transcript: {e}")

    if not entries:
        raise IngestionError("Transcript is empty.")

    # Format: [HH:MM:SS] text
    lines = []
    for e in entries:
        # Support both FetchedTranscriptSnippet (.text, .start) and plain dicts
        start = getattr(e, "start", None)
        text = getattr(e, "text", None)
        if start is None and isinstance(e, dict):
            start = e.get("start", 0)
            text = e.get("text", "")
        if text is None:
            continue
        ts = _fmt_timestamp(start or 0)
        lines.append(f"[{ts}] {text.strip()}")

    text = _truncate("\n".join(lines))
    return IngestionResult(
        text=text,
        title=f"YouTube video {video_id}",
        source_type="youtube",
        source_meta={
            "url": url,
            "video_id": video_id,
            "segment_count": len(entries),
            "char_count": len(text),
        },
    )


def _fmt_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---- Helpers ----

def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    truncated = text[:MAX_TEXT_CHARS]
    return truncated + f"\n\n_[truncated at {MAX_TEXT_CHARS:,} characters]_"
