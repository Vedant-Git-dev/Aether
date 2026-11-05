"""The lexical BM25 index over memory chunks.

The vector path answers "tell me about the invoice", but "bill" against
"invoice" is lost on it and "slide 3" is unanswerable in principle. The
lexical half is the other signal: an inverted index over the same chunks
`VectorMemory` stores, keyed by tokens, scored BM25 (k1=1.2, b=0.75), with
field boosts (subject and filename x3, sender name x2, the context header
x1), an exact-phrase bonus and typo tolerance over the token vocabulary.

It is in-memory for the same reason every other view is: the chunk text is
sealed, so the index is built from decrypted rows at boot and never runs
inside SQL. It rides the single chunk writer — `VectorMemory` notifies it
of committed chunks — so there are never two writers disagreeing. Text
lives in process memory only, never back on disk; a chunk that leaves the
hot set is forgotten here and cold-scanned again if a query asks.

Every public retrieval method is failure-tolerant: a broken query costs
the turn its lexical hits, never the turn.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
from rapidfuzz import process as fuzz_process

from .crypto import Cipher
from .storage import newest_chunk_rows, older_chunk_rows

log = logging.getLogger("aether.memory.lexical")

BM25_K1 = 1.2
BM25_B = 0.75
# the exact-phrase boost: the query found verbatim lifts the chunk
PHRASE_BOOST = 1.5

# the fields one indexed document carries, with their BM25F weights —
# subject and filename carry x3, the sender name x2, the chunk's context
# header x1 and the body itself x1
_FIELDS = ("subject", "sender", "header", "body")
FIELD_WEIGHTS: dict[str, float] = {
    "subject": 3.0,
    "sender": 2.0,
    "header": 1.0,
    "body": 1.0,
}

_QUERY_INFINITE = re.compile(r"[\w']+", re.UNICODE)
# phrases stay inside their quotes: "invoice due" must match the pair, not
# each word anywhere
_PHRASE_RE = re.compile(r'"([^"]{2,120})"')
_WHITESPACE = re.compile(r"\s+")

_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


@dataclass(frozen=True)
class LexicalHit:
    """One lexical match: the chunk it matched and the BM25 score."""

    event_id: int
    chunk_id: int
    ordinal: int
    source: str
    kind: str
    filename: str
    location: str
    occurred_at: datetime
    # decrypted chunk text, process memory only — a hit carries its words so
    # the packer can cite them and the verifier can check claims against them
    text: str
    score: float
    # true when the hit added no score of its own — a neighbor chunk pulled
    # in beside a scored one so a citation has its context
    neighbor: bool = False


def fold_text(text: str) -> str:
    """Case and diacritic folding — thE Résumé matches `the resume`.

    NFKD decomposition strips combining marks, so accented text folds to
    plain ascii where the words still look the same. Transliteration of
    native scripts is not attempted here; the router's rewrite step and
    the tokenizer's word-splitting cover the romanized forms people type.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stem(word: str) -> str:
    """A light, deterministic suffix strip — `invoices` finds `invoice`.

    The plural rules are the only subtle part: `-ies` becomes `-y`, `-es`
    strips only after a sibilant (boxes, batches) so `invoices` keeps its
    final `e`, and a single `-s` never strips from `-ss`. Inflections strip
    next, and no stem falls below three characters.
    """
    if word.endswith("ies") and len(word) - 3 >= 3:
        return word[:-3] + "y"
    if (
        word.endswith("es")
        and len(word) - 2 >= 3
        and word[:-2].endswith(("s", "x", "z", "ch", "sh"))
    ):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
        return word[:-1]
    for suffix in ("ization", "ations", "ation", "ings", "ing", "ed"):
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    """Folded, stop-worded, stemmed query tokens — one tokenizer for both
    indexing and asking, so the two sides always mean the same thing."""
    folded = fold_text(text)
    return [
        _stem(tok)
        for tok in _QUERY_INFINITE.findall(folded)
        if tok not in _STOP_WORDS and len(tok) >= 2
    ]


