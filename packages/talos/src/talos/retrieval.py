from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    mode: str
    query_variants: int
    candidates: int
    dense_candidates: int
    sparse_candidates: int
    returned: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RankedItem:
    index: int
    score: float
    dense_score: float
    sparse_score: float


def query_variants(
    query: str,
    *,
    history: Sequence[str],
    rewrite: bool,
    expand: bool,
    synonyms: Mapping[str, tuple[str, ...]],
    maximum: int,
) -> tuple[str, ...]:
    values = [query.strip()]
    if rewrite and history:
        context = " ".join(item.strip() for item in history[-2:] if item.strip())
        if context:
            values.append(f"{context} {query}".strip())
    if expand:
        tokens = _tokens(query)
        extras = [synonym for token in tokens for synonym in synonyms.get(token, ())]
        if extras:
            values.append(f"{query} {' '.join(extras)}")
    unique: list[str] = []
    for value in values:
        if value and value.casefold() not in {item.casefold() for item in unique}:
            unique.append(value)
    return tuple(unique[:maximum])


async def rank_items[T](
    items: Sequence[T],
    variants: Sequence[str],
    *,
    text: Callable[[T], str],
    vector: Callable[[T], Sequence[float]],
    embed: Callable[[str], Awaitable[Sequence[float]]],
    mode: str,
    dense_enabled: bool,
    sparse_enabled: bool,
    rrf_k: int,
    rerank_enabled: bool,
    top_k: int,
    threshold: float,
) -> tuple[list[RankedItem], RetrievalMetrics]:
    started = time.perf_counter()
    if not items:
        return [], RetrievalMetrics(mode, len(variants), 0, 0, 0, 0, 0)
    dense_scores = await _dense_scores(items, variants, vector, embed, dense_enabled)
    sparse_scores = _sparse_scores(items, variants, text, sparse_enabled)

    if mode == "basic":
        selected = _basic_selection(dense_scores, threshold, top_k)
    else:
        selected = _advanced_selection(
            items,
            variants,
            text,
            dense_scores,
            sparse_scores,
            dense_enabled,
            sparse_enabled,
            rerank_enabled,
            rrf_k,
            threshold,
            top_k,
        )
    elapsed = int((time.perf_counter() - started) * 1000)
    metrics = RetrievalMetrics(
        mode=mode,
        query_variants=len(variants),
        candidates=len(items),
        dense_candidates=sum(score >= threshold for score in dense_scores),
        sparse_candidates=sum(score > 0 for score in sparse_scores),
        returned=len(selected),
        duration_ms=elapsed,
    )
    return selected, metrics


async def _dense_scores[T](
    items: Sequence[T],
    variants: Sequence[str],
    vector: Callable[[T], Sequence[float]],
    embed: Callable[[str], Awaitable[Sequence[float]]],
    enabled: bool,
) -> list[float]:
    scores = [0.0] * len(items)
    if not enabled:
        return scores
    for query in variants:
        query_vector = await embed(query)
        for index, item in enumerate(items):
            candidate = vector(item)
            if len(candidate) == len(query_vector):
                similarity = sum(a * b for a, b in zip(query_vector, candidate, strict=True))
                scores[index] = max(scores[index], similarity)
    return scores


def _sparse_scores[T](
    items: Sequence[T],
    variants: Sequence[str],
    text: Callable[[T], str],
    enabled: bool,
) -> list[float]:
    if not enabled:
        return [0.0] * len(items)
    query_terms = Counter(token for query in variants for token in _tokens(query))
    document_terms = [Counter(_tokens(text(item))) for item in items]
    document_frequency = Counter(token for terms in document_terms for token in terms)
    average_length = max(
        1.0, sum(sum(terms.values()) for terms in document_terms) / len(document_terms)
    )
    return [
        _bm25_score(terms, query_terms, document_frequency, len(items), average_length)
        for terms in document_terms
    ]


def _bm25_score(
    terms: Counter[str],
    query_terms: Counter[str],
    document_frequency: Counter[str],
    document_count: int,
    average_length: float,
) -> float:
    length = max(1, sum(terms.values()))
    score = 0.0
    for token, query_frequency in query_terms.items():
        frequency = terms[token]
        if not frequency:
            continue
        inverse_frequency = math.log(
            1
            + (document_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
        )
        saturation = (frequency * 2.2) / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))
        score += inverse_frequency * saturation * query_frequency
    return score


def _basic_selection(scores: Sequence[float], threshold: float, top_k: int) -> list[RankedItem]:
    ranked = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    return [
        RankedItem(index, scores[index], scores[index], 0.0)
        for index in ranked
        if scores[index] >= threshold
    ][:top_k]


def _advanced_selection[T](
    items: Sequence[T],
    variants: Sequence[str],
    text: Callable[[T], str],
    dense_scores: Sequence[float],
    sparse_scores: Sequence[float],
    dense_enabled: bool,
    sparse_enabled: bool,
    rerank_enabled: bool,
    rrf_k: int,
    threshold: float,
    top_k: int,
) -> list[RankedItem]:
    dense_rank = _rank_positions(dense_scores) if dense_enabled else {}
    sparse_rank = _rank_positions(sparse_scores) if sparse_enabled else {}
    fused = [
        (
            (1 / (rrf_k + dense_rank[index]) if index in dense_rank else 0.0)
            + (1 / (rrf_k + sparse_rank[index]) if index in sparse_rank else 0.0),
            index,
        )
        for index in range(len(items))
    ]
    if rerank_enabled:
        query_tokens = {token for query in variants for token in _tokens(query)}
        fused = [
            (
                score
                + len(query_tokens.intersection(_tokens(text(items[index]))))
                / max(1, len(query_tokens))
                * 0.01,
                index,
            )
            for score, index in fused
        ]
    return [
        RankedItem(index, score, dense_scores[index], sparse_scores[index])
        for score, index in sorted(fused, key=lambda item: (-item[0], item[1]))[:top_k]
        if dense_scores[index] >= threshold or sparse_scores[index] > 0
    ]


def parse_synonyms(value: str) -> Mapping[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    for group in value.split(";"):
        key, separator, raw_values = group.partition(":")
        if separator and key.strip():
            output[key.strip().casefold()] = tuple(
                item.strip() for item in raw_values.split(",") if item.strip()
            )
    return output


def _tokens(value: str) -> list[str]:
    return [item.casefold() for item in _TOKEN.findall(value)]


def _rank_positions(scores: Sequence[float]) -> dict[int, int]:
    return {
        index: rank
        for rank, index in enumerate(
            sorted(range(len(scores)), key=lambda item: (-scores[item], item)),
            start=1,
        )
        if scores[index] > 0
    }
