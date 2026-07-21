"""Embedding providers used by the RAG pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import settings

VECTOR_DIM = 1024  # Stable local-hashing dimension kept for backwards compatibility.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class EmbeddingProviderConfigurationError(RuntimeError):
    """Raised before a remote embedding request when configuration is incomplete."""


class EmbeddingProviderRequestError(RuntimeError):
    """Raised when an embedding provider returns no usable vectors."""


class EmbeddingProvider(ABC):
    provider_name = "base"
    dimension: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedding(EmbeddingProvider):
    """Deterministic local embedding for dev/test without external ML calls."""

    provider_name = "local"

    def __init__(self, *, dimension: int = VECTOR_DIM) -> None:
        if dimension <= 0:
            raise EmbeddingProviderConfigurationError("Embedding dimension must be positive")
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hashing_vector(text, dimension=self.dimension) for text in texts]


class OpenAICompatibleEmbedding(EmbeddingProvider):
    """Embedding provider implementing the OpenAI-compatible ``/embeddings`` contract."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout_sec: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.dimension = dimension
        self.timeout_sec = timeout_sec
        self.transport = transport
        missing = [
            name
            for name, value in (
                ("EMBEDDING_BASE_URL", self.base_url),
                ("EMBEDDING_API_KEY", self.api_key),
                ("EMBEDDING_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise EmbeddingProviderConfigurationError(
                "OpenAI-compatible embeddings are not configured: "
                + ", ".join(missing)
                + " is required"
            )
        if self.dimension <= 0:
            raise EmbeddingProviderConfigurationError("EMBEDDING_DIMENSION must be positive")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_sec,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise EmbeddingProviderRequestError(
                "OpenAI-compatible embedding request failed"
            ) from exc

        vectors = self._extract_vectors(payload, expected_count=len(texts))
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingProviderRequestError(
                    "Embedding dimension mismatch: "
                    f"provider returned {len(vector)}, configured {self.dimension}"
                )
        return vectors

    @staticmethod
    def _extract_vectors(payload: Any, *, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingProviderRequestError("Embedding provider returned an invalid response")
        ordered: list[tuple[int, list[float]]] = []
        for position, item in enumerate(payload["data"]):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise EmbeddingProviderRequestError(
                    "Embedding provider returned an invalid vector"
                )
            try:
                vector = [float(value) for value in item["embedding"]]
                index = int(item.get("index", position))
            except (TypeError, ValueError) as exc:
                raise EmbeddingProviderRequestError(
                    "Embedding provider returned a non-numeric vector"
                ) from exc
            ordered.append((index, vector))
        ordered.sort(key=lambda item: item[0])
        vectors = [vector for _, vector in ordered]
        if len(vectors) != expected_count:
            raise EmbeddingProviderRequestError(
                f"Embedding provider returned {len(vectors)} vectors for {expected_count} inputs"
            )
        return vectors


def get_embedder(
    model: str | None = None,
    *,
    timeout_sec: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    ) -> EmbeddingProvider:
    configured_provider = settings.EMBEDDING_PROVIDER.strip().lower()
    configured_model = (model or settings.EMBEDDING_MODEL).strip()
    if configured_provider in {"local", "hashing"}:
        return LocalEmbedding(dimension=settings.EMBEDDING_DIMENSION)
    if configured_provider in {"openai", "openai-compatible", "unirouter"}:
        return OpenAICompatibleEmbedding(
            base_url=settings.EMBEDDING_BASE_URL,
            api_key=settings.EMBEDDING_API_KEY,
            model=configured_model,
            dimension=settings.EMBEDDING_DIMENSION,
            timeout_sec=timeout_sec or settings.EMBEDDING_TIMEOUT_SEC,
            transport=transport,
        )
    raise EmbeddingProviderConfigurationError(
        f"Unsupported embedding provider '{settings.EMBEDDING_PROVIDER}'"
    )


def _hashing_vector(text: str, *, dimension: int) -> list[float]:
    vector = [0.0] * dimension
    tokens = [token.lower() for token in TOKEN_RE.findall(text) if len(token) > 2]
    if not tokens:
        return vector

    for token in tokens:
        index, sign = _token_bucket(token, dimension=dimension)
        vector[index] += sign

    for left, right in zip(tokens, tokens[1:], strict=False):
        index, sign = _token_bucket(f"{left} {right}", dimension=dimension)
        vector[index] += 0.5 * sign

    length = math.sqrt(sum(value * value for value in vector))
    if length == 0:
        return vector
    return [round(value / length, 8) for value in vector]


def _token_bucket(token: str, *, dimension: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    raw = int.from_bytes(digest, "big")
    sign = 1.0 if raw & 1 else -1.0
    return raw % dimension, sign