def _split_fields(text: str, filename: str, sender: str) -> dict[str, str]:
    """One chunk's text into its weighted fields.

    The chunk header (`[Mail: subject | ... | from Name <addr> | date]`)
    is split into its parts; whatever is left is the body — identical
    split for the writer of the chunks (vectors.chunk_header) or any
    older row, since the missing parts simply stay empty.
    """
    body = text
    subject = ""
    header = ""
    match = re.match(r"\[([^\]]+)\]", text)
    if match:
        header = match.group(0)
        body = text[match.end() :]
        parts = [part.strip() for part in match.group(1).split("|")]
        for part in parts:
            if ":" in part:
                label, _, value = part.partition(":")
                if label.strip().lower() in ("mail", "doc") and value.strip():
                    subject = value.strip()
            sender_match = re.match(r"from\s+(.*)", part, re.IGNORECASE)
            if sender_match:
                sender = sender_match.group(1) or sender
    return {
        "subject": f"{subject} {filename}".strip(),
        "sender": sender,
        "header": header,
        "body": body,
    }


# The per-field term frequency is packed into one integer per token per
# chunk (8 bits per field): nested dicts per posting cost hundreds of
# megabytes at the hot budget, while one int costs tens.
_FIELD_ORDER = ("body", "header", "sender", "subject")
_FIELD_SHIFT = {name: 8 * index for index, name in enumerate(_FIELD_ORDER)}
_TF_MASK = 0xFF


def _pack_tfs(counts: dict[str, list[int]]) -> dict[str, int]:
    packed: dict[str, int] = {}
    for token, per_field in counts.items():
        value = 0
        for index, name in enumerate(_FIELD_ORDER):
            value |= min(per_field[index], _TF_MASK) << _FIELD_SHIFT[name]
        packed[token] = value
    return packed


class _Doc:
    """One chunk resident in the index, with its field lengths."""

    __slots__ = (
        "event_id",
        "ordinal",
        "chunk_id",
        "source",
        "kind",
        "filename",
        "location",
        "occurred_at",
        "text",
        "tokens",
        "lengths",
        "phrases",
    )

    def __init__(
        self,
        *,
        chunk_id: int,
        event_id: int,
        ordinal: int,
        source: str,
        kind: str,
        filename: str,
        location: str,
        occurred_at: datetime,
        text: str,
        tokens: tuple[str, ...],
        lengths: dict[str, int],
        phrases: set[str],
    ) -> None:
        self.chunk_id = chunk_id
        self.event_id = event_id
        self.ordinal = ordinal
        self.source = source
        self.kind = kind
        self.filename = filename
        self.location = location
        self.occurred_at = occurred_at
        self.text = text
        # the unique tokens, kept only so removal can walk this document's
        # postings — the frequencies themselves live in the shared index
        self.tokens = tokens
        self.lengths = lengths
        self.phrases = phrases

    def weighted_length(self) -> float:
        """The field-boosted document length — what b=0.75 normalizes
        against when the weights differ between fields."""
        return sum(self.lengths[name] * FIELD_WEIGHTS[name] for name in _FIELDS)


