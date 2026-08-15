"""Fixed, read-only QueryGaP repository used only by the MCP service.

There is deliberately no generic query/SQL method here. Every exported function
has a bounded input contract and executes a fixed statement through the MCP-only
database boundary.
"""

from __future__ import annotations

import math
import re
from typing import Any, Sequence

from .database import read_cursor


_FULL_STUDY_ID_RE = re.compile(r"^phs\d{6,}\.v\d+\.p\d+$", re.IGNORECASE)
_METHODS = {"keyword", "semantic", "hybrid"}
_ALIASES = {
    "fhs": "Framingham Cohort",
    "framingham heart study": "Framingham Cohort",
    "whi": "Women's Health Initiative",
}


def _exact_study_id(value: str) -> str:
    if not isinstance(value, str) or not _FULL_STUDY_ID_RE.fullmatch(value.strip()):
        raise ValueError("A full versioned dbGaP study accession is required.")
    return value.strip().lower()


def _bounded_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    query = " ".join(value.split()).strip()
    if not query or len(query) > 500:
        raise ValueError("query must contain between 1 and 500 characters")
    return query


def _bounded_limit(value: int, maximum: int = 20) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _method(value: str) -> str:
    if value not in _METHODS:
        raise ValueError("method must be keyword, semantic, or hybrid")
    return value


def _vector(value: Sequence[float] | None) -> str:
    if value is None or isinstance(value, (str, bytes)):
        raise ValueError("A query embedding is required.")
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("The query embedding is invalid.") from None
    if len(numbers) != 1536 or not all(math.isfinite(item) for item in numbers):
        raise ValueError("The query embedding must contain 1536 finite values.")
    # A validated textual vector avoids registering pgvector's connection adapter,
    # which would otherwise perform type-discovery outside our guarded transaction.
    return "[" + ",".join(repr(item) for item in numbers) + "]"


