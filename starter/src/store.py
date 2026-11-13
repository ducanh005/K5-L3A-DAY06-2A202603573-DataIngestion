"""PostgreSQL + pgvector store with per-strategy indexes, idempotent upsert, and SQL filtering."""
from __future__ import annotations

import math
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Chunk

STRATEGIES = ("A", "B", "C")
ALLOWED_FILTER_KEYS = frozenset({"document_id", "source", "page", "year", "document_type", "section"})
_VALID_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VECTOR_DIM_RE = re.compile(r"vector\((\d+)\)")


def _format_vector_literal(vector: list[float], dimensions: int) -> str:
    if len(vector) != dimensions:
        raise ValueError(f"Vector dimension mismatch: expected {dimensions}, got {len(vector)}.")
    parts: list[str] = []
    for val in vector:
        fval = float(val)
        if not math.isfinite(fval):
            raise ValueError("Embedding vector contains non-finite value.")
        parts.append(repr(fval))
    return "[" + ",".join(parts) + "]"


class PgVectorStore:
    """Stores and searches strategy-isolated chunk vectors in PostgreSQL + pgvector."""

    def __init__(
        self,
        database_url: str,
        dimensions: int,
        *,
        table_prefix: str = "day6_chunks",
    ) -> None:
        if not database_url or not database_url.strip():
            raise ValueError("database_url must be a non-empty PostgreSQL connection string.")
        if not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("dimensions must be a positive integer.")
        if not _VALID_PREFIX_RE.match(table_prefix):
            raise ValueError(f"Invalid table_prefix '{table_prefix}'. Use lowercase letters/digits/underscore.")

        self.database_url = database_url.strip()
        self.dimensions = dimensions
        self.table_prefix = table_prefix
        self._tables = {s: f"{table_prefix}_{s.lower()}" for s in STRATEGIES}
        self.last_upsert_stats: dict[str, int] = {"upserted": 0, "deleted_obsolete": 0}

    def _table_for(self, strategy: str) -> str:
        key = strategy.strip().upper()
        if key not in self._tables:
            raise ValueError(f"Unknown strategy '{strategy}'. Expected one of {STRATEGIES}.")
        return self._tables[key]

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def create_schema(self) -> None:
        """Create pgvector extension, strategy tables, and B-tree/HNSW indexes."""
        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            for strategy in STRATEGIES:
                table = self._tables[strategy]
                existing_dim = self._existing_embedding_dim(conn, table)
                if existing_dim is not None and existing_dim != self.dimensions:
                    raise RuntimeError(
                        f"Existing table '{table}' has vector({existing_dim}), "
                        f"which conflicts with configured dimensions={self.dimensions}."
                    )
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        chunk_id TEXT PRIMARY KEY,
                        document_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        page INTEGER NOT NULL,
                        year INTEGER,
                        document_type TEXT,
                        section TEXT,
                        text TEXT NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding vector({self.dimensions}) NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_doc_page ON {table} (document_id, page);")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_year ON {table} (year);")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_doc_type ON {table} (document_type);")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_section ON {table} (section);")
                if self.dimensions <= 2000:
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_hnsw_cos "
                        f"ON {table} USING hnsw (embedding vector_cosine_ops);"
                    )
            conn.commit()

    def _existing_embedding_dim(self, conn: psycopg.Connection[Any], table: str) -> int | None:
        row = conn.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod) AS col_type
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = current_schema()
              AND c.relname = %s
              AND a.attname = 'embedding'
              AND NOT a.attisdropped;
            """,
            (table,),
        ).fetchone()
        if not row or not row.get("col_type"):
            return None
        match = _VECTOR_DIM_RE.search(str(row["col_type"]))
        return int(match.group(1)) if match else None

    def upsert(
        self,
        strategy: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        *,
        prune_missing_documents: bool = False,
    ) -> None:
        """Upsert chunks + vectors idempotently and prune obsolete chunks for updated documents."""
        table = self._table_for(strategy)
        if len(chunks) != len(vectors):
            raise ValueError(f"Chunk/vector count mismatch: {len(chunks)} chunks vs {len(vectors)} vectors.")
        if not chunks:
            self.last_upsert_stats = {"upserted": 0, "deleted_obsolete": 0}
            return

        seen_ids: set[str] = set()
        rows: list[tuple[Any, ...]] = []
        doc_ids: set[str] = set()
        chunk_ids: list[str] = []
        for chunk, vec in zip(chunks, vectors):
            if chunk.chunk_id in seen_ids:
                raise ValueError(f"Duplicate chunk_id in upsert batch: '{chunk.chunk_id}'.")
            seen_ids.add(chunk.chunk_id)
            meta = dict(chunk.metadata or {})
            vec_lit = _format_vector_literal(vec, self.dimensions)
            doc_ids.add(chunk.document_id)
            chunk_ids.append(chunk.chunk_id)
            rows.append(
                (
                    chunk.chunk_id,
                    chunk.document_id,
                    chunk.source,
                    int(chunk.page),
                    meta.get("year"),
                    meta.get("document_type"),
                    meta.get("section"),
                    chunk.text,
                    Jsonb(meta),
                    vec_lit,
                )
            )

        deleted_count = 0
        with self._connect() as conn:
            with conn.transaction():
                if prune_missing_documents:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE NOT (chunk_id = ANY(%s));",
                        (chunk_ids,),
                    )
                else:
                    cur = conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE document_id = ANY(%s)
                          AND NOT (chunk_id = ANY(%s));
                        """,
                        (sorted(doc_ids), chunk_ids),
                    )
                deleted_count = cur.rowcount or 0

                with conn.cursor() as cur_up:
                    cur_up.executemany(
                        f"""
                        INSERT INTO {table} (
                            chunk_id, document_id, source, page,
                            year, document_type, section, text, metadata, embedding
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s::vector
                        )
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            document_id = EXCLUDED.document_id,
                            source = EXCLUDED.source,
                            page = EXCLUDED.page,
                            year = EXCLUDED.year,
                            document_type = EXCLUDED.document_type,
                            section = EXCLUDED.section,
                            text = EXCLUDED.text,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding,
                            updated_at = NOW();
                        """,
                        rows,
                    )
        self.last_upsert_stats = {"upserted": len(rows), "deleted_obsolete": deleted_count}

    def count(self, strategy: str) -> int:
        """Return total stored chunks for the given strategy."""
        table = self._table_for(strategy)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table};").fetchone()
        return int(row["n"]) if row else 0

    def drop_tables(self) -> None:
        """Drop all strategy tables under this store's table_prefix (intended for isolated tests)."""
        with self._connect() as conn:
            with conn.transaction():
                for strategy in STRATEGIES:
                    conn.execute(f"DROP TABLE IF EXISTS {self._tables[strategy]};")

    def search(
        self,
        strategy: str,
        query_vector: list[float],
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Filter permitted metadata in SQL, rank by pgvector cosine distance, and return top-k hits."""
        table = self._table_for(strategy)
        if not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k must be a positive integer.")

        q_lit = _format_vector_literal(query_vector, self.dimensions)
        where_clauses, filter_params = self._build_filter_sql(filters)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params: list[Any] = [q_lit, q_lit, *filter_params, q_lit, top_k]

        sql = f"""
            SELECT
                chunk_id,
                document_id,
                source,
                page,
                text,
                metadata,
                (embedding <=> %s::vector) AS cosine_distance,
                1.0 - (embedding <=> %s::vector) AS score
            FROM {table}
            {where_sql}
            ORDER BY embedding <=> %s::vector ASC, chunk_id ASC
            LIMIT %s;
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "source": row["source"],
                "page": int(row["page"]),
                "text": row["text"],
                "score": float(row["score"]),
                "distance": float(row["cosine_distance"]),
                "metadata": dict(row["metadata"] or {}),
            }
            for row in rows
        ]

    @staticmethod
    def _build_filter_sql(filters: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
        if not filters:
            return [], []
        unknown = set(filters) - ALLOWED_FILTER_KEYS
        if unknown:
            raise ValueError(f"Unsupported metadata filter keys: {sorted(unknown)}")

        clauses: list[str] = []
        params: list[Any] = []
        for key in sorted(filters):
            value = filters[key]
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                seq = list(value)
                if not seq:
                    raise ValueError(f"Filter list for '{key}' must not be empty.")
                clauses.append(f"{key} = ANY(%s)")
                params.append(seq)
            elif isinstance(value, (str, int, float, bool)):
                clauses.append(f"{key} = %s")
                params.append(value)
            else:
                raise ValueError(f"Unsupported filter value type for '{key}': {type(value).__name__}")
        return clauses, params
