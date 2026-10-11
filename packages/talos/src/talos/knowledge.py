from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import importlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol
from uuid import uuid4

from talos.errors import TalosError
from talos.retrieval import RetrievalMetrics, parse_synonyms, query_variants, rank_items

_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[a-z0-9_\-/+=]{12,}"
)


class KnowledgeError(TalosError):
    code = "knowledge_error"


class EmbeddingTransientError(KnowledgeError):
    code = "embedding_transient_error"
    retryable = True


@dataclass(frozen=True, slots=True)
class RagSettings:
    dimensions: int
    chunk_characters: int
    chunk_overlap: int
    top_k: int
    similarity_threshold: float
    max_source_bytes: int
    max_chunks: int
    embedding_model: str
    embedding_adapter: str
    allowed_extensions: tuple[str, ...]
    max_query_characters: int
    max_ingestion_attempts: int
    retry_base_seconds: int
    max_pdf_pages: int
    max_pdf_parse_seconds: float
    embedding_api_base: str
    embedding_api_key_name: str
    embedding_timeout_seconds: float
    mode: str
    query_rewriting: bool
    query_expansion: bool
    max_query_variants: int
    dense_enabled: bool
    sparse_enabled: bool
    rrf_k: int
    rerank_enabled: bool
    rerank_adapter: str
    rerank_model: str
    rerank_api_base: str
    rerank_api_key_name: str
    rerank_timeout_seconds: float
    expansion_synonyms: str

    def __post_init__(self) -> None:
        self._validate_chunking()
        self._validate_source_limits()
        self._validate_retrieval()

    def _validate_chunking(self) -> None:
        if not 16 <= self.dimensions <= 4096:
            raise KnowledgeError("Embedding dimensions must be between 16 and 4096")
        if not 100 <= self.chunk_characters <= 10000:
            raise KnowledgeError("Chunk size must be between 100 and 10000")
        if not 0 <= self.chunk_overlap < self.chunk_characters:
            raise KnowledgeError("Chunk overlap is invalid")

    def _validate_source_limits(self) -> None:
        if not 1 <= self.top_k <= 50 or not 0 <= self.similarity_threshold <= 1:
            raise KnowledgeError("Retrieval limits are invalid")
        if not 1 <= self.max_source_bytes <= 10_000_000 or self.max_chunks <= 0:
            raise KnowledgeError("Knowledge source limits are invalid")
        if not 1 <= self.max_ingestion_attempts <= 10:
            raise KnowledgeError("Knowledge ingestion attempt limit is invalid")
        if not 1 <= self.retry_base_seconds <= 60:
            raise KnowledgeError("Knowledge retry base must be between 1 and 60 seconds")
        if not 1 <= self.max_pdf_pages <= 1000:
            raise KnowledgeError("PDF page limit is invalid")
        if not 1 <= self.max_pdf_parse_seconds <= 120:
            raise KnowledgeError("PDF parse timeout is invalid")
        if not 1 <= self.max_query_characters <= 10000:
            raise KnowledgeError("Knowledge query limit is invalid")
        if not self.allowed_extensions or any(
            not extension.startswith(".") for extension in self.allowed_extensions
        ):
            raise KnowledgeError("Knowledge source extensions are invalid")

    def _validate_retrieval(self) -> None:
        if not 0 < self.embedding_timeout_seconds <= 120:
            raise KnowledgeError("Embedding timeout is invalid")
        if self.mode not in {"basic", "advanced"}:
            raise KnowledgeError("RAG mode must be basic or advanced")
        if not self.dense_enabled and not self.sparse_enabled:
            raise KnowledgeError("At least one retrieval strategy must be enabled")
        if self.mode == "basic" and not self.dense_enabled:
            raise KnowledgeError("Basic RAG requires dense retrieval")
        if not 1 <= self.max_query_variants <= 8 or not 1 <= self.rrf_k <= 1000:
            raise KnowledgeError("Advanced retrieval limits are invalid")
        if self.rerank_adapter not in {"none", "talos.retrieval:lexical_rerank"}:
            raise KnowledgeError("Rerank adapter is not allowlisted")
        if not 0 < self.rerank_timeout_seconds <= 120:
            raise KnowledgeError("Rerank timeout is invalid")


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    id: str
    source_id: str
    source_name: str
    ordinal: int
    text: str
    vector: tuple[float, ...]
    start_line: int
    end_line: int
    checksum: str
    embedding_model: str
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    id: str
    name: str
    checksum: str
    chunk_count: int
    embedding_model: str
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    marker: str
    chunk: KnowledgeChunk
    score: float


