"""Relevance scorers for query-aware compression.

Protocol + implementations: BM25 (default, zero-latency) and Embedding
(semantic, requires Ollama/OpenAI). Switching strategy: use embedding
when available, fall back to BM25.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from typing import Any, Protocol

from memtomem_stm.utils.digest import framed_digest

logger = logging.getLogger(__name__)


def _fifo_prune(d: dict[str, Any], cap: int) -> None:
    """Evict oldest (first-inserted) entries once *cap* is exceeded.

    Prunes to roughly half the cap so steady-state inserts don't re-trip on
    every call. Same policy as the surfacing engine's bounded guard maps,
    copied rather than imported: ``proxy`` does not depend on ``surfacing`` at
    runtime, and five lines are cheaper than that edge.
    """
    if len(d) <= cap:
        return
    # Floor the target at one entry: ``cap // 2`` is 0 at ``cap = 1``, which
    # would empty the map on every insert and make a legal capacity behave
    # like the disabled setting.
    excess = len(d) - max(1, cap // 2)
    for k in list(d)[:excess]:
        del d[k]


class RelevanceScorer(Protocol):
    """Scores sections by relevance to a query. Higher = more relevant.

    Implementations that perform blocking I/O inside ``score_sections``
    (network calls, disk reads) should additionally set a class attribute
    ``uses_blocking_io = True``. It is deliberately *not* part of the
    Protocol body — callers read it via ``getattr(scorer, "uses_blocking_io",
    False)`` so existing structural implementations stay conformant — and
    async call sites use it to decide whether to off-load the surrounding
    sync compression to a worker thread (#618).
    """

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]: ...


# ── BM25 Scorer ───────────────────────────────────────────────────────


class BM25Scorer:
    """BM25-like section relevance scoring with heading weighting.

    Headings are weighted 3× over body text. Case-insensitive with
    basic suffix stemming. Zero external dependencies.
    """

    _TOKEN_RE = re.compile(
        r"[a-zA-Z0-9"
        r"\uac00-\ud7a3"  # Korean (Hangul syllables)
        r"\u3040-\u309f"  # Hiragana
        r"\u30a0-\u30ff"  # Katakana
        r"\u4e00-\u9fff"  # CJK Unified Ideographs
        r"\u0400-\u04ff"  # Cyrillic
        r"\u0600-\u06ff"  # Arabic
        r"\u0900-\u097f"  # Devanagari
        r"\u0e00-\u0e7f"  # Thai
        r"_]+"
    )
    _SUFFIX_RE = re.compile(r"(ing|ed|ly|tion|ness|ment|ies|es|s)$")
    _HEADING_WEIGHT = 3.0

    # Pure CPU — safe to run inline on the event loop (see RelevanceScorer).
    uses_blocking_io = False

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        query_terms = self._tokenize(query)
        if not query_terms or not sections:
            return [0.0] * len(sections)

        # Pre-compute per-section TF (heading-weighted)
        doc_tfs: list[dict[str, float]] = []
        doc_lens: list[float] = []
        for title, body in sections:
            heading_tokens = self._tokenize(title)
            body_tokens = self._tokenize(body)
            tf: dict[str, float] = {}
            for t in heading_tokens:
                tf[t] = tf.get(t, 0.0) + self._HEADING_WEIGHT
            for t in body_tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            doc_tfs.append(tf)
            doc_lens.append(len(heading_tokens) * self._HEADING_WEIGHT + len(body_tokens))

        avgdl = sum(doc_lens) / len(doc_lens) if doc_lens else 1.0
        if avgdl == 0.0:
            # Every section tokenized to nothing (symbols-only content):
            # dl/avgdl in the BM25 denominator below would divide by zero.
            # Any positive stand-in works — tf is 0 everywhere, so every
            # score comes out 0.0 regardless.
            avgdl = 1.0

        # IDF per query term
        n = len(sections)
        idfs: dict[str, float] = {}
        for t in set(query_terms):
            df = sum(1 for tfs in doc_tfs if t in tfs)
            idfs[t] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

        # BM25 score per section
        scores: list[float] = []
        for i in range(n):
            total = 0.0
            for t in query_terms:
                tf_val = doc_tfs[i].get(t, 0.0)
                idf = idfs.get(t, 0.0)
                num = tf_val * (self._k1 + 1.0)
                den = tf_val + self._k1 * (1.0 - self._b + self._b * doc_lens[i] / avgdl)
                total += idf * num / den if den > 0 else 0.0
            scores.append(total)
        return scores

    def _tokenize(self, text: str) -> list[str]:
        tokens = self._TOKEN_RE.findall(text.lower())
        return [self._stem(t) for t in tokens]

    def _stem(self, token: str) -> str:
        if len(token) > 4:
            return self._SUFFIX_RE.sub("", token)
        return token


# ── Embedding Scorer ──────────────────────────────────────────────────


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    # Each vector is divided by its own largest magnitude before multiplying.
    # ``sum(x * y ...)`` overflows to ``inf`` on components a provider can
    # legitimately serialise — ``1e308`` is finite, ``1e308 ** 2`` is not — and
    # ``inf / inf`` is ``nan``, a score that is neither high nor low and that
    # sorts unpredictably. Scaling cancels exactly in the ratio, which is all
    # cosine needs, so ordinary inputs are unaffected.
    scale_a = max(map(abs, a), default=0.0)
    scale_b = max(map(abs, b), default=0.0)
    if scale_a == 0.0 or scale_b == 0.0:
        return 0.0
    dot = sum((x / scale_a) * (y / scale_b) for x, y in zip(a, b))
    norm_a = math.sqrt(sum((x / scale_a) ** 2 for x in a))
    norm_b = math.sqrt(sum((y / scale_b) ** 2 for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _payload_list(payload: object, *, key: str, provider: str) -> list[Any]:
    """Return ``payload[key]`` as a list, or raise saying what was wrong.

    ``score_sections`` logs the exception MESSAGE and nothing else — a
    deliberate choice, so that an expected fallback does not bury real errors
    in a log aggregator. That makes the message the operator's entire signal,
    and ``KeyError('embeddings')`` named neither the provider that answered nor
    what the body actually held. An error envelope and a parser bug looked
    identical.

    An EMPTY list is returned as-is rather than rejected: ``_embed_exact``
    owns the count contract, and embedding zero texts legitimately yields zero
    vectors. A missing key is never softened into an empty result — that would
    turn a failed request into a silent all-zero ranking.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"{provider} embedding response is not a JSON object (got {type(payload).__name__})"
        )
    if key not in payload:
        present = ", ".join(sorted(str(k) for k in payload)) or "(no keys)"
        raise ValueError(f"{provider} embedding response has no '{key}' field; it holds: {present}")
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(
            f"{provider} embedding response field '{key}' is {type(value).__name__}, not a list"
        )
    return value


