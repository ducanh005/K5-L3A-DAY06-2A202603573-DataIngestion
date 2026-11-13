"""Gemini embedding adapter implementing the shared Embedder contract."""
from __future__ import annotations

import math
import os
import re
import time
from typing import Any, Protocol

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

DEFAULT_GEMINI_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_DIMENSIONS = 768
MAX_BATCH_SIZE = 100
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)


class Embedder(Protocol):
    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...

    def embed_query(self, question: str) -> list[float]:
        """Return a query vector compatible with the stored document vectors."""
        ...


def _normalize_model_name(model_name: str) -> str:
    clean = model_name.strip()
    if clean.startswith("models/"):
        clean = clean.split("/", 1)[1]
    return clean


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        raise RuntimeError("Received zero or non-finite embedding vector from provider.")
    return [v / norm for v in vector]


def _extract_retry_seconds(exc: Exception) -> float | None:
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        return float(match.group(1))
    return None


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code in _RETRYABLE_STATUS_CODES:
            return True
    msg = str(exc).upper()
    return any(tok in msg for tok in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "TIMEOUT"))


class MyEmbedder:
    """Gemini Developer API embedding adapter using `google-genai`."""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        batch_size: int = 50,
        api_key: str | None = None,
        max_retries: int = 5,
        retry_base_seconds: float = 2.0,
        client: Any | None = None,
    ) -> None:
        raw_model = model_name or os.getenv("EMBEDDING_MODEL", "") or DEFAULT_GEMINI_MODEL
        self.model_name = _normalize_model_name(raw_model)
        if self.model_name != DEFAULT_GEMINI_MODEL:
            raise ValueError(
                f"Unsupported Gemini embedding model '{self.model_name}'. "
                f"Use '{DEFAULT_GEMINI_MODEL}' for batched text retrieval with task_type."
            )
        if not isinstance(dimensions, int) or not (128 <= dimensions <= 3072):
            raise ValueError("dimensions must be an integer between 128 and 3072.")
        if not isinstance(batch_size, int) or not (1 <= batch_size <= MAX_BATCH_SIZE):
            raise ValueError(f"batch_size must be an integer between 1 and {MAX_BATCH_SIZE}.")

        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.1, retry_base_seconds)

        if client is not None:
            self._client = client
        else:
            resolved_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
            if not resolved_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is missing. Set a real key in starter/.env; "
                    "mock fallback is disabled."
                )
            self._client = genai.Client(api_key=resolved_key)

        self.stats: dict[str, Any] = {
            "provider": "google-gemini",
            "model": self.model_name,
            "dimensions": self.dimensions,
            "normalized_non_default_dims": self.dimensions != 3072,
            "document_task_type": "RETRIEVAL_DOCUMENT",
            "query_task_type": "RETRIEVAL_QUERY",
            "api_calls": 0,
            "document_texts": 0,
            "query_texts": 0,
            "input_chars": 0,
            "reported_tokens": 0,
            "estimated_tokens": 0,
            "elapsed_seconds": 0.0,
            "retries": 0,
        }

    def reset_stats(self) -> None:
        """Reset cumulative embedding telemetry counters."""
        for key in (
            "api_calls",
            "document_texts",
            "query_texts",
            "input_chars",
            "reported_tokens",
            "estimated_tokens",
            "retries",
        ):
            self.stats[key] = 0
        self.stats["elapsed_seconds"] = 0.0

    def get_stats(self) -> dict[str, Any]:
        """Return a snapshot of embedding usage and timing statistics."""
        return dict(self.stats)

    def _embed_batch(self, texts: list[str], *, task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.dimensions,
        )
        t0 = time.perf_counter()
        response = self._call_with_retry(texts, config)
        self.stats["elapsed_seconds"] += time.perf_counter() - t0
        self.stats["api_calls"] += 1

        embeddings = getattr(response, "embeddings", None)
        if not embeddings or len(embeddings) != len(texts):
            got = 0 if not embeddings else len(embeddings)
            raise RuntimeError(f"Expected {len(texts)} embeddings from Gemini API, got {got}.")

        vectors: list[list[float]] = []
        reported_tokens = 0
        for item in embeddings:
            values = list(getattr(item, "values", None) or [])
            if len(values) != self.dimensions:
                raise RuntimeError(
                    f"Expected {self.dimensions}-dim vector from '{self.model_name}', got {len(values)}."
                )
            # Gemini docs require manual L2 normalization for gemini-embedding-001 when dims != 3072.
            vec = _l2_normalize([float(x) for x in values]) if self.dimensions != 3072 else [float(x) for x in values]
            vectors.append(vec)
            stats = getattr(item, "statistics", None)
            tok = getattr(stats, "token_count", None) if stats is not None else None
            if isinstance(tok, (int, float)):
                reported_tokens += int(tok)

        chars = sum(len(t) for t in texts)
        self.stats["input_chars"] += chars
        self.stats["estimated_tokens"] += sum(max(1, math.ceil(len(t) / 4)) for t in texts)
        self.stats["reported_tokens"] += reported_tokens
        return vectors

    def _call_with_retry(self, texts: list[str], config: types.EmbedContentConfig) -> Any:
        payload: Any = texts[0] if len(texts) == 1 else texts
        for attempt in range(self.max_retries + 1):
            try:
                return self._client.models.embed_content(
                    model=self.model_name,
                    contents=payload,
                    config=config,
                )
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable_error(exc):
                    raise
                delay = _extract_retry_seconds(exc)
                if delay is None:
                    delay = self.retry_base_seconds * (2 ** attempt)
                self.stats["retries"] += 1
                time.sleep(min(delay, 60.0))
        raise RuntimeError("Unreachable retry loop exit.")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document chunks with `RETRIEVAL_DOCUMENT` task type in batches."""
        if not texts:
            return []
        cleaned = [t for t in texts]
        if any(not isinstance(t, str) or not t.strip() for t in cleaned):
            raise ValueError("All document texts passed to embed_documents must be non-empty strings.")

        vectors: list[list[float]] = []
        for start in range(0, len(cleaned), self.batch_size):
            batch = cleaned[start : start + self.batch_size]
            vectors.extend(self._embed_batch(batch, task_type="RETRIEVAL_DOCUMENT"))
        self.stats["document_texts"] += len(cleaned)
        return vectors

    def embed_query(self, question: str) -> list[float]:
        """Embed a search question with `RETRIEVAL_QUERY` task type."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string.")
        vector = self._embed_batch([question], task_type="RETRIEVAL_QUERY")[0]
        self.stats["query_texts"] += 1
        return vector
