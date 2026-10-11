"""Chunk index: PyLate/FastPLAID candidate generation, exact MaxSim rerank, and a BM25 evaluation baseline."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Chunk, Contract, session_scope
from berkshire_contracts.extraction.fields import ENUM_LABELS
from berkshire_contracts.retrieval.colbert import LateInteractionEncoder, get_encoder, maxsim_segments

log = logging.getLogger(__name__)

INDEX_VERSION = "index-v1"
SearchMode = Literal["plaid_rerank", "plaid", "exact", "bm25"]


@dataclass
class ChunkMeta:
    chunk_id: str
    file_id: str
    filename: str
    family_id: str | None
    page_no: int
    section: str | None
    text: str
    index_text: str


@dataclass
class SearchHit:
    chunk: ChunkMeta
    score: float
    normalized: float
    rank: int
    # Mean best-match cosine over the query's own tokens only. Full MaxSim includes ~25 [MASK] expansion tokens
    # that match almost anything, so it scores off-topic questions as high as answerable ones.
    relevance: float | None = None


def index_header(contract: Contract) -> str:
    doc_type = ENUM_LABELS.get(contract.document_type or "", contract.document_type or "Document")
    parts = [doc_type]
    if contract.vendor_name:
        parts.append(contract.vendor_name)
    number = contract.contract_number or contract.family_id
    if number:
        parts.append(f"Contract {number}")
    parts.append(Path(contract.filename).stem.replace("_", " ")[:80])
    return " | ".join(parts)


def load_chunk_meta() -> list[ChunkMeta]:
    with session_scope() as s:
        contracts = {c.file_id: c for c in s.query(Contract).all()}
        rows = s.query(Chunk).order_by(Chunk.file_id, Chunk.page_no, Chunk.ordinal).all()
        metas = []
        for row in rows:
            contract = contracts[row.file_id]
            header = index_header(contract)
            section = f"\nSection: {row.section}" if row.section else ""
            metas.append(
                ChunkMeta(
                    chunk_id=row.chunk_id,
                    file_id=row.file_id,
                    filename=contract.filename,
                    family_id=contract.family_id,
                    page_no=row.page_no,
                    section=row.section,
                    text=row.text,
                    index_text=f"{header}{section}\n{row.text}",
                )
            )
        return metas


def _bm25_tokenize(texts: list[str]):
    import bm25s
    import Stemmer

    return bm25s.tokenize(texts, stopwords="en", stemmer=Stemmer.Stemmer("english"), show_progress=False)


def build_index(encoder: LateInteractionEncoder | None = None) -> dict[str, float | int]:
    import bm25s
    from pylate import indexes

    settings = get_settings()
    encoder = encoder or get_encoder()
    metas = load_chunk_meta()
    if not metas:
        raise RuntimeError("no chunks to index; run `contracts chunk` first")
    started = time.perf_counter()
    embeddings = encoder.encode_documents([m.index_text for m in metas], batch_size=32, progress=True)
    keep = [i for i, e in enumerate(embeddings) if e.shape[0] > 0]
    metas = [metas[i] for i in keep]
    embeddings = [embeddings[i] for i in keep]

    out = settings.index_dir
    tmp = out.with_name(out.name + ".building")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    lengths = np.array([e.shape[0] for e in embeddings], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(lengths)]).astype(np.int64)
    np.save(tmp / "embeddings.npy", np.concatenate(embeddings).astype(np.float16))
    np.save(tmp / "offsets.npy", offsets)
    (tmp / "chunks.json").write_text(json.dumps([m.__dict__ for m in metas]))

    plaid = indexes.PLAID(
        index_folder=str(tmp), index_name="plaid", override=True, device="cpu", show_progress=False
    )
    plaid.add_documents(documents_ids=[m.chunk_id for m in metas], documents_embeddings=embeddings)

    bm25 = bm25s.BM25()
    bm25.index(_bm25_tokenize([m.index_text for m in metas]), show_progress=False)
    bm25.save(str(tmp / "bm25"))

    meta = {
        "index_version": INDEX_VERSION,
        "model": encoder.model_name,
        "chunks": len(metas),
        "tokens": int(offsets[-1]),
        "embedding_dim": int(embeddings[0].shape[1]),
        "build_seconds": round(time.perf_counter() - started, 1),
    }
    (tmp / "meta.json").write_text(json.dumps(meta, indent=2))
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    with _index_lock:
        _cached_index.cache_clear()
    return meta


class ContractIndex:
    def __init__(self, index_dir: Path, encoder: LateInteractionEncoder | None = None) -> None:
        self.index_dir = index_dir
        self.meta = json.loads((index_dir / "meta.json").read_text())
        self.chunks = [ChunkMeta(**c) for c in json.loads((index_dir / "chunks.json").read_text())]
        self.position = {c.chunk_id: i for i, c in enumerate(self.chunks)}
        self.embeddings = np.load(index_dir / "embeddings.npy").astype(np.float32)
        self.offsets = np.load(index_dir / "offsets.npy")
        self.by_file: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.chunks):
            self.by_file[c.file_id].append(i)
        self._encoder = encoder
        self.plaid = self._load_plaid()
        self.bm25 = self._load_bm25()

    @property
    def encoder(self) -> LateInteractionEncoder:
        if self._encoder is None:
            self._encoder = get_encoder()
        return self._encoder

    def _load_plaid(self):
        try:
            from pylate import indexes

            return indexes.PLAID(
                index_folder=str(self.index_dir),
                index_name="plaid",
                override=False,
                device="cpu",
                show_progress=False,
            )
        except Exception as exc:
            log.warning("PLAID index unavailable (%s); falling back to exact MaxSim over all chunks", exc)
            return None

    def _load_bm25(self):
        try:
            import bm25s

            return bm25s.BM25.load(str(self.index_dir / "bm25"))
        except Exception as exc:
            log.warning("BM25 index unavailable: %s", exc)
            return None

    def chunk_embeddings(self, i: int) -> np.ndarray:
        return self.embeddings[self.offsets[i] : self.offsets[i + 1]]

    def _exact(self, query: np.ndarray, positions: list[int] | None) -> dict[int, float]:
        if positions is None:
            scores = maxsim_segments(query, self.embeddings, self.offsets)
            return {i: float(s) for i, s in enumerate(scores)}
        return {i: float((self.chunk_embeddings(i) @ query.T).max(axis=0).sum()) for i in positions}

    def _plaid(self, query: np.ndarray, k: int, positions: list[int] | None) -> dict[int, float]:
        subset = [self.chunks[i].chunk_id for i in positions] if positions is not None else None
        results = self.plaid(np.expand_dims(query, 0), k=k, subset=subset)[0]
        return {self.position[r["id"]]: float(r["score"]) for r in results if r["id"] in self.position}

    def _bm25(self, text: str, k: int, positions: list[int] | None) -> dict[int, float]:
        if self.bm25 is None:
            return {}
        n = len(self.chunks)
        docs, scores = self.bm25.retrieve(
            _bm25_tokenize([text]), k=n if positions is not None else min(k, n), show_progress=False
        )
        allowed = set(positions) if positions is not None else None
        out: dict[int, float] = {}
        for idx, score in zip(docs[0], scores[0], strict=True):
            if score <= 0 or (allowed is not None and int(idx) not in allowed):
                continue
            out[int(idx)] = float(score)
            if len(out) >= k:
                break
        return out

    def search(
        self,
        query: str,
        k: int = 8,
        file_ids: set[str] | None = None,
        mode: SearchMode = "plaid_rerank",
        candidates: int = 200,
        max_per_file: int | None = 3,
        max_per_family: int | None = 4,
    ) -> list[SearchHit]:
        positions = None
        if file_ids is not None:
            positions = [i for fid in file_ids for i in self.by_file.get(fid, [])]
            if not positions:
                return []

        content: np.ndarray | None = None
        if mode == "bm25":
            scored = self._bm25(query, max(k * 10, 100), positions)
            norm = dict.fromkeys(scored, 0.0)
        else:
            q = self.encoder.encode_query(query)
            content = q[: min(self.encoder.content_tokens(query), q.shape[0])]
            if mode == "exact" or self.plaid is None:
                scored = self._exact(q, positions)
            elif mode == "plaid":
                scored = self._plaid(q, max(k * 10, 100), positions)
            else:
                pool = self._plaid(q, candidates, positions)
                if positions is not None and len(positions) <= candidates:
                    pool.update(dict.fromkeys(positions, 0.0))
                scored = self._exact(q, list(pool))
            norm = {i: s / q.shape[0] for i, s in scored.items()}

        ranked = sorted(scored, key=lambda i: scored[i], reverse=True)
        hits: list[SearchHit] = []
        per_file: dict[str, int] = defaultdict(int)
        per_family: dict[str, int] = defaultdict(int)
        single_scope = file_ids is not None and len(file_ids) <= 2
        for i in ranked:
            chunk = self.chunks[i]
            family = chunk.family_id or chunk.file_id
            if not single_scope:
                if max_per_file and per_file[chunk.file_id] >= max_per_file:
                    continue
                if max_per_family and per_family[family] >= max_per_family:
                    continue
            per_file[chunk.file_id] += 1
            per_family[family] += 1
            relevance = None
            if content is not None:
                relevance = float((self.chunk_embeddings(i) @ content.T).max(axis=0).mean())
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=scored[i],
                    normalized=norm.get(i, 0.0),
                    rank=len(hits) + 1,
                    relevance=relevance,
                )
            )
            if len(hits) >= k:
                break
        return hits


_index_lock = threading.Lock()


@lru_cache(maxsize=1)
def _cached_index() -> ContractIndex:
    settings = get_settings()
    if not (settings.index_dir / "meta.json").exists():
        raise FileNotFoundError(f"no index at {settings.index_dir}; run `contracts index`")
    return ContractIndex(settings.index_dir)


def load_index() -> ContractIndex:
    """Shared index. The lock keeps a question asked during app warm-up from loading a second copy."""
    with _index_lock:
        return _cached_index()