class LexicalIndex:
    """The resident inverted index over the chunk text it has seen."""

    def __init__(self, *, hot_chunks: int = 8_000, cold_batch: int = 500) -> None:
        self._hot_chunks = max(16, int(hot_chunks))
        self._cold_batch = max(16, int(cold_batch))
        self._docs: dict[int, _Doc] = {}
        # token -> chunk id -> packed per-field term frequencies
        self._postings: dict[str, dict[int, int]] = {}
        self._lengths: dict[int, float] = {}
        # average length of each field across the resident docs — BM25F's
        # b=0.75 normalizes every field against its own average, not a
        # blended one. Recomputed lazily at query time: the boot rebuild
        # adds thousands of chunks, and recomputing per add is quadratic.
        self._avg_lengths: dict[str, float] = dict.fromkeys(_FIELDS, 1.0)
        self._avg_length = 0.0
        self._avg_dirty = True
        # chunk ids loaded for every event, so `remove_event` can forget
        # without a scan
        self._by_event: dict[int, set[int]] = {}
        self._covers_below: int | None = None  # newest id NOT yet scanned
        self.skipped = 0

    # -- state ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._docs)

    @property
    def exhausted(self) -> bool:
        return self._covers_below is not None and self._covers_below <= 1

    # -- building -----------------------------------------------------------

    async def rebuild(self, pool: asyncpg.Pool, cipher: Cipher) -> int:
        """Load the newest `hot_chunks` chunk rows at boot — decrypted, in
        memory, same pattern as the event index."""
        rows = await newest_chunk_rows(pool, self._hot_chunks)
        loaded = self._ingest_rows(cipher, rows)
        if rows:
            self._covers_below = int(rows[-1]["id"])
        log.info(
            "lexical index rebuilt: %d chunk(s), %d token(s), covered below %s",
            len(self._docs),
            len(self._postings),
            self._covers_below,
        )
        return loaded

    async def grow(self, pool: asyncpg.Pool, cipher: Cipher) -> bool:
        """One bounded batch of older chunks cold-scanned in. True when a
        batch arrived — the caller decides whether to keep going."""
        if (
            self._covers_below is None
            or self._covers_below <= 1
            or len(self._docs) >= self._hot_chunks
        ):
            return False
        rows = await older_chunk_rows(pool, self._covers_below, self._cold_batch)
        if not rows:
            self._covers_below = 1
            return False
        self._ingest_rows(cipher, rows)
        self._covers_below = int(rows[-1]["id"])
        return True

    def _ingest_rows(self, cipher: Cipher, rows: list[asyncpg.Record]) -> int:
        loaded = 0
        for row in rows:
            chunk_id = int(row["id"])
            if chunk_id in self._docs:
                continue
            try:
                text = cipher.decrypt_text(row["text_enc"], aad=f"chunks:text_enc:{chunk_id}")
                meta = cipher.decrypt_json(
                    row["meta_enc"], aad=f"memory_chunks:meta_enc:{chunk_id}"
                )
            except Exception:
                self.skipped += 1
                log.warning("chunk %s undecryptable — left out of the lexical index", chunk_id)
                continue
            self.add_chunk(
                chunk_id=chunk_id,
                event_id=int(row["event_id"]),
                ordinal=int(row.get("ordinal") or 0),
                source=str(meta.get("source", "")),
                kind=str(meta.get("kind", "")),
                filename=str(meta.get("filename", "")),
                location=str(meta.get("location", "")),
                occurred_at=_meta_time(meta.get("occurred_at")),
                text=text,
                sender="",
            )
            loaded += 1
        return loaded

    # -- updating (driven by the chunk writer) ------------------------------

    def add_chunk(
        self,
        *,
        chunk_id: int,
        event_id: int,
        ordinal: int,
        source: str,
        kind: str,
        filename: str,
        location: str,
        occurred_at: datetime,
        text: str,
        sender: str = "",
    ) -> None:
        if chunk_id in self._docs or not text.strip():
            return
        fields = _split_fields(text, filename, sender)
        field_tokens = {name: tokenize(fields[name]) for name in _FIELDS}
        counts: dict[str, list[int]] = {}
        for index, name in enumerate(_FIELD_ORDER):
            for token in field_tokens[name]:
                counts.setdefault(token, [0, 0, 0, 0])[index] += 1
        packed = _pack_tfs(counts)
        lengths = {name: len(field_tokens[name]) for name in _FIELDS}
        phrases = {
            " ".join(_QUERY_INFINITE.findall(fold_text(fields[name])))
            for name in _FIELDS
            if fields[name]
        }
        doc = _Doc(
            chunk_id=chunk_id,
            event_id=event_id,
            ordinal=ordinal,
            source=source,
            kind=kind,
            filename=filename,
            location=location,
            occurred_at=occurred_at,
            text=text,
            tokens=tuple(packed),
            lengths=lengths,
            phrases=phrases,
        )
        self._docs[chunk_id] = doc
        self._by_event.setdefault(event_id, set()).add(chunk_id)
        for token, value in packed.items():
            self._postings.setdefault(token, {})[chunk_id] = value
        self._lengths[chunk_id] = doc.weighted_length() or 1.0
        self._avg_dirty = True
        self._trim()

    def remove_event(self, event_id: int) -> None:
        """Every chunk of one event leaves the index — the noise collapse
        path (`replace_with_summary`) replaces them with one new chunk."""
        for chunk_id in self._by_event.pop(event_id, set()):
            doc = self._docs.pop(chunk_id, None)
            if doc is None:
                continue
            for token in doc.tokens:
                bucket = self._postings.get(token)
                if bucket is not None:
                    bucket.pop(chunk_id, None)
                    if not bucket:
                        del self._postings[token]
            self._lengths.pop(chunk_id, None)
        self._avg_dirty = True

    def _ensure_avg_lengths(self) -> None:
        if not self._avg_dirty:
            return
        self._avg_length = (
            sum(self._lengths.values()) / len(self._lengths) if self._lengths else 1.0
        )
        if self._docs:
            for name in _FIELDS:
                self._avg_lengths[name] = sum(
                    doc.lengths[name] for doc in self._docs.values()
                ) / len(self._docs)
        else:
            self._avg_lengths = dict.fromkeys(_FIELDS, 1.0)
        self._avg_dirty = False

    def _trim(self) -> None:
        """Keep at most `hot_chunks` resident chunks, dropping the oldest
        ids (chunk ids grow with time, so the oldest rows are the oldest
        data). A trimmed chunk comes back by cold scan if a query needs it."""
        if len(self._docs) <= self._hot_chunks:
            return
        victims = sorted(self._docs)[: len(self._docs) - self._hot_chunks]
        for chunk_id in victims:
            doc = self._docs.pop(chunk_id)
            event_bucket = self._by_event.get(doc.event_id)
            if event_bucket is not None:
                event_bucket.discard(chunk_id)
                if not event_bucket:
                    del self._by_event[doc.event_id]
            for token in doc.tokens:
                bucket = self._postings.get(token)
                if bucket is not None:
                    bucket.pop(doc.chunk_id, None)
                    if not bucket:
                        del self._postings[token]
            self._lengths.pop(chunk_id, None)
        self._avg_dirty = True

    # -- retrieval ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
        source: str = "",
        kind: str = "",
        location: str = "",
        filename_stem: str = "",
    ) -> list[LexicalHit]:
        """BM25 over the resident chunks. Returns `limit` hits, best
        first — an empty or stop-word-only query returns nothing."""
        tokens = tokenize(query)
        if not tokens or not self._docs:
            return []
        self._ensure_avg_lengths()
        resolved = self._resolve_query_tokens(query, tokens)
        phrase_bits = [fold_text(p) for p in _PHRASE_RE.findall(query)]
        full = " ".join(resolved)
        scores: dict[int, float] = {}
        for token, count in Counter(resolved).items():
            bucket = self._postings.get(token)
            if not bucket:
                continue
            idf = math.log(1.0 + (len(self._docs) - len(bucket) + 0.5) / (len(bucket) + 0.5))
            for chunk_id, packed in bucket.items():
                doc = self._docs[chunk_id]
                # BM25F: one score per field, boosted, summed — a token hit
                # in the subject or a filename outranks the same hit in a body
                score = 0.0
                for name in _FIELD_ORDER:
                    tf = float((packed >> _FIELD_SHIFT[name]) & _TF_MASK)
                    if tf <= 0.0:
                        continue
                    dl = doc.lengths[name] or 1.0
                    avg = self._avg_lengths.get(name) or 1.0
                    score += FIELD_WEIGHTS[name] * (
                        (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avg))
                    )
                if score > 0.0:
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * count * score
        if not scores:
            return []
        # exact-phrase boost: the whole query arriving as a phrase the
        # document actually contains, or the user's quoted phrase found as
        # written, lifts the chunk above bag-of-words competition
        for chunk_id in scores:
            doc = self._docs[chunk_id]
            if _phrase_in(doc.phrases, phrase_bits, full):
                scores[chunk_id] *= PHRASE_BOOST
        hits = self._to_hits(
            scores, source=source, kind=kind, location=location, filename_stem=filename_stem
        )
        hits.sort(key=lambda hit: (-hit.score, hit.event_id, hit.ordinal))
        return hits[:limit]

    def _resolve_query_tokens(self, query: str, tokens: list[str]) -> list[str]:
        """Typo tolerance: an unknown token of 5+ characters matches the
        closest vocabulary token within edit distance 2 (rapidfuzz).
        Per-token, cached inside this call only — one paragraph, no global
        cache, so a bad guess can never harden."""
        vocab_list: list[str] | None = None
        out: list[str] = []
        for token in tokens:
            if token in self._postings or len(token) < 5:
                out.append(token)
                continue
            if vocab_list is None:
                vocab_list = list(self._postings)
            best = fuzz_process.extractOne(token, vocab_list, score_cutoff=92)
            out.append(best[0] if best is not None else token)
        return out

    def _to_hits(
        self,
        scores: dict[int, float],
        *,
        source: str,
        kind: str,
        location: str,
        filename_stem: str,
    ) -> list[LexicalHit]:
        hits = []
        for chunk_id, score in scores.items():
            doc = self._docs[chunk_id]
            if source and doc.source != source:
                continue
            if kind and doc.kind != kind:
                continue
            if location and location.lower() not in (doc.location or "").lower():
                continue
            stem = filename_stem.lower()
            if stem and stem not in (doc.filename or "").lower():
                continue
            hits.append(
                LexicalHit(
                    event_id=doc.event_id,
                    chunk_id=chunk_id,
                    ordinal=doc.ordinal,
                    source=doc.source,
                    kind=doc.kind,
                    filename=doc.filename,
                    location=doc.location,
                    occurred_at=doc.occurred_at,
                    text=doc.text,
                    score=float(score),
                )
            )
        return hits

    def neighbors(self, hit: LexicalHit) -> list[LexicalHit]:
        """The chunks immediately before and after one hit, same event and
        document order — a citation that names slide 3 should be able to
        show what came next. Only resident chunks are returned: the hot
        budget, never a surprise cold scan."""
        found: list[LexicalHit] = []
        by_ordinal = sorted(
            (doc for doc in self._docs.values() if doc.event_id == hit.event_id),
            key=lambda doc: (doc.ordinal, doc.chunk_id),
        )
        for doc in by_ordinal:
            if abs(doc.ordinal - hit.ordinal) == 1:
                found.append(
                    LexicalHit(
                        event_id=doc.event_id,
                        chunk_id=doc.chunk_id,
                        ordinal=doc.ordinal,
                        source=doc.source,
                        kind=doc.kind,
                        filename=doc.filename,
                        location=doc.location,
                        occurred_at=doc.occurred_at,
                        text=doc.text,
                        score=0.0,
                        neighbor=True,
                    )
                )
        return found


def _phrase_in(doc_phrases: set[str], quoted: list[str], full_query: str) -> bool:
    if quoted:
        # the user's quotes must be found as written
        return any(
            phrase in doc_phrases or any(phrase in p for p in doc_phrases) for phrase in quoted
        )
    return bool(doc_phrases) and full_query in doc_phrases


def _meta_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)
