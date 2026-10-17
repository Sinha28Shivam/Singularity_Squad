"""Policy chunk storage (`policy_chunks`): chunk text, page/section metadata and optional vectors.

Each chunk carries denormalized ``document_type``, ``vendor_key`` and ``document_status`` so retrieval can
apply access/applicability filters before ranking. ``set_document_status`` keeps them in sync.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from pymongo import ASCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError
from app.policy_rag.schemas import PolicyChunk
from app.repositories.ticket_repository import SCHEMA_VERSION, _resolve_db


def _coll(db: Optional[Database]):
    return _resolve_db(db).policy_chunks


def replace_chunks(document_id: str, chunks: Iterable[PolicyChunk | dict], *,
                   embeddings: Optional[list[Optional[list[float]]]] = None,
                   document_type: Optional[str] = None, vendor_key: Optional[str] = None,
                   document_status: str = "indexing", db: Optional[Database] = None) -> int:
    """Delete then insert all chunks of a document (used by ingest and reprocess). Returns count."""
    rows: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        data = chunk.model_dump() if isinstance(chunk, PolicyChunk) else dict(chunk)
        data.pop("_id", None)
        if data.get("document_id") != document_id:
            raise ValueError("chunk belongs to a different document")
        vector = embeddings[i] if embeddings is not None and i < len(embeddings) else None
        if vector is not None:
            data["embedding"] = [float(x) for x in vector]
        else:
            data["embedding_model"] = None
        data.update({"document_type": document_type, "vendor_key": vendor_key,
                     "document_status": document_status, "schema_version": SCHEMA_VERSION})
        rows.append(data)
    coll = _coll(db)
    try:
        coll.delete_many({"document_id": document_id})
        if rows:
            coll.insert_many(rows)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return len(rows)


def set_document_status(document_id: str, status: str, *, db: Optional[Database] = None) -> int:
    try:
        res = _coll(db).update_many({"document_id": document_id}, {"$set": {"document_status": status}})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return int(res.modified_count)


def delete_chunks(document_id: str, *, db: Optional[Database] = None) -> int:
    try:
        return int(_coll(db).delete_many({"document_id": document_id}).deleted_count)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def list_chunks(*, document_ids: Optional[Iterable[str]] = None, document_status: Optional[str] = None,
                document_statuses: Optional[Iterable[str]] = None, include_embeddings: bool = True,
                db: Optional[Database] = None) -> list[dict]:
    """Chunks ordered by (document_id, chunk_index)."""
    query: dict[str, Any] = {}
    if document_ids is not None:
        query["document_id"] = {"$in": list(document_ids)}
    if document_status:
        query["document_status"] = document_status
    elif document_statuses is not None:
        query["document_status"] = {"$in": list(document_statuses)}
    projection: dict[str, int] = {"_id": 0}
    if not include_embeddings:
        projection["embedding"] = 0
    try:
        return list(_coll(db).find(query, projection).sort([("document_id", ASCENDING), ("chunk_index", ASCENDING)]))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_chunk(chunk_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _coll(db).find_one({"chunk_id": chunk_id}, {"_id": 0})
    except PyMongoError as exc:
        raise PersistenceError() from exc


def count_chunks(document_id: str, *, db: Optional[Database] = None) -> int:
    try:
        return int(_coll(db).count_documents({"document_id": document_id}))
    except PyMongoError as exc:
        raise PersistenceError() from exc
