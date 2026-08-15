"""Bounded query-embedding adapter for the standalone MCP service."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from hashlib import sha256
from typing import Any

from .quota import EmbeddingBudget, SQLiteDailyEmbeddingBudget


DEFAULT_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


def _enabled(value: str | None) -> bool:
    return (value or "1").strip().lower() not in {"0", "false", "no", "off"}


class OpenAIEmbeddingProvider:
    """Generate and locally cache fixed-size query embeddings.

    The OpenAI client is created lazily, uses no automatic retries, and has a
    short timeout. This adapter never accepts a user-supplied provider key.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 10.0,
        cache_size: int = 4096,
        budget: EmbeddingBudget | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        self._cache_size = max(0, min(int(cache_size), 10_000))
        self._budget = budget
        self._client = client
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> OpenAIEmbeddingProvider | None:
        """Return a provider only when server-funded embeddings are enabled."""
        if not _enabled(os.getenv("QG_MCP_EMBEDDINGS_ENABLED")):
            return None
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=(os.getenv("QG_MCP_EMBEDDING_MODEL") or DEFAULT_MODEL).strip(),
            timeout_seconds=float(os.getenv("QG_MCP_EMBEDDING_TIMEOUT_SECONDS", "10")),
            cache_size=int(os.getenv("QG_MCP_EMBEDDING_CACHE_SIZE", "4096")),
            budget=SQLiteDailyEmbeddingBudget.from_environment(),
        )

    def __call__(self, text: str) -> list[float]:
        normalized = " ".join(text.split()).strip()
        cache_key = sha256(normalized.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return list(cached)

        if self._budget is not None:
            self._budget.acquire()
        vector = self._request_embedding(normalized)

        if self._cache_size:
            with self._lock:
                self._cache[cache_key] = vector
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return list(vector)

    def startup_probe(self, text: str) -> list[float]:
        """Verify capacity and provider health without reserving a budget unit."""
        if self._budget is not None:
            self._budget.check_available()
        normalized = " ".join(text.split()).strip()
        return list(self._request_embedding(normalized))

    def _request_embedding(self, normalized: str) -> tuple[float, ...]:
        response = self._get_client().embeddings.create(
            model=self._model,
            input=[normalized],
            dimensions=EMBEDDING_DIMENSIONS,
        )
        vector = tuple(float(value) for value in response.data[0].embedding)
        return vector

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key,
                timeout=self._timeout_seconds,
                max_retries=0,
            )
        return self._client


__all__ = ["DEFAULT_MODEL", "EMBEDDING_DIMENSIONS", "OpenAIEmbeddingProvider"]
