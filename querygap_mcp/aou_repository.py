"""Fixed read adapters for the public All of Us metadata catalog."""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from typing import Any

from querygap_mcp.database import tuple_read_cursor as read_cursor


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
MIN_SEMANTIC_SCORE = 0.36
IDENTIFIER_SYSTEM_ALIASES = {
    "aou": "omop_concept_id",
    "omop": "omop_concept_id",
    "omop_concept_id": "omop_concept_id",
    "loinc": "loinc",
    "snomed": "snomedct",
    "snomedct": "snomedct",
    "ppi": "ppi",
    "fitbit": "aou_ui_fitbit_id",
    "aou_ui_fitbit_id": "aou_ui_fitbit_id",
    "icd10cm": "icd10cm",
    "icd9cm": "icd9cm",
    "icd10pcs": "icd10pcs",
    "cpt": "cpt4",
    "cpt4": "cpt4",
    "hcpcs": "hcpcs",
    "rxnorm": "rxnorm",
    "ndc": "ndc",
}
_IDENTIFIER_PREFIX_PATTERN = "|".join(
    sorted(
        (re.escape(value) for value in IDENTIFIER_SYSTEM_ALIASES),
        key=len,
        reverse=True,
    )
)


def _normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"\s+", " ", normalized)


def _parse_identifier_query(query: str) -> tuple[str | None, str | None]:
    value = (query or "").strip()
    match = re.match(
        rf"^({_IDENTIFIER_PREFIX_PATTERN})\s*(?::|\s)\s*(.+)$",
        value,
        flags=re.I,
    )
    if match:
        return (
            IDENTIFIER_SYSTEM_ALIASES[match.group(1).lower()],
            match.group(2).strip(),
        )
    if value.isdigit() and len(value) >= 4:
        return None, value
    if re.fullmatch(r"(?=.*\d)[A-Za-z0-9_.-]{3,}", value):
        return None, value
    return None, None