def _require_vector(value: object, *, provider: str, where: str) -> list[float]:
    """Return ``value`` as a vector of finite numbers, or say what it is not.

    Checking that the container is a list is not enough. ``_embed_exact``'s
    contract is a COUNT — as many vectors back as texts sent — so a reply like
    ``{"embeddings": [[0.1, 0.2], ["bad", 0.2], [0.3, 0.4]]}`` satisfied it and
    the bad row was written to the cache, where it answered every later call
    for that text without a request. The element check is what makes the
    cache-write guarantee true rather than nearly true.

    ``bool`` is excluded deliberately: it passes ``isinstance(x, int)`` and is
    never a component of an embedding.
    """
    if not isinstance(value, list):
        raise ValueError(
            f"{provider} embedding response: '{where}' is {type(value).__name__}, not a list"
        )
    for i, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(
                f"{provider} embedding response: '{where}[{i}]' is "
                f"{type(component).__name__}, not a number"
            )
        if not math.isfinite(component):
            raise ValueError(f"{provider} embedding response: '{where}[{i}]' is {component}")
    return value


def _require_uniform_dimensions(vectors: list[list[float]], *, provider: str) -> None:
    """Every vector compared against another must have the same length.

    ``_cosine_similarity`` pairs components with ``zip``, which stops at the
    shorter vector — so a 1-D against a 2-D vector scores on the first
    component alone and reports a confident similarity for what is really an
    incomparable pair. Nothing raises, so no fallback is counted and nothing
    is logged.

    Checked over the vectors that will be COMPARED, not just the ones that
    arrived together: a query cached by an earlier call and a section fetched
    now are compared to each other.
    """
    dimensions = {len(vector) for vector in vectors}
    if 0 in dimensions:
        raise ValueError(f"{provider} embedding response contains an empty vector")
    if len(dimensions) > 1:
        raise ValueError(
            f"{provider} embeddings have differing dimensions ({sorted(dimensions)}); "
            "a similarity across them is silently wrong"
        )