def _rows(cursor: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _normalize_scores(rows: list[dict[str, Any]], key: str = "score") -> None:
    if not rows:
        return
    scores = [float(row.get(key) or 0.0) for row in rows]
    low, high = min(scores), max(scores)
    for row, score in zip(rows, scores):
        row[key] = 1.0 if low == high else (score - low) / (high - low)


def normalize_study_query_alias(query: str) -> str:
    normalized = " ".join(str(query or "").strip().lower().split())
    return _ALIASES.get(normalized, query)


def resolve_study_accession(study_id: str) -> dict[str, Any] | None:
    """Resolve only an exact versioned accession known to a catalog table."""
    study_id = _exact_study_id(study_id)
    base_study_id = study_id.split(".", 1)[0]
    with read_cursor() as cursor:
        cursor.execute(
            """
            WITH exact_presence AS (
                SELECT
                    EXISTS (SELECT 1 FROM studies WHERE study_id = %s)
                    OR EXISTS (SELECT 1 FROM datasets WHERE study_id = %s)
                    OR EXISTS (SELECT 1 FROM variables WHERE study_id = %s)
                    AS present
            )
            SELECT
                %s::text AS study_id,
                COALESCE(exact.name, base.name) AS name,
                (SELECT COUNT(*)::int FROM datasets WHERE study_id = %s)
                    AS dataset_count,
                (SELECT COUNT(*)::int FROM variables WHERE study_id = %s)
                    AS variable_count
            FROM exact_presence presence
            LEFT JOIN studies exact ON exact.study_id = %s
            LEFT JOIN studies base ON base.study_id = %s
            WHERE presence.present
            """,
            (
                study_id,
                study_id,
                study_id,
                study_id,
                study_id,
                study_id,
                study_id,
                base_study_id,
            ),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_study_metadata(study_id: str) -> dict[str, Any] | None:
    """Return exact metadata or a clearly labelled base-accession fallback.

    The fallback is considered only after the requested version is proven to
    exist in an exact study, dataset, or variable row.
    """
    study_id = _exact_study_id(study_id)
    base_study_id = study_id.split(".", 1)[0]
    with read_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                selected.metadata,
                CASE WHEN selected.study_id = %s THEN 'exact' ELSE 'base' END
                    AS metadata_scope,
                selected.study_id AS metadata_source_study_id
            FROM studies selected
            WHERE selected.study_id IN (%s, %s)
              AND selected.metadata IS NOT NULL
              AND (
                    EXISTS (SELECT 1 FROM studies WHERE study_id = %s)
                 OR EXISTS (SELECT 1 FROM datasets WHERE study_id = %s)
                 OR EXISTS (SELECT 1 FROM variables WHERE study_id = %s)
              )
            ORDER BY (selected.study_id = %s) DESC
            LIMIT 1
            """,
            (
                study_id,
                study_id,
                base_study_id,
                study_id,
                study_id,
                study_id,
                study_id,
            ),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def search_studies(
    query: str,
    query_embedding: Sequence[float] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find versioned accessions by exact/base study metadata and coverage."""
    del query_embedding  # Study resolution is intentionally lexical in MCP v0.
    query = _bounded_query(query)
    limit = _bounded_limit(limit)
    starts_pattern = f"{query}%"
    like_pattern = f"%{query}%"
    with read_cursor() as cursor:
        cursor.execute(
            """
            WITH accessions AS (
                SELECT study_id FROM studies
                WHERE study_id ~ '^phs[0-9]{6,}[.]v[0-9]+[.]p[0-9]+$'
                UNION
                SELECT study_id FROM datasets
                WHERE study_id ~ '^phs[0-9]{6,}[.]v[0-9]+[.]p[0-9]+$'
                UNION
                SELECT study_id FROM variables
                WHERE study_id ~ '^phs[0-9]{6,}[.]v[0-9]+[.]p[0-9]+$'
            ),
            dataset_counts AS (
                SELECT study_id, COUNT(*)::int AS dataset_count
                FROM datasets GROUP BY study_id
            ),
            variable_counts AS (
                SELECT study_id, COUNT(*)::int AS variable_count
                FROM variables GROUP BY study_id
            )
            SELECT
                a.study_id,
                COALESCE(exact.name, base.name) AS name,
                COALESCE(dc.dataset_count, 0) AS dataset_count,
                COALESCE(vc.variable_count, 0) AS variable_count,
                CASE
                    WHEN lower(btrim(COALESCE(exact.name, base.name, '')))
                         = lower(btrim(%s)) THEN 4
                    WHEN COALESCE(exact.name, base.name, '') ILIKE %s THEN 3
                    WHEN COALESCE(exact.name, base.name, '') ILIKE %s THEN 2
                    ELSE 1
                END AS match_rank
            FROM accessions a
            LEFT JOIN studies exact ON exact.study_id = a.study_id
            LEFT JOIN studies base ON base.study_id = split_part(a.study_id, '.', 1)
            LEFT JOIN dataset_counts dc ON dc.study_id = a.study_id
            LEFT JOIN variable_counts vc ON vc.study_id = a.study_id
            WHERE
                COALESCE(exact.name, base.name, '') ILIKE %s
                OR to_tsvector(
                    'simple',
                    COALESCE(exact.name, base.name, '') || ' ' ||
                    COALESCE(exact.metadata, base.metadata, '{}'::jsonb)::text
                ) @@ plainto_tsquery('simple', %s)
            ORDER BY match_rank DESC, variable_count DESC, dataset_count DESC, a.study_id
            LIMIT %s
            """,
            (query, starts_pattern, like_pattern, like_pattern, query, limit),
        )
        return _rows(cursor)


def search_variables_flexible(
    query: str,
    query_embedding: Sequence[float] | None = None,
    method: str = "hybrid",
    study_id: str | None = None,
    limit: int = 20,
    keyword_weight: float = 0.3,
    semantic_weight: float = 0.7,
    include_stats: bool = False,
) -> list[dict[str, Any]]:
    query = _bounded_query(query)
    method = _method(method)
    study_id = _exact_study_id(study_id or "")
    limit = _bounded_limit(limit)
    stats_select = """
        , vr.stat_n, vr.stat_mean, vr.stat_median, vr.stat_sd, vr.stat_min, vr.stat_max
    """ if include_stats else ""
    stats_join = """
        LEFT JOIN variable_reports vr
          ON vr.variable_id = v.variable_id
         AND vr.study_id = v.study_id
    """ if include_stats else ""
    vector = _vector(query_embedding) if method != "keyword" else None

    with read_cursor() as cursor:
        if method == "keyword":
            cursor.execute(
                f"""
                SELECT
                    v.variable_id, v.name, v.description, v.dataset_id, v.study_id,
                    ts_rank(v.search_tsv, websearch_to_tsquery('english', %s)) AS score
                    {stats_select}
                FROM variables v
                {stats_join}
                WHERE v.study_id = %s
                  AND v.search_tsv @@ websearch_to_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, study_id, query, limit),
            )
            rows = _rows(cursor)
            _normalize_scores(rows)
            return rows

        if method == "semantic":
            cursor.execute(
                f"""
                SELECT
                    v.variable_id, v.name, v.description, v.dataset_id, v.study_id,
                    1 - (v.embedding <=> %s::vector) AS score
                    {stats_select}
                FROM variables v
                {stats_join}
                WHERE v.study_id = %s AND v.embedding IS NOT NULL
                ORDER BY v.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, study_id, vector, limit),
            )
            return _rows(cursor)

        semantic_limit = max(limit * 5, 100)
        cursor.execute(
            f"""
            WITH keyword_results AS (
                SELECT
                    v.variable_id,
                    ts_rank(v.search_tsv, websearch_to_tsquery('english', %s))
                        AS keyword_score
                FROM variables v
                WHERE v.study_id = %s
                  AND v.search_tsv @@ websearch_to_tsquery('english', %s)
            ),
            semantic_results AS (
                SELECT
                    v.variable_id,
                    1 - (v.embedding <=> %s::vector) AS semantic_score
                FROM variables v
                WHERE v.study_id = %s AND v.embedding IS NOT NULL
                ORDER BY v.embedding <=> %s::vector
                LIMIT %s
            ),
            keyword_normalized AS (
                SELECT
                    variable_id,
                    CASE WHEN MAX(keyword_score) OVER () = MIN(keyword_score) OVER ()
                         THEN 1.0
                         ELSE (keyword_score - MIN(keyword_score) OVER ()) /
                              NULLIF(MAX(keyword_score) OVER () - MIN(keyword_score) OVER (), 0)
                    END AS keyword_score
                FROM keyword_results
            ),
            combined AS (
                SELECT
                    COALESCE(k.variable_id, s.variable_id) AS variable_id,
                    COALESCE(k.keyword_score, 0) AS keyword_score,
                    COALESCE(s.semantic_score, 0) AS semantic_score
                FROM keyword_normalized k
                FULL OUTER JOIN semantic_results s ON s.variable_id = k.variable_id
            )
            SELECT
                v.variable_id, v.name, v.description, v.dataset_id, v.study_id,
                c.keyword_score, c.semantic_score,
                (%s * c.keyword_score + %s * c.semantic_score) AS score
                {stats_select}
            FROM combined c
            JOIN variables v
              ON v.variable_id = c.variable_id AND v.study_id = %s
            {stats_join}
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                query,
                study_id,
                query,
                vector,
                study_id,
                vector,
                semantic_limit,
                float(keyword_weight),
                float(semantic_weight),
                study_id,
                limit,
            ),
        )
        return _rows(cursor)


def search_datasets_flexible(
    query: str,
    query_embedding: Sequence[float] | None = None,
    method: str = "hybrid",
    study_id: str | None = None,
    limit: int = 20,
    keyword_weight: float = 0.3,
    semantic_weight: float = 0.7,
) -> list[dict[str, Any]]:
    query = _bounded_query(query)
    method = _method(method)
    study_id = _exact_study_id(study_id or "")
    limit = _bounded_limit(limit)
    vector = _vector(query_embedding) if method != "keyword" else None
    with read_cursor() as cursor:
        if method == "keyword":
            cursor.execute(
                """
                SELECT d.dataset_id, d.name, d.description, d.study_id,
                       ts_rank(d.search_tsv, websearch_to_tsquery('english', %s)) AS score
                FROM datasets d
                WHERE d.study_id = %s
                  AND d.search_tsv @@ websearch_to_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, study_id, query, limit),
            )
            rows = _rows(cursor)
            _normalize_scores(rows)
            return rows

        if method == "semantic":
            cursor.execute(
                """
                SELECT d.dataset_id, d.name, d.description, d.study_id,
                       1 - (d.embedding <=> %s::vector) AS score
                FROM datasets d
                WHERE d.study_id = %s AND d.embedding IS NOT NULL
                ORDER BY d.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, study_id, vector, limit),
            )
            return _rows(cursor)

        semantic_limit = max(limit * 5, 50)
        cursor.execute(
            """
            WITH keyword_results AS (
                SELECT d.dataset_id,
                       ts_rank(d.search_tsv, websearch_to_tsquery('english', %s)) AS keyword_score
                FROM datasets d
                WHERE d.study_id = %s
                  AND d.search_tsv @@ websearch_to_tsquery('english', %s)
            ),
            semantic_results AS (
                SELECT d.dataset_id,
                       1 - (d.embedding <=> %s::vector) AS semantic_score
                FROM datasets d
                WHERE d.study_id = %s AND d.embedding IS NOT NULL
                ORDER BY d.embedding <=> %s::vector
                LIMIT %s
            ),
            keyword_normalized AS (
                SELECT dataset_id,
                       CASE WHEN MAX(keyword_score) OVER () = MIN(keyword_score) OVER ()
                            THEN 1.0
                            ELSE (keyword_score - MIN(keyword_score) OVER ()) /
                                 NULLIF(MAX(keyword_score) OVER () - MIN(keyword_score) OVER (), 0)
                       END AS keyword_score
                FROM keyword_results
            ),
            combined AS (
                SELECT COALESCE(k.dataset_id, s.dataset_id) AS dataset_id,
                       COALESCE(k.keyword_score, 0) AS keyword_score,
                       COALESCE(s.semantic_score, 0) AS semantic_score
                FROM keyword_normalized k
                FULL OUTER JOIN semantic_results s ON s.dataset_id = k.dataset_id
            )
            SELECT d.dataset_id, d.name, d.description, d.study_id,
                   c.keyword_score, c.semantic_score,
                   (%s * c.keyword_score + %s * c.semantic_score) AS score
            FROM combined c
            JOIN datasets d ON d.dataset_id = c.dataset_id AND d.study_id = %s
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                query,
                study_id,
                query,
                vector,
                study_id,
                vector,
                semantic_limit,
                float(keyword_weight),
                float(semantic_weight),
                study_id,
                limit,
            ),
        )
        return _rows(cursor)