class KnowledgeStore(Protocol):
    async def replace(
        self,
        source: KnowledgeSource,
        chunks: Sequence[KnowledgeChunk],
        *,
        lease_attempt: int | None = None,
    ) -> None: ...

    async def list_sources(self) -> list[KnowledgeSource]: ...

    async def list_chunks(
        self, *, limit: int, conversation_id: str | None = None
    ) -> list[KnowledgeChunk]: ...


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> tuple[float, ...]: ...


class VectorStore(Protocol):
    async def search(
        self,
        vector: tuple[float, ...],
        *,
        model: str,
        candidates: int,
        top_k: int,
        threshold: float,
    ) -> list[tuple[KnowledgeChunk, float]]: ...


class JsonCosineVectorStore:
    """Bounded application cosine over JSON vectors returned by KnowledgeStore."""

    def __init__(self, store: KnowledgeStore, dimensions: int) -> None:
        self._store = store
        self._dimensions = dimensions

    async def search(
        self,
        vector: tuple[float, ...],
        *,
        model: str,
        candidates: int,
        top_k: int,
        threshold: float,
    ) -> list[tuple[KnowledgeChunk, float]]:
        chunks = await self._store.list_chunks(limit=candidates)
        if len(vector) != self._dimensions or any(
            len(chunk.vector) != self._dimensions or chunk.embedding_model != model
            for chunk in chunks
        ):
            raise KnowledgeError("Knowledge index has an incompatible embedding model")
        scored = [
            (sum(a * b for a, b in zip(vector, chunk.vector, strict=True)), chunk)
            for chunk in chunks
        ]
        selected = sorted(scored, key=lambda item: (-item[0], item[1].source_name, item[1].ordinal))
        return [(chunk, score) for score, chunk in selected if score >= threshold][:top_k]


class InMemoryKnowledgeStore:
    def __init__(self) -> None:
        self._sources: dict[tuple[str | None, str], KnowledgeSource] = {}
        self._chunks: dict[tuple[str | None, str], list[KnowledgeChunk]] = {}
        self._lock = asyncio.Lock()

    async def replace(
        self,
        source: KnowledgeSource,
        chunks: Sequence[KnowledgeChunk],
        *,
        lease_attempt: int | None = None,
    ) -> None:
        if lease_attempt is not None and lease_attempt < 1:
            raise KnowledgeError("Lease attempts must be positive")
        key = (source.conversation_id, source.name)
        async with self._lock:
            self._sources[key] = source
            self._chunks[key] = list(chunks)

    async def list_sources(self) -> list[KnowledgeSource]:
        async with self._lock:
            return sorted(self._sources.values(), key=lambda item: item.name)

    async def list_chunks(
        self, *, limit: int, conversation_id: str | None = None
    ) -> list[KnowledgeChunk]:
        async with self._lock:
            groups = (
                group
                for (scope, _), group in self._chunks.items()
                if scope is None or scope == conversation_id
            )
            return [chunk for group in groups for chunk in group][:limit]


