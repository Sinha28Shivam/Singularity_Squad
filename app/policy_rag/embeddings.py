"""Embedding providers for policy chunks.

``Embedder.embed(texts) -> list[list[float]] | None``: ``None`` means the provider is unavailable (never
raises). ``OpenAIEmbedder`` is used when the LLM is enabled; ``LocalHashEmbedder`` is a deterministic,
dependency-light hashed bag-of-words fallback used offline (and in tests).

Vectors are only comparable when produced by the same model, so retrieval picks the embedder that
matches the ``embedding_model`` stored on each chunk (``embedder_for_model``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional, Protocol

import numpy as np

from app import llm

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "policy_rag_config.json"
LOCAL_MODEL = "local-hash-512"
LOCAL_DIMS = 512
_TOKEN = re.compile(r"[a-z0-9]+")
_MAX_BATCH = 64


@lru_cache(maxsize=1)
def _config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def load_config() -> dict:
    """Policy RAG config (fresh copy on each call)."""
    return json.loads(_config_text())


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]: ...


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class LocalHashEmbedder:
    """Hashed unigrams + bigrams, sublinear TF, L2-normalised. Deterministic across processes."""

    model = LOCAL_MODEL

    def __init__(self, dims: int = LOCAL_DIMS) -> None:
        self.dims = dims

    def _bucket(self, token: str) -> tuple[int, float]:
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(h, "little")
        return value % self.dims, (1.0 if (value >> 63) & 1 == 0 else -1.0)

    def _vector(self, text: str) -> list[float]:
        toks = tokenize(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        vec = np.zeros(self.dims, dtype=np.float64)
        counts: dict[str, int] = {}
        for f in feats:
            counts[f] = counts.get(f, 0) + 1
        for f, c in counts.items():
            idx, sign = self._bucket(f)
            vec[idx] += sign * (1.0 + math.log(c))
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return [float(round(x, 6)) for x in vec]

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        try:
            return [self._vector(t) for t in texts]
        except Exception as exc:  # noqa: BLE001 - never raise from an embedder
            logger.warning("Local embedding failed (%s)", type(exc).__name__)
            return None


class OpenAIEmbedder:
    """OpenAI-compatible embeddings via the shared client. Returns None when offline or on any failure."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or str(load_config().get("embedding_model") or "text-embedding-3-small")

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        if not llm.llm_enabled():
            return None
        try:
            out: list[list[float]] = []
            for i in range(0, len(texts), _MAX_BATCH):
                batch = [t if t.strip() else " " for t in texts[i:i + _MAX_BATCH]]
                resp = llm._client().embeddings.create(model=self.model, input=batch)
                data = sorted(resp.data, key=lambda d: d.index)
                out.extend([list(map(float, d.embedding)) for d in data])
            if len(out) != len(texts):
                return None
            return out
        except Exception as exc:  # noqa: BLE001 - network/auth errors -> unavailable
            logger.warning("Embedding service unavailable (%s)", type(exc).__name__)
            return None


def get_embedder() -> Embedder:
    """Preferred embedder for indexing: the remote model when the LLM is enabled, else local hashing."""
    if llm.llm_enabled():
        return OpenAIEmbedder()
    return LocalHashEmbedder()


def embedder_for_model(model: Optional[str]) -> Optional[Embedder]:
    """Embedder able to produce query vectors comparable with chunks embedded by ``model``."""
    if not model:
        return None
    if model == LOCAL_MODEL:
        return LocalHashEmbedder()
    if llm.llm_enabled():
        return OpenAIEmbedder(model)
    return None


def embed_with_fallback(texts: list[str]) -> tuple[Optional[list[list[float]]], Optional[str]]:
    """Embed for indexing: try the preferred embedder, fall back to local hashing. Returns (vectors, model)."""
    primary = get_embedder()
    vectors = primary.embed(texts)
    if vectors is not None:
        return vectors, primary.model
    if primary.model != LOCAL_MODEL:
        local = LocalHashEmbedder()
        vectors = local.embed(texts)
        if vectors is not None:
            return vectors, local.model
    return None, None


def provider_for_model(model: Optional[str]) -> str:
    """"local_hash" for the hashed fallback (not semantic), "openai" for remote models, "none" otherwise."""
    if not model:
        return "none"
    return "local_hash" if model == LOCAL_MODEL else "openai"


def preferred_provider() -> tuple[str, str]:
    """(provider, model) the indexer would use right now."""
    model = get_embedder().model
    return provider_for_model(model), model


def retrieval_config_version(extra: Optional[dict] = None) -> str:
    """Stable id of the retrieval configuration (weights, thresholds, embedding settings)."""
    cfg = load_config()
    keys = ("min_relevance", "retrieval_top_k", "embedding_model", "chunk_target_tokens", "chunk_overlap_tokens")
    data = {k: cfg.get(k) for k in keys}
    data.update(extra or {})
    payload = json.dumps(data, sort_keys=True, default=str)
    return "rcfg-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))
