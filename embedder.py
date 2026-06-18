"""
Embedding backends for the Learning Tool.

Default backend is fastembed (ONNX-based, ~30MB model, downloads from
HuggingFace on first use to ~/.cache/fastembed). Embeddings are 384-dim
vectors from all-MiniLM-L6-v2.

A MockBackend is provided for tests and for environments without network
access on first run — it produces deterministic synthetic vectors based on
a hash of the input. It is NOT useful for real semantic search.

If you want to swap in another backend (Ollama embeddings, OpenAI, etc.),
implement the EmbedderBackend protocol and pass it to EmbeddingService.
"""

import hashlib
import logging
import math
from typing import Protocol


logger = logging.getLogger(__name__)


class EmbedderBackend(Protocol):
    """Anything that can turn strings into float vectors of a fixed dim."""

    def dimension(self) -> int: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    def name(self) -> str: ...


# ---------------- FastEmbed (production default) ----------------

class FastEmbedBackend:
    """fastembed-backed embedder using all-MiniLM-L6-v2 (384-dim).

    Model is lazily loaded on first embed call. First call may take several
    seconds while the model downloads (~30MB) and ONNX Runtime warms up.
    """

    _MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    _DIM = 384

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or self._MODEL_NAME
        self._model = None  # lazy

    def _ensure_loaded(self):
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "fastembed is not installed. Install with: pip install fastembed"
            ) from e
        logger.info(f"Loading embedding model: {self._model_name} (first call may download ~30MB)")
        self._model = TextEmbedding(model_name=self._model_name)
        logger.info("Embedding model ready.")

    def dimension(self) -> int:
        return self._DIM

    def name(self) -> str:
        return f"fastembed:{self._model_name}"

    def embed(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = next(self._model.embed([text]))
        return vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        return [v.tolist() for v in self._model.embed(texts)]


# ---------------- Mock backend (tests / no-network) ----------------

class MockBackend:
    """Deterministic synthetic embedder for tests.

    Produces 384-dim unit-length vectors derived from a SHA-256 of the input.
    Cosine similarity between any two distinct inputs will be ~uniform random
    in [-1, 1], so this is USELESS for actual semantic search — but ideal for
    validating schema, indexing, hashes, hooks, and SQL paths.
    """

    _DIM = 384

    def dimension(self) -> int:
        return self._DIM

    def name(self) -> str:
        return "mock"

    def embed(self, text: str) -> list[float]:
        return self._synthesize(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._synthesize(t) for t in texts]

    def _synthesize(self, text: str) -> list[float]:
        # Generate 384 floats deterministically from the text. Repeated SHA-256
        # extension gives us enough bytes; then normalize to unit length.
        digest = b""
        seed = text.encode("utf-8")
        i = 0
        while len(digest) < self._DIM * 2:  # 2 bytes per float
            digest += hashlib.sha256(seed + i.to_bytes(4, "little")).digest()
            i += 1
        # Map each pair of bytes to a float in [-1, 1)
        vec = []
        for k in range(self._DIM):
            n = int.from_bytes(digest[k * 2 : k * 2 + 2], "little")
            vec.append((n / 32768.0) - 1.0)
        # Normalize
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


# ---------------- Factory ----------------

def make_default_backend() -> EmbedderBackend:
    """Try to make a real backend; if fastembed isn't installed, raise."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "fastembed is not installed. Install with `pip install fastembed sqlite-vec` "
            "to enable semantic search. The rest of Learning Tool works without it."
        )
    return FastEmbedBackend()


def is_available() -> bool:
    """Whether the default embedding stack is importable."""
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False
