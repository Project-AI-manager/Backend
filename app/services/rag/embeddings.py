"""Embedding providers used by the RAG pipeline."""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

VECTOR_DIM = 1024
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedding(EmbeddingProvider):
    """Deterministic local embedding for dev/test without external ML calls."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hashing_vector(text) for text in texts]


def get_embedder() -> EmbeddingProvider:
    return LocalEmbedding()


def _hashing_vector(text: str) -> list[float]:
    vector = [0.0] * VECTOR_DIM
    tokens = [token.lower() for token in TOKEN_RE.findall(text) if len(token) > 2]
    if not tokens:
        return vector

    for token in tokens:
        index, sign = _token_bucket(token)
        vector[index] += sign

    for left, right in zip(tokens, tokens[1:], strict=False):
        index, sign = _token_bucket(f"{left} {right}")
        vector[index] += 0.5 * sign

    length = math.sqrt(sum(value * value for value in vector))
    if length == 0:
        return vector
    return [round(value / length, 8) for value in vector]


def _token_bucket(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    raw = int.from_bytes(digest, "big")
    sign = 1.0 if raw & 1 else -1.0
    return raw % VECTOR_DIM, sign