def search_documents_flexible(
    query: str,
    query_embedding: Sequence[float] | None = None,
    method: str = "hybrid",
    study_id: str | None = None,
    limit: int = 15,
    keyword_weight: float = 0.3,
    semantic_weight: float = 0.7,
) -> list[dict[str, Any]]:
    query = _bounded_query(query)
    method = _method(method)
    study_id = _exact_study_id(study_id or "")
    limit = _bounded_limit(limit)
    pattern = f"%{query}%"
    vector = _vector(query_embedding) if method != "keyword" else None
    with read_cursor() as cursor:
        if method == "keyword":
            cursor.execute(
                """
                SELECT d.document_id, d.name, d.url, d.document_type, d.study_id,
                       1.0 AS score
                FROM documents d
                WHERE d.study_id = %s AND d.name ILIKE %s
                ORDER BY d.name
                LIMIT %s
                """,
                (study_id, pattern, limit),
            )
            return _rows(cursor)

        if method == "semantic":
            cursor.execute(
                """
                SELECT d.document_id, d.name, d.url, d.document_type, d.study_id,
                       1 - (d.embedding <=> %s::vector) AS score
                FROM documents d
                WHERE d.study_id = %s AND d.embedding IS NOT NULL
                ORDER BY d.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, study_id, vector, limit),
            )
            return _rows(cursor)

        semantic_limit = max(limit * 5, 50)
        cursor.execute(
            """
            WITH keyword_results AS (
                SELECT d.document_id, 1.0 AS keyword_score
                FROM documents d
                WHERE d.study_id = %s AND d.name ILIKE %s
            ),
            semantic_results AS (
                SELECT d.document_id,
                       1 - (d.embedding <=> %s::vector) AS semantic_score
                FROM documents d
                WHERE d.study_id = %s AND d.embedding IS NOT NULL
                ORDER BY d.embedding <=> %s::vector
                LIMIT %s
            ),
            combined AS (
                SELECT COALESCE(k.document_id, s.document_id) AS document_id,
                       COALESCE(k.keyword_score, 0) AS keyword_score,
                       COALESCE(s.semantic_score, 0) AS semantic_score
                FROM keyword_results k
                FULL OUTER JOIN semantic_results s ON s.document_id = k.document_id
            )
            SELECT d.document_id, d.name, d.url, d.document_type, d.study_id,
                   c.keyword_score, c.semantic_score,
                   (%s * c.keyword_score + %s * c.semantic_score) AS score
            FROM combined c
            JOIN documents d
              ON d.document_id = c.document_id AND d.study_id = %s
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                study_id,
                pattern,
                vector,
                study_id,
                vector,
                semantic_limit,
                float(keyword_weight),
                float(semantic_weight),
                study_id,
                limit,
            ),
        )
        return _rows(cursor)