def _active_snapshot(cursor: Any) -> str:
    cursor.execute(
        """
        SELECT snapshot_key
        FROM aou.active_snapshots
        ORDER BY activated_at_utc DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("No active All of Us metadata snapshot is available")
    return str(row[0])


def _exact_candidates(
    cursor: Any,
    *,
    snapshot_key: str,
    query: str,
) -> list[dict[str, Any]]:
    system, value = _parse_identifier_query(query)
    cursor.execute(
        """
        WITH identifier_items AS (
            SELECT identifier.item_key,
                   'identifier:' || identifier.identifier_system AS match_kind
            FROM aou.identifiers AS identifier
            WHERE identifier.snapshot_key = %s
              AND %s IS NOT NULL
              AND lower(identifier.identifier_value) = lower(%s)
              AND (%s IS NULL OR lower(identifier.identifier_system) = lower(%s))
        ),
        alias_items AS (
            SELECT alias.item_key, 'alias:' || alias.alias_type AS match_kind
            FROM aou.aliases AS alias
            WHERE alias.snapshot_key = %s
              AND alias.normalized_alias = %s
        ),
        exact_items AS MATERIALIZED (
            SELECT * FROM identifier_items
            UNION ALL
            SELECT * FROM alias_items
        ),
        exact_documents AS MATERIALIZED (
            SELECT document.doc_key,
                   document.snapshot_key,
                   document.is_lexically_searchable,
                   exact_items.match_kind
            FROM exact_items
            JOIN aou.search_doc_members AS member
              ON member.item_key = exact_items.item_key
            JOIN aou.search_docs AS document
              ON document.doc_key = member.doc_key
        )
        SELECT DISTINCT ON (doc_key) doc_key, match_kind
        FROM exact_documents
        WHERE snapshot_key = %s
          AND is_lexically_searchable
        ORDER BY doc_key,
                 CASE WHEN match_kind LIKE 'identifier:%%' THEN 0 ELSE 1 END,
                 match_kind
        """,
        (
            snapshot_key,
            value,
            value,
            system,
            system,
            snapshot_key,
            _normalize_alias(query),
            snapshot_key,
        ),
    )
    return [
        {"doc_key": str(row[0]), "match_kind": str(row[1])}
        for row in cursor.fetchall()
    ]


def _keyword_candidates(
    cursor: Any,
    *,
    snapshot_key: str,
    query: str,
    limit: int,
    include_title_fallback: bool,
    fallback_threshold: int,
) -> list[dict[str, Any]]:
    cursor.execute(
        """
        WITH parsed AS (
            SELECT websearch_to_tsquery('simple', %s) AS terms
        ),
        full_text_hits AS MATERIALIZED (
            SELECT document.doc_key,
                   document.snapshot_key,
                   ts_rank_cd(document.search_tsv, parsed.terms, 32) AS lexical_score,
                   similarity(lower(document.title), lower(%s)) AS title_similarity
            FROM aou.search_docs AS document
            CROSS JOIN parsed
            WHERE document.is_lexically_searchable
              AND document.search_tsv @@ parsed.terms
        )
        SELECT doc_key, lexical_score, title_similarity
        FROM full_text_hits
        WHERE snapshot_key = %s
        ORDER BY lexical_score + 0.15 * title_similarity DESC, doc_key
        LIMIT %s
        """,
        (query, query, snapshot_key, limit),
    )
    full_text = [
        {
            "doc_key": str(row[0]),
            "lexical_score": float(row[1] or 0),
            "title_similarity": float(row[2] or 0),
        }
        for row in cursor.fetchall()
    ]
    if not include_title_fallback or len(full_text) >= min(
        limit, max(1, fallback_threshold)
    ):
        return full_text

    cursor.execute(
        """
        WITH title_fallback_hits AS MATERIALIZED (
            SELECT document.doc_key,
                   document.snapshot_key,
                   similarity(lower(document.title), lower(%s)) AS title_similarity,
                   lower(document.title) <-> lower(%s) AS title_distance
            FROM aou.search_docs AS document
            WHERE document.is_lexically_searchable
            ORDER BY lower(document.title) <-> lower(%s)
            LIMIT %s
        )
        SELECT doc_key, 0::real AS lexical_score, title_similarity
        FROM title_fallback_hits
        WHERE snapshot_key = %s
        ORDER BY title_distance, doc_key
        LIMIT %s
        """,
        (query, query, query, max(limit * 8, 100), snapshot_key, limit),
    )
    candidates = {row["doc_key"]: row for row in full_text}
    for doc_key, lexical_score, title_similarity in cursor.fetchall():
        similarity_score = float(title_similarity or 0)
        if similarity_score < 0.28:
            continue
        candidate = candidates.setdefault(
            str(doc_key),
            {"doc_key": str(doc_key), "lexical_score": 0.0, "title_similarity": 0.0},
        )
        candidate["lexical_score"] = max(
            candidate["lexical_score"], float(lexical_score or 0)
        )
        candidate["title_similarity"] = max(
            candidate["title_similarity"], similarity_score
        )
    return sorted(
        candidates.values(),
        key=lambda row: (
            -(row["lexical_score"] + 0.15 * row["title_similarity"]),
            row["doc_key"],
        ),
    )[:limit]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in vector) + "]"


def _semantic_candidates(
    cursor: Any,
    *,
    snapshot_key: str,
    query_embedding: list[float],
    limit: int,
) -> list[dict[str, Any]]:
    vector = _vector_literal(query_embedding)
    embedding_model = (
        os.environ.get("QG_MCP_AOU_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    ).strip()
    oversample_limit = min(max(limit * 10, 250), 1000)
    cursor.execute("SET LOCAL ivfflat.probes = 50")
    cursor.execute("SET LOCAL ivfflat.iterative_scan = relaxed_order")
    cursor.execute("SET LOCAL enable_seqscan = off")
    cursor.execute(
        """
        WITH semantic_content_hits AS MATERIALIZED (
            SELECT cache.content_sha256,
                   cache.embedding <=> %s::vector AS semantic_distance
            FROM aou.embedding_cache AS cache
            WHERE cache.embedding_model = %s
              AND cache.dimensions = 1536
            ORDER BY cache.embedding <=> %s::vector
            LIMIT %s
        )
        SELECT document.doc_key,
               1 - hit.semantic_distance AS semantic_score
        FROM semantic_content_hits AS hit
        JOIN aou.search_docs AS document
          ON document.snapshot_key = %s
         AND document.content_sha256 = hit.content_sha256
        WHERE document.is_embeddable
        ORDER BY hit.semantic_distance, document.doc_key
        LIMIT %s
        """,
        (vector, embedding_model, vector, oversample_limit, snapshot_key, limit),
    )
    rows = cursor.fetchall()
    cursor.execute("SET LOCAL enable_seqscan = on")
    cursor.execute("SET LOCAL ivfflat.iterative_scan = off")
    return [
        {"doc_key": str(row[0]), "semantic_score": float(row[1])}
        for row in rows
        if float(row[1] or 0) >= MIN_SEMANTIC_SCORE
    ]


def _document_summaries(
    cursor: Any,
    doc_keys: list[str],
) -> dict[str, dict[str, Any]]:
    if not doc_keys:
        return {}
    cursor.execute(
        """
        SELECT document.doc_key,
               document.snapshot_key,
               document.item_key,
               document.search_group_key,
               document.search_role,
               document.item_kind,
               document.title,
               document.subtitle,
               document.domain_id,
               document.vocabulary_id,
               document.concept_code,
               document.native_id,
               item.standard_concept,
               item.attributes ->> 'ehr_variable_role' AS ehr_variable_role,
               item.attributes ->> 'ehr_search_layer' AS ehr_search_layer,
               item.attributes ->> 'mapping_status' AS mapping_status,
               link.url AS preferred_url,
               link.target_specificity AS preferred_link_specificity,
               link.label AS preferred_link_label
        FROM aou.search_docs AS document
        JOIN aou.items AS item ON item.item_key = document.item_key
        LEFT JOIN aou.links AS link ON link.link_key = document.preferred_link_key
        WHERE document.doc_key = ANY(%s)
        """,
        (doc_keys,),
    )
    columns = [description.name for description in cursor.description]
    return {str(row[0]): dict(zip(columns, row)) for row in cursor.fetchall()}


def _intent_boost(
    query: str,
    document: dict[str, Any],
    signals: dict[str, float],
) -> float:
    lowered = query.lower()
    kind = document.get("item_kind") or ""
    title = (document.get("title") or "").lower()
    is_survey_variable = kind in {"survey_question_occurrence", "survey_variable"}
    has_lexical_evidence = (
        signals.get("lexical_score", 0) > 0
        or signals.get("title_similarity", 0) >= 0.45
    )
    fitbit_topic_query = (
        "fitbit" in lowered
        or (
            any(
                phrase in lowered
                for phrase in ("daily steps", "step count", "intraday steps")
            )
            and any(term in title for term in ("step", "activity"))
        )
        or ("activity daily summary" in lowered and "activity" in title)
        or ("sleep daily summary" in lowered and "sleep" in title)
    )
    if fitbit_topic_query and kind == "fitbit_metric":
        return 0.04
    if any(term in lowered for term in ("survey", "question", "asked")) and is_survey_variable:
        return 0.02
    survey_topic_query = any(
        term in lowered
        for term in (
            "gender identity",
            "sexual orientation",
            "education level",
            "highest education",
            "how often",
            "do you",
            "have you",
            "every day",
        )
    ) or bool(re.fullmatch(r"(?:highest\s+)?education(?:\s+level)?", lowered.strip()))
    if survey_topic_query and is_survey_variable and has_lexical_evidence:
        return 0.05
    if any(
        term in lowered
        for term in ("all of us", "program collected", "physical measurement")
    ) and kind in {"program_measurement_definition", "omop_concept"}:
        return 0.015
    return 0.0


def _ehr_variable_rank_boost(document: dict[str, Any]) -> float:
    if document.get("item_kind") != "omop_concept":
        return 0.0
    return (
        0.02
        if document.get("ehr_variable_role") in {"standard", "classification"}
        else 0.0
    )


def search_aou_catalog(
    *,
    query: str,
    query_embedding: list[float] | None,
    method: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Search one active public AoU metadata snapshot; never hydrate results."""

    with read_cursor() as cursor:
        snapshot_key = _active_snapshot(cursor)
        exact = _exact_candidates(cursor, snapshot_key=snapshot_key, query=query)
        keyword = (
            _keyword_candidates(
                cursor,
                snapshot_key=snapshot_key,
                query=query,
                limit=max(100, limit * 10),
                include_title_fallback=(
                    not exact and (method == "keyword" or query_embedding is None)
                ),
                fallback_threshold=limit,
            )
            if method in {"keyword", "hybrid"}
            else []
        )
        semantic = (
            _semantic_candidates(
                cursor,
                snapshot_key=snapshot_key,
                query_embedding=query_embedding,
                limit=max(100, limit * 10),
            )
            if method in {"semantic", "hybrid"} and query_embedding is not None
            else []
        )

        scores: defaultdict[str, float] = defaultdict(float)
        reasons: defaultdict[str, list[str]] = defaultdict(list)
        signals: defaultdict[str, dict[str, float]] = defaultdict(dict)
        for rank, row in enumerate(exact, start=1):
            scores[row["doc_key"]] += 10.0 + 1.0 / (rank + 1)
            reasons[row["doc_key"]].append(row["match_kind"])
        for rank, row in enumerate(keyword, start=1):
            scores[row["doc_key"]] += 1.0 / (60 + rank)
            reasons[row["doc_key"]].append("lexical")
            signals[row["doc_key"]]["lexical_score"] = row["lexical_score"]
            signals[row["doc_key"]]["title_similarity"] = row["title_similarity"]
        for rank, row in enumerate(semantic, start=1):
            scores[row["doc_key"]] += 1.0 / (60 + rank)
            reasons[row["doc_key"]].append("semantic")
            signals[row["doc_key"]]["semantic_score"] = row["semantic_score"]

        documents = _document_summaries(cursor, list(scores))
        scores = defaultdict(
            float,
            {key: value for key, value in scores.items() if key in documents},
        )
        for doc_key, document in documents.items():
            scores[doc_key] += _intent_boost(query, document, signals[doc_key])
            scores[doc_key] += _ehr_variable_rank_boost(document)

        ordered = sorted(
            scores,
            key=lambda key: (-scores[key], documents[key].get("title") or "", key),
        )
        selected: list[str] = []
        title_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
        for doc_key in ordered:
            document = documents[doc_key]
            title_key = (
                _normalize_alias(document.get("title") or ""),
                _normalize_alias(document.get("domain_id") or ""),
            )
            if title_counts[title_key] >= 2:
                continue
            title_counts[title_key] += 1
            selected.append(doc_key)
            if len(selected) >= limit:
                break

        results: list[dict[str, Any]] = []
        for doc_key in selected:
            result = dict(documents[doc_key])
            result["score"] = scores[doc_key]
            result["match_reasons"] = sorted(set(reasons[doc_key]))
            result.update(signals[doc_key])
            results.append(result)
        return results


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [description.name for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _hydrate_survey_scales(
    cursor: Any,
    scale_item_keys: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not scale_item_keys:
        return [], [], []
    cursor.execute(
        """
        SELECT scale.*
        FROM aou.survey_scales AS scale
        WHERE scale.scale_key = ANY(%s)
        ORDER BY scale.sheet_name, scale.scale_name, scale.scale_key
        LIMIT 100
        """,
        (scale_item_keys,),
    )
    scales = _dict_rows(cursor)
    cursor.execute(
        """
        SELECT membership.membership_key,
               membership.snapshot_key,
               membership.scale_key,
               membership.variable_item_key,
               membership.question_code,
               membership.question_text,
               membership.question_source_value_text,
               membership.ordinal,
               membership.duplicate_ordinal,
               membership.reverse_scored,
               membership.variable_match_method,
               membership.qa_flags,
               membership.source_row_number,
               membership.source_locator,
               membership.source_record_sha256,
               variable.canonical_key AS variable_canonical_key,
               variable.name AS variable_name,
               variable.concept_code AS variable_concept_code,
               variable.description AS variable_description,
               document.doc_key AS variable_doc_key,
               link.url AS variable_preferred_url,
               link.target_specificity AS variable_link_specificity
        FROM aou.survey_scale_memberships AS membership
        JOIN aou.items AS variable ON variable.item_key = membership.variable_item_key
        LEFT JOIN aou.search_docs AS document
          ON document.item_key = membership.variable_item_key
         AND document.snapshot_key = membership.snapshot_key
        LEFT JOIN aou.links AS link ON link.link_key = document.preferred_link_key
        WHERE membership.scale_key = ANY(%s)
        ORDER BY membership.scale_key, membership.ordinal,
                 membership.duplicate_ordinal, membership.membership_key
        LIMIT 200
        """,
        (scale_item_keys,),
    )
    memberships = _dict_rows(cursor)
    membership_keys = [row["membership_key"] for row in memberships]
    if not membership_keys:
        return scales, memberships, []
    cursor.execute(
        """
        SELECT response.response_weight_key,
               response.snapshot_key,
               response.membership_key,
               response.answer_item_key,
               response.response_code,
               response.response_label,
               response.weight_text,
               response.weight_value,
               response.derived_weight_value,
               response.derived_weight_method,
               response.derived_weight_source_row_number,
               response.ordinal,
               response.answer_match_method,
               response.source_row_number,
               response.source_locator,
               response.source_record_sha256,
               answer.name AS answer_name,
               answer.concept_code AS answer_concept_code
        FROM aou.survey_scale_response_weights AS response
        LEFT JOIN aou.items AS answer ON answer.item_key = response.answer_item_key
        WHERE response.membership_key = ANY(%s)
        ORDER BY response.membership_key, response.ordinal,
                 response.response_weight_key
        LIMIT 500
        """,
        (membership_keys,),
    )
    return scales, memberships, _dict_rows(cursor)


def _hydrate_document(cursor: Any, doc_key: str) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT member.item_key, item.canonical_key, item.item_kind, item.name,
               item.domain_id, item.vocabulary_id, item.concept_code,
               item.standard_concept, item.survey_concept_id, item.survey_path,
               item.description, item.help_text, item.attributes,
               item.source_locator, item.source_record_sha256
        FROM aou.search_doc_members AS member
        JOIN aou.items AS item ON item.item_key = member.item_key
        WHERE member.doc_key = %s
        ORDER BY item.item_kind, item.name NULLS LAST, item.item_key
        LIMIT 200
        """,
        (doc_key,),
    )
    members = _dict_rows(cursor)
    item_keys = [member["item_key"] for member in members]
    scale_item_keys = [
        member["item_key"]
        for member in members
        if member["item_kind"] == "survey_scale"
    ]
    scales, memberships, response_weights = _hydrate_survey_scales(
        cursor, scale_item_keys
    )
    relationship_item_keys = list(
        dict.fromkeys(
            [
                *item_keys,
                *(membership["variable_item_key"] for membership in memberships),
            ]
        )
    )

    identifiers: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    if item_keys:
        cursor.execute(
            """
            SELECT identifier.item_key, identifier.identifier_system,
                   identifier.identifier_value, identifier.identifier_role,
                   identifier.is_primary
            FROM aou.identifiers AS identifier
            WHERE identifier.item_key = ANY(%s)
            ORDER BY identifier.item_key, identifier.is_primary DESC,
                     identifier.identifier_system, identifier.identifier_value
            LIMIT 500
            """,
            (item_keys,),
        )
        identifiers = _dict_rows(cursor)
        cursor.execute(
            """
            SELECT link.item_key, link.link_type, link.target_specificity,
                   link.url, link.label, link.is_preferred
            FROM aou.links AS link
            WHERE link.item_key = ANY(%s)
            ORDER BY link.item_key, link.is_preferred DESC, link.link_type
            LIMIT 500
            """,
            (item_keys,),
        )
        links = _dict_rows(cursor)
        cursor.execute(
            """
            SELECT detail.*
            FROM aou.program_measurement_details AS detail
            WHERE detail.item_key = ANY(%s)
            ORDER BY detail.item_key
            LIMIT 200
            """,
            (item_keys,),
        )
        measurements = _dict_rows(cursor)
    if relationship_item_keys:
        cursor.execute(
            """
            SELECT edge.edge_key, edge.from_item_key, edge.predicate,
                   edge.to_item_key, edge.qualifier_item_key,
                   edge.assertion_status, edge.match_method,
                   edge.source_locator, edge.evidence_text,
                   source.name AS source_name, target.name AS target_name,
                   target.vocabulary_id AS target_vocabulary_id,
                   target.concept_code AS target_concept_code
            FROM aou.edges AS edge
            JOIN aou.items AS source ON source.item_key = edge.from_item_key
            JOIN aou.items AS target ON target.item_key = edge.to_item_key
            WHERE edge.from_item_key = ANY(%s) OR edge.to_item_key = ANY(%s)
            ORDER BY edge.predicate, edge.ordinal NULLS LAST, edge.edge_key
            LIMIT 100
            """,
            (relationship_item_keys, relationship_item_keys),
        )
        relationships = _dict_rows(cursor)
        cursor.execute(
            """
            SELECT answer.item_key,
                   answer.name AS label,
                   answer.concept_code AS answer_code,
                   MIN(edge.ordinal) AS ordinal,
                   answer.attributes -> 'labels' AS labels,
                   answer.attributes -> 'versions' AS versions,
                   COUNT(*) AS declared_occurrences
            FROM aou.edges AS edge
            JOIN aou.items AS answer
              ON answer.item_key = edge.to_item_key
             AND answer.item_kind = 'survey_answer_option'
            WHERE edge.from_item_key = ANY(%s)
              AND edge.predicate = 'has_answer'
            GROUP BY answer.item_key, answer.name, answer.concept_code,
                     answer.attributes -> 'labels', answer.attributes -> 'versions'
            ORDER BY MIN(edge.ordinal) NULLS LAST,
                     lower(answer.name) NULLS LAST, answer.item_key
            LIMIT 200
            """,
            (relationship_item_keys,),
        )
        answers = _dict_rows(cursor)

    cursor.execute(
        """
        SELECT snapshot.snapshot_key, snapshot.release_id, snapshot.release_label,
               snapshot.extracted_at_utc, snapshot.source_commit_sha,
               snapshot.coverage_status, snapshot.selection_method
        FROM aou.search_docs AS document
        JOIN aou.snapshots AS snapshot USING (snapshot_key)
        WHERE document.doc_key = %s
        """,
        (doc_key,),
    )
    columns = [description.name for description in cursor.description]
    row = cursor.fetchone()
    snapshot = dict(zip(columns, row)) if row else None
    return {
        "members": members,
        "identifiers": identifiers,
        "links": links,
        "program_measurements": measurements,
        "survey_scales": scales,
        "survey_scale_memberships": memberships,
        "survey_scale_response_weights": response_weights,
        "survey_answer_options": answers,
        "relationships": relationships,
        "snapshot": snapshot,
    }


def get_aou_item(doc_key: str) -> dict[str, Any] | None:
    """Return one active searchable item with its bounded related metadata."""

    with read_cursor() as cursor:
        snapshot_key = _active_snapshot(cursor)
        summaries = _document_summaries(cursor, [doc_key])
        summary = summaries.get(doc_key)
        if not summary:
            return None
        cursor.execute(
            """
            SELECT 1
            FROM aou.search_docs
            WHERE doc_key = %s AND snapshot_key = %s
            """,
            (doc_key, snapshot_key),
        )
        if cursor.fetchone() is None:
            return None
        result = dict(summary)
        result["details"] = _hydrate_document(cursor, doc_key)
        return result


__all__ = ["get_aou_item", "search_aou_catalog"]