class HashingEmbedder:
    """Deterministic, local lexical embedding; no network or credential required."""

    def __init__(
        self, settings: RagSettings, *, environment: Mapping[str, str] | None = None
    ) -> None:
        self.dimensions = settings.dimensions
        self.model = settings.embedding_model

    async def embed(self, text: str) -> tuple[float, ...]:
        await asyncio.sleep(0)
        vector = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return tuple(value / norm for value in vector) if norm else tuple(vector)


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore,
        settings: RagSettings,
        *,
        embedder: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.vector_store = vector_store or JsonCosineVectorStore(store, settings.dimensions)
        self.last_metrics = RetrievalMetrics(settings.mode, 1, 0, 0, 0, 0, 0)
        if embedder is not None:
            self.embedder = embedder
        else:
            module_name, separator, symbol = settings.embedding_adapter.partition(":")
            if not separator or (module_name, symbol) not in {
                ("talos.knowledge", "HashingEmbedder"),
                ("talos.embeddings", "OpenAICompatibleEmbedder"),
            }:
                raise KnowledgeError("Embedding adapter is not allowlisted")
            try:
                adapter = getattr(importlib.import_module(module_name), symbol)
            except (ImportError, AttributeError) as exc:
                raise KnowledgeError("Embedding adapter is unavailable") from exc
            if not callable(adapter):
                raise KnowledgeError("Embedding adapter is not callable")
            selected = adapter(settings, environment=environment)
            if not hasattr(selected, "embed"):
                raise KnowledgeError("Embedding adapter does not implement embed")
            self.embedder = selected

    def validate_text(self, name: str, content: str) -> None:
        if PurePath(name).name != name or "\\" in name or not name or len(name) > 200:
            raise KnowledgeError("Source name must be a short filename")
        if (
            name.lower().startswith(".env")
            or name.lower().endswith(".pdf")
            or PurePath(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
            or PurePath(name).suffix.lower() not in self.settings.allowed_extensions
        ):
            raise KnowledgeError("Unsupported knowledge source type")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > self.settings.max_source_bytes or "\x00" in content:
            raise KnowledgeError("Knowledge source is empty, binary, or too large")
        if _SECRET.search(content):
            raise KnowledgeError("Knowledge source may contain a secret")

    def validate_pdf(self, name: str, content: str) -> bytes:
        if PurePath(name).name != name or "\\" in name or not name.lower().endswith(".pdf"):
            raise KnowledgeError("PDF source name is invalid")
        if ".pdf" not in self.settings.allowed_extensions:
            raise KnowledgeError("PDF ingestion is not enabled")
        if len(content) > (self.settings.max_source_bytes * 4 // 3) + 8:
            raise KnowledgeError("PDF source exceeds its size limit")
        try:
            payload = base64.b64decode(content, validate=True)
        except binascii.Error as exc:
            raise KnowledgeError("PDF content must be base64-encoded") from exc
        if not payload.startswith(b"%PDF-") or len(payload) > self.settings.max_source_bytes:
            raise KnowledgeError("PDF signature or size is invalid")
        return payload

    def validate_source(self, name: str, content: str) -> None:
        if name.lower().endswith(".pdf"):
            self.validate_pdf(name, content)
        else:
            self.validate_text(name, content)

    async def ingest_pdf(
        self,
        name: str,
        content: str,
        *,
        source_id: str | None = None,
        lease_attempt: int | None = None,
        conversation_id: str | None = None,
    ) -> KnowledgeSource:
        payload = self.validate_pdf(name, content)
        from talos.pdf_extract import extract_pdf_pages

        pages = await extract_pdf_pages(
            payload,
            max_pages=self.settings.max_pdf_pages,
            max_text_bytes=self.settings.max_source_bytes,
            parse_seconds=self.settings.max_pdf_parse_seconds,
        )
        if not any(page.strip() for page in pages):
            raise KnowledgeError("PDF contains no extractable text")
        if sum(len(page.encode("utf-8")) for page in pages) > self.settings.max_source_bytes:
            raise KnowledgeError("Extracted PDF text exceeds its size limit")
        if any(_SECRET.search(page) for page in pages):
            raise KnowledgeError("PDF may contain a secret")
        source_id = source_id or str(uuid4())
        chunks: list[KnowledgeChunk] = []
        for page_number, page in enumerate(pages, start=1):
            for text, start, end in _chunk_lines(
                page, self.settings.chunk_characters, self.settings.chunk_overlap
            ):
                if len(chunks) >= self.settings.max_chunks:
                    raise KnowledgeError("PDF exceeds its chunk limit")
                chunks.append(
                    KnowledgeChunk(
                        id=str(uuid4()),
                        source_id=source_id,
                        source_name=name,
                        ordinal=len(chunks),
                        text=text,
                        vector=await self.embedder.embed(text),
                        start_line=start,
                        end_line=end,
                        checksum=hashlib.sha256(text.encode()).hexdigest(),
                        embedding_model=self.settings.embedding_model,
                        page_number=page_number,
                    )
                )
        source = KnowledgeSource(
            id=source_id,
            name=name,
            checksum=hashlib.sha256(payload).hexdigest(),
            chunk_count=len(chunks),
            embedding_model=self.settings.embedding_model,
            conversation_id=conversation_id,
        )
        await self.store.replace(source, chunks, lease_attempt=lease_attempt)
        return source

    async def ingest_text(
        self,
        name: str,
        content: str,
        *,
        source_id: str | None = None,
        lease_attempt: int | None = None,
        conversation_id: str | None = None,
    ) -> KnowledgeSource:
        self.validate_text(name, content)
        encoded = content.encode("utf-8")
        pieces = _chunk_lines(content, self.settings.chunk_characters, self.settings.chunk_overlap)
        if len(pieces) > self.settings.max_chunks:
            raise KnowledgeError("Knowledge source exceeds its chunk limit")
        source_id = source_id or str(uuid4())
        chunks = [
            KnowledgeChunk(
                id=str(uuid4()),
                source_id=source_id,
                source_name=name,
                ordinal=index,
                text=text,
                vector=await self.embedder.embed(text),
                start_line=start,
                end_line=end,
                checksum=hashlib.sha256(text.encode()).hexdigest(),
                embedding_model=self.settings.embedding_model,
            )
            for index, (text, start, end) in enumerate(pieces)
        ]
        source = KnowledgeSource(
            id=source_id,
            name=name,
            checksum=hashlib.sha256(encoded).hexdigest(),
            chunk_count=len(chunks),
            embedding_model=self.settings.embedding_model,
            conversation_id=conversation_id,
        )
        await self.store.replace(source, chunks, lease_attempt=lease_attempt)
        return source

    async def search(
        self,
        query: str,
        *,
        history: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> list[KnowledgeHit]:
        if not query.strip() or len(query) > self.settings.max_query_characters:
            raise KnowledgeError("Knowledge query is empty or too long")
        variants = query_variants(
            query,
            history=history,
            rewrite=self.settings.mode == "advanced" and self.settings.query_rewriting,
            expand=self.settings.mode == "advanced" and self.settings.query_expansion,
            synonyms=parse_synonyms(self.settings.expansion_synonyms),
            maximum=self.settings.max_query_variants,
        )
        chunks = await self.store.list_chunks(
            limit=self.settings.max_chunks, conversation_id=conversation_id
        )
        ranked, self.last_metrics = await rank_items(
            chunks,
            variants,
            text=lambda chunk: chunk.text,
            vector=lambda chunk: chunk.vector,
            embed=self.embedder.embed,
            mode=self.settings.mode,
            dense_enabled=self.settings.dense_enabled,
            sparse_enabled=self.settings.mode == "advanced" and self.settings.sparse_enabled,
            rrf_k=self.settings.rrf_k,
            rerank_enabled=self.settings.mode == "advanced" and self.settings.rerank_enabled,
            top_k=self.settings.top_k,
            threshold=self.settings.similarity_threshold,
        )
        return [
            KnowledgeHit(marker=f"K{index}", chunk=chunks[item.index], score=item.score)
            for index, item in enumerate(ranked, start=1)
        ]


def _chunk_lines(content: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    lines = content.splitlines()
    output: list[tuple[str, int, int]] = []
    start = 0
    while start < len(lines):
        end = start
        length = 0
        while end < len(lines) and (length < size or end == start):
            length += len(lines[end]) + 1
            end += 1
        text = "\n".join(lines[start:end]).strip()
        if text:
            output.append((text, start + 1, end))
        if end == len(lines):
            break
        previous = end
        backward = 0
        while end > start + 1 and backward < overlap:
            end -= 1
            backward += len(lines[end]) + 1
        start = max(start + 1, min(end, previous - 1))
    return output