def search_ukb_fields_keyword(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = _bounded_query(query)
    limit = _bounded_limit(limit)
    with read_cursor() as cursor:
        cursor.execute(
            """
            WITH base AS (
                SELECT
                    u.field_id, u.title, u.notes, u.category_path, u.aliases,
                    u.source_url,
                    ts_rank(u.search_tsv, websearch_to_tsquery('simple', %s))
                        AS keyword_score,
                    CASE
                        WHEN CAST(u.field_id AS text) = %s THEN 3.0
                        WHEN COALESCE(u.aliases, '') <> '' AND EXISTS (
                            SELECT 1
                            FROM unnest(string_to_array(u.aliases, '|')) AS a(alias)
                            WHERE lower(a.alias) = lower(%s)
                        ) THEN 2.5
                        ELSE 0.0
                    END AS alias_boost
                FROM ukb_search_docs_fields u
                WHERE u.search_tsv @@ websearch_to_tsquery('simple', %s)
                   OR CAST(u.field_id AS text) = %s
                   OR (COALESCE(u.aliases, '') <> '' AND EXISTS (
                        SELECT 1
                        FROM unnest(string_to_array(u.aliases, '|')) AS a(alias)
                        WHERE lower(a.alias) = lower(%s)
                   ))
            )
            SELECT field_id, title, notes, category_path, aliases, source_url,
                   keyword_score, alias_boost,
                   (keyword_score + alias_boost) AS score
            FROM base
            ORDER BY score DESC, field_id
            LIMIT %s
            """,
            (query, query, query, query, query, query, limit),
        )
        return _rows(cursor)


def search_ukb_fields_hybrid(
    query: str,
    query_embedding: Sequence[float],
    limit: int = 20,
    keyword_weight: float = 0.6,
    semantic_weight: float = 0.4,
) -> list[dict[str, Any]]:
    query = _bounded_query(query)
    limit = _bounded_limit(limit)
    vector = _vector(query_embedding)
    with read_cursor() as cursor:
        cursor.execute(
            """
            WITH keyword_results AS (
                SELECT
                    u.field_id,
                    ts_rank(u.search_tsv, websearch_to_tsquery('simple', %s))
                        AS keyword_score,
                    CASE
                        WHEN CAST(u.field_id AS text) = %s THEN 3.0
                        WHEN COALESCE(u.aliases, '') <> '' AND EXISTS (
                            SELECT 1
                            FROM unnest(string_to_array(u.aliases, '|')) AS a(alias)
                            WHERE lower(a.alias) = lower(%s)
                        ) THEN 2.5
                        ELSE 0.0
                    END AS alias_boost
                FROM ukb_search_docs_fields u
                WHERE u.search_tsv @@ websearch_to_tsquery('simple', %s)
                   OR CAST(u.field_id AS text) = %s
                   OR (COALESCE(u.aliases, '') <> '' AND EXISTS (
                        SELECT 1
                        FROM unnest(string_to_array(u.aliases, '|')) AS a(alias)
                        WHERE lower(a.alias) = lower(%s)
                   ))
            ),
            semantic_results AS (
                SELECT u.field_id,
                       1 - (u.embedding <=> %s::vector) AS semantic_score
                FROM ukb_search_docs_fields u
                WHERE u.embedding IS NOT NULL
                ORDER BY u.embedding <=> %s::vector
                LIMIT 100
            ),
            combined AS (
                SELECT COALESCE(k.field_id, s.field_id) AS field_id,
                       COALESCE(k.keyword_score, 0) AS keyword_score,
                       COALESCE(k.alias_boost, 0) AS alias_boost,
                       COALESCE(s.semantic_score, 0) AS semantic_score
                FROM keyword_results k
                FULL OUTER JOIN semantic_results s ON s.field_id = k.field_id
            )
            SELECT u.field_id, u.title, u.notes, u.category_path, u.aliases,
                   u.source_url, c.keyword_score, c.alias_boost, c.semantic_score,
                   (%s * (c.keyword_score + c.alias_boost) +
                    %s * c.semantic_score) AS score
            FROM combined c
            JOIN ukb_search_docs_fields u ON u.field_id = c.field_id
            ORDER BY score DESC, u.field_id
            LIMIT %s
            """,
            (
                query,
                query,
                query,
                query,
                query,
                query,
                vector,
                vector,
                float(keyword_weight),
                float(semantic_weight),
                limit,
            ),
        )
        return _rows(cursor)


def get_ukb_field_details(
    field_id: int,
    notes_max_chars: int = 1200,
) -> dict[str, Any] | None:
    if isinstance(field_id, bool) or not isinstance(field_id, int) or field_id <= 0:
        raise ValueError("field_id must be a positive integer")
    if not isinstance(notes_max_chars, int) or not 1 <= notes_max_chars <= 3000:
        raise ValueError("notes_max_chars must be between 1 and 3000")
    with read_cursor() as cursor:
        cursor.execute(
            """
            SELECT field_id, title, notes, units, value_type, item_type,
                   category_path, encoding_id, encoding_title, source_url,
                   category_url, encoding_url
            FROM ukb_search_docs_fields
            WHERE field_id = %s
            """,
            (field_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    notes = str(result.get("notes") or "").strip()
    if len(notes) > notes_max_chars:
        notes = notes[: max(0, notes_max_chars - 3)].rstrip() + "..."
    result["notes"] = notes or None
    result["field_id"] = int(result["field_id"])
    return result


def get_ukb_field_instance_summaries(field_id: int) -> dict[str, Any] | None:
    if isinstance(field_id, bool) or not isinstance(field_id, int) or field_id <= 0:
        raise ValueError("field_id must be a positive integer")
    with read_cursor() as cursor:
        cursor.execute(
            """
            SELECT u.field_id, u.title, u.source_url, u.instance_id,
                   u.instance_min, u.instance_max, u.instance_descriptions,
                   p.participants AS overall_participants,
                   p.item_count AS overall_item_count,
                   p.defined_instance_min, p.defined_instance_max,
                   p.not_summarised_reason
            FROM ukb_search_docs_fields u
            LEFT JOIN ukb_field_page_data p ON p.field_id = u.field_id
            WHERE u.field_id = %s
            """,
            (field_id,),
        )
        base = cursor.fetchone()
        if not base:
            return None
        cursor.execute(
            """
            SELECT instance, instance_description, participants, item_count,
                   min, median, mean, stddev, max
            FROM ukb_field_instance_stats
            WHERE field_id = %s AND instance >= 0
            ORDER BY instance ASC
            """,
            (field_id,),
        )
        instances = _rows(cursor)

    return {
        "field_id": int(base["field_id"]),
        "title": base.get("title"),
        "source_url": base.get("source_url"),
        "instances_defined": {
            "min": base.get("defined_instance_min", base.get("instance_min")),
            "max": base.get("defined_instance_max", base.get("instance_max")),
        },
        "overall_participants": base.get("overall_participants"),
        "overall_item_count": base.get("overall_item_count"),
        "instance_descriptions": base.get("instance_descriptions"),
        "not_summarised_reason": base.get("not_summarised_reason"),
        "instances": instances,
    }


__all__ = [
    "get_study_metadata",
    "get_ukb_field_details",
    "get_ukb_field_instance_summaries",
    "normalize_study_query_alias",
    "resolve_study_accession",
    "search_datasets_flexible",
    "search_documents_flexible",
    "search_studies",
    "search_ukb_fields_hybrid",
    "search_ukb_fields_keyword",
    "search_variables_flexible",
]