class EmbeddingScorer:
    """Semantic relevance scoring via embedding cosine similarity.

    Uses sync httpx to call Ollama or OpenAI embedding API.
    Falls back to BM25Scorer on any error (network, timeout, model not loaded).
    """

    # Sync HTTP call inside score_sections — async call sites off-load the
    # surrounding compression to a worker thread so the request can't stall
    # the event loop for up to the full timeout (#618).
    uses_blocking_io = True

    def __init__(
        self,
        provider: str = "ollama",
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        timeout: float = 10.0,
        cache_size: int = 256,
    ) -> None:
        api_key = ""
        if provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required when "
                    "EmbeddingScorer provider='openai'"
                )
        self._provider = provider
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key
        self._fallback = BM25Scorer()
        self.fallback_count: int = 0
        # With uses_blocking_io=True the scorer runs on worker threads while
        # manager metrics read fallback_count from the loop thread; the lock
        # keeps concurrent increments from losing a count (the metrics
        # boolean-delta would silently flip to False).
        self._fallback_lock = threading.Lock()
        # Section bodies are derived deterministically from upstream responses
        # (JSON keys, markdown headings) and truncated, so the same text comes
        # back call after call while only the query rotates. A pass sends one
        # batch, so what a hit saves is that text's share of it — and a pass
        # that hits on everything saves the whole request, up to the full
        # timeout on a cold or slow backend. Keyed per instance rather than per
        # module: a config change
        # rebuilds the scorer (`ProxyManager._relevance_scorer_for`), which
        # discards the cache with it, so nothing has to invalidate on a model
        # or provider edit. Guarded by its own lock for the same reason
        # `fallback_count` is — with `uses_blocking_io` the scorer runs on
        # worker threads, several at once, on one shared instance. The prune
        # below reads the key order and then deletes from it, which is not one
        # atomic step; two threads pruning together can delete the same key
        # twice. CPython's GIL makes that narrow enough that a contention test
        # does not reliably reproduce it (16 threads x 80 passes at capacity 2
        # produced none), so the lock is here on the argument, not on a red
        # test — and a free-threaded build removes even that narrowness.
        self._cache_size = max(0, cache_size)
        self._cache: dict[str, list[float]] = {}
        self._cache_lock = threading.Lock()

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        if not query or not sections:
            return [0.0] * len(sections)

        try:
            return self._score_via_embedding(query, sections)
        except Exception as exc:
            # Expected fallback path (Ollama offline, network hiccup, timeout):
            # we already have a working BM25 scorer and fallback_count surfaces
            # the rate. Full stack on every hit buries real errors in log-
            # aggregation pipelines, so log the exception message only.
            with self._fallback_lock:
                self.fallback_count += 1
            logger.warning("EmbeddingScorer failed, falling back to BM25: %s", exc)
            return self._fallback.score_sections(query, sections)

    def _score_via_embedding(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        # Build texts: query + each section (title + body truncated)
        texts = [query]
        for title, body in sections:
            # Truncate body to ~500 chars to limit embedding cost
            section_text = f"{title}\n{body[:500]}"
            texts.append(section_text)

        embeddings = self._embed_cached(texts)
        query_emb = embeddings[0]
        return [_cosine_similarity(query_emb, emb) for emb in embeddings[1:]]

    def _embed_cached(self, texts: list[str]) -> list[list[float]]:
        """One embedding per text, fetching only the ones not already held.

        A repeat text costs a dict lookup instead of its share of an HTTP
        round trip, and a call whose texts are all held issues no request at
        all. The request keeps the shape it had — one batch — so a provider
        sees the same traffic pattern, only shorter.

        The lock is taken twice for as long as a dict operation, never across
        the request: two threads racing on the same miss both fetch it, which
        costs one redundant round trip and cannot produce a wrong answer, since
        an embedding is a function of (provider, model, text).
        """
        if self._cache_size == 0:
            return self._embed_exact(texts)

        keys = [framed_digest((self._provider, self._model, text)) for text in texts]
        with self._cache_lock:
            held = {key: self._cache[key] for key in keys if key in self._cache}

        # Deduplicated and order-preserving: the same section can appear twice
        # in one response, and asking for it twice would pay for it twice.
        misses: list[str] = []
        miss_keys: list[str] = []
        seen: set[str] = set()
        for key, text in zip(keys, texts):
            if key in held or key in seen:
                continue
            seen.add(key)
            miss_keys.append(key)
            misses.append(text)

        if misses:
            fetched = self._embed_exact(misses)
            # Across the cache too, not only within this batch. The batch check
            # in ``_embed_exact`` cannot see a vector an earlier call cached, so
            # a 2-D query already held and a 1-D section fetched now passed both
            # checks and compared 1.0. Validated BEFORE the write, so a reply
            # that disagrees with what is held never becomes what is held.
            _require_uniform_dimensions(list(held.values()) + fetched, provider=self._provider)
            with self._cache_lock:
                for key, embedding in zip(miss_keys, fetched):
                    self._cache[key] = embedding
                _fifo_prune(self._cache, self._cache_size)
            held.update(zip(miss_keys, fetched))
        else:
            _require_uniform_dimensions(list(held.values()), provider=self._provider)

        return [held[key] for key in keys]

    def _embed_exact(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, or an error naming the mismatch.

        The correspondence is positional: the nth vector answers the nth text.
        A reply with a different count has already lost it, so scoring it
        mis-ranks silently, and caching it would keep doing so for every later
        call. Raising lands in ``score_sections``'s fallback, which is where a
        provider that cannot be trusted belongs. Checked on the cached and
        uncached paths alike — a guard only the cache enforces would make
        ``embedding_cache_size = 0`` mean something other than "no cache".
        """
        embeddings = self._embed_batch(texts)
        if len(embeddings) != len(texts):
            raise ValueError(
                f"embedding provider returned {len(embeddings)} vectors for {len(texts)} inputs"
            )
        # The count is not the whole contract. Two batch-level shapes passed it
        # and were cached, where they answered every later call for those texts
        # without a request:
        #   [[], [], []]           -> every similarity 0.0, no ranking signal
        #   [[1.0, 0.0], [1.0]]    -> ``zip`` stops at the shorter vector, so a
        #                             1-D and a 2-D vector compared 1.0, a
        #                             confidently WRONG similarity
        # Neither raised, so ``fallback_count`` never moved and nothing was
        # logged. Checked here rather than per provider: it is a property of
        # the batch, and this is the last point before ``_embed_cached`` writes.
        _require_uniform_dimensions(embeddings, provider=self._provider)
        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for EmbeddingScorer")

        if self._provider == "ollama":
            return self._embed_ollama(httpx, texts)
        elif self._provider == "openai":
            return self._embed_openai(httpx, texts)
        else:
            raise ValueError(f"Unknown embedding provider: {self._provider}")

    def _embed_ollama(self, httpx_mod: object, texts: list[str]) -> list[list[float]]:
        import httpx as _httpx

        resp = _httpx.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        raw = _payload_list(resp.json(), key="embeddings", provider="ollama")
        return [
            _require_vector(vector, provider="ollama", where=f"embeddings[{i}]")
            for i, vector in enumerate(raw)
        ]

    def _embed_openai(self, httpx_mod: object, texts: list[str]) -> list[list[float]]:
        import httpx as _httpx

        url = self._base_url + "/v1/embeddings"
        resp = _httpx.post(
            url,
            json={"model": self._model, "input": texts, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = _payload_list(resp.json(), key="data", provider="openai")
        # Sort by "index" when the provider populates it (official OpenAI).
        # OpenAI-compatible servers — Ollama's compat layer, LiteLLM, LM Studio —
        # often omit the field, in which case we trust the input order. The
        # isinstance guard matters: ``"index" in d`` on a string is a substring
        # test, so a list of strings used to pass this check and then fail in
        # the sort key.
        if data and all(isinstance(d, dict) and "index" in d for d in data):
            # Sorting by a field nobody checked accepted duplicates and
            # out-of-range values: indices ``[0, 0, 2]`` sorted without error
            # and produced vectors with no correspondence to the inputs, which
            # were then cached. When the provider states the order it must
            # state it completely.
            indices = [entry["index"] for entry in data]
            if not all(isinstance(i, int) and not isinstance(i, bool) for i in indices):
                raise ValueError(
                    "openai embedding response: every 'data[].index' must be an integer"
                )
            if sorted(indices) != list(range(len(data))):
                raise ValueError(
                    f"openai embedding response: 'data[].index' is {sorted(indices)}, "
                    f"not a permutation of 0..{len(data) - 1}"
                )
            data.sort(key=lambda x: x["index"])
        vectors: list[list[float]] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise ValueError(f"openai embedding response: 'data[{i}].embedding' is missing")
            vectors.append(
                _require_vector(item["embedding"], provider="openai", where=f"data[{i}].embedding")
            )
        return vectors


# ── Factory ───────────────────────────────────────────────────────────


def create_scorer(
    scorer_type: str = "bm25",
    provider: str = "ollama",
    model: str = "nomic-embed-text",
    base_url: str = "http://localhost:11434",
    timeout: float = 10.0,
    cache_size: int = 256,
) -> RelevanceScorer:
    """Create a relevance scorer from config.

    Args:
        scorer_type: "bm25" (default) or "embedding"
        provider: "ollama" or "openai" (only for embedding)
        model: embedding model name
        base_url: embedding API base URL
        timeout: embedding API timeout in seconds
        cache_size: embeddings held per scorer instance; 0 disables the cache
    """
    if scorer_type == "embedding":
        return EmbeddingScorer(
            provider=provider,
            model=model,
            base_url=base_url,
            timeout=timeout,
            cache_size=cache_size,
        )
    return BM25Scorer()
