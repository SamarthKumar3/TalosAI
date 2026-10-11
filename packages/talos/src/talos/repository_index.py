from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from talos.knowledge import EmbeddingProvider, RagSettings, _chunk_lines
from talos.repository import RepositoryFile, RepositorySettings, validate_repository_file
from talos.retrieval import RetrievalMetrics, parse_synonyms, query_variants, rank_items

_SYMBOL = re.compile(
    r"^\s*(?:(?:async\s+)?def|class|function|export\s+(?:default\s+)?(?:function|class))"
    r"\s+([A-Za-z_][A-Za-z_0-9]*)",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class RepositoryChunk:
    id: str
    version_id: str
    path: str
    language: str
    file_checksum: str
    ordinal: int
    text: str
    vector: tuple[float, ...]
    start_line: int
    end_line: int
    checksum: str
    symbol: str | None


@dataclass(frozen=True, slots=True)
class RepositoryHit:
    marker: str
    path: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    commit: str
    dirty: bool
    snapshot: str
    checksum: str
    text: str
    score: float


class RepositoryStore(Protocol):
    async def reusable(
        self, repository_id: str, model: str
    ) -> dict[tuple[str, str], list[RepositoryChunk]]: ...

    async def publish(
        self, version_id: str, chunks: Sequence[RepositoryChunk], *, attempt: int
    ) -> bool: ...

    async def search_chunks(
        self, *, repository_id: str | None, model: str, limit: int
    ) -> list[tuple[RepositoryChunk, str, bool, str]]: ...


class RepositoryIndexService:
    def __init__(
        self,
        store: RepositoryStore,
        embedder: EmbeddingProvider,
        rag: RagSettings,
        settings: RepositorySettings,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.rag = rag
        self.settings = settings
        self.last_metrics = RetrievalMetrics(rag.mode, 1, 0, 0, 0, 0, 0)

    async def build(
        self,
        repository_id: str,
        version_id: str,
        files: Sequence[RepositoryFile],
        *,
        attempt: int,
    ) -> bool:
        previous = await self.store.reusable(repository_id, self.rag.embedding_model)
        chunks: list[RepositoryChunk] = []
        for item in files:
            validate_repository_file(item, self.settings)
            prior = previous.get((item.path, item.checksum))
            if prior is not None:
                chunks.extend(
                    RepositoryChunk(
                        id=str(uuid4()),
                        version_id=version_id,
                        path=chunk.path,
                        language=chunk.language,
                        file_checksum=chunk.file_checksum,
                        ordinal=chunk.ordinal,
                        text=chunk.text,
                        vector=chunk.vector,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        checksum=chunk.checksum,
                        symbol=chunk.symbol,
                    )
                    for chunk in prior
                )
                if len(chunks) > self.rag.max_chunks:
                    raise ValueError("Repository exceeds the configured chunk limit")
                continue
            for ordinal, (text, start, end) in enumerate(
                _chunk_lines(item.content, self.rag.chunk_characters, self.rag.chunk_overlap)
            ):
                if len(chunks) >= self.rag.max_chunks:
                    raise ValueError("Repository exceeds the configured chunk limit")
                match = _SYMBOL.search(text)
                chunks.append(
                    RepositoryChunk(
                        id=str(uuid4()),
                        version_id=version_id,
                        path=item.path,
                        language=item.language,
                        file_checksum=item.checksum,
                        ordinal=ordinal,
                        text=text,
                        vector=await self.embedder.embed(text),
                        start_line=start,
                        end_line=end,
                        checksum=hashlib.sha256(text.encode()).hexdigest(),
                        symbol=match.group(1) if match else None,
                    )
                )
        return await self.store.publish(version_id, chunks, attempt=attempt)

    async def search(
        self,
        query: str,
        *,
        repository_id: str | None = None,
        history: Sequence[str] = (),
    ) -> list[RepositoryHit]:
        if not query.strip() or len(query) > self.rag.max_query_characters:
            raise ValueError("Repository query is empty or too long")
        rows = await self.store.search_chunks(
            repository_id=repository_id,
            model=self.rag.embedding_model,
            limit=self.rag.max_chunks,
        )
        variants = query_variants(
            query,
            history=history,
            rewrite=self.rag.mode == "advanced" and self.rag.query_rewriting,
            expand=self.rag.mode == "advanced" and self.rag.query_expansion,
            synonyms=parse_synonyms(self.rag.expansion_synonyms),
            maximum=self.rag.max_query_variants,
        )
        ranked, self.last_metrics = await rank_items(
            rows,
            variants,
            text=lambda row: row[0].text,
            vector=lambda row: row[0].vector,
            embed=self.embedder.embed,
            mode=self.rag.mode,
            dense_enabled=self.rag.dense_enabled,
            sparse_enabled=self.rag.mode == "advanced" and self.rag.sparse_enabled,
            rrf_k=self.rag.rrf_k,
            rerank_enabled=self.rag.mode == "advanced" and self.rag.rerank_enabled,
            top_k=self.rag.top_k,
            threshold=self.rag.similarity_threshold,
        )
        return [
            RepositoryHit(
                marker=f"R{index}",
                path=chunk.path,
                language=chunk.language,
                symbol=chunk.symbol,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                commit=commit,
                dirty=dirty,
                snapshot=snapshot,
                checksum=chunk.checksum,
                text=chunk.text,
                score=item.score,
            )
            for index, item in enumerate(ranked, start=1)
            for chunk, commit, dirty, snapshot in [rows[item.index]]
        ]


def files_from_payload(payload: Sequence[Mapping[str, str]]) -> tuple[RepositoryFile, ...]:
    return tuple(
        RepositoryFile(
            path=item["path"],
            content=item["content"],
            checksum=item["checksum"],
            language=item["language"],
        )
        for item in payload
    )
