"""Pure-Python, structured, read-only QueryGaP retrieval service."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit

from .contracts import JsonObject, Provenance, ServiceError
from .quota import EmbeddingBudgetExceeded


_FULL_STUDY_ID_RE = re.compile(r"^phs\d{6,}\.v\d+\.p\d+$", re.IGNORECASE)
_FULL_STUDY_ID_SEARCH_RE = re.compile(r"phs\d{6,}\.v\d+\.p\d+", re.IGNORECASE)
_ACCESSION_NUMBER_RE = re.compile(r"^(?:phv|pht|phd)(\d+)", re.IGNORECASE)

_DBGAP_SOURCE = "NCBI dbGaP"
_UKB_SOURCE = "UK Biobank Showcase"
_AOU_SOURCE = "All of Us Research Program Data Browser"
_AOU_DOC_KEY_RE = re.compile(r"^aou\.doc\.[0-9a-f]{24}$")
_AOU_VARIABLE_TYPES = frozenset(
    {"ehr", "survey", "physical_measurement", "fitbit"}
)
_AOU_EHR_DOMAINS = frozenset({"Condition", "Drug", "Measurement", "Procedure"})
_AOU_EHR_ROLES = frozenset({"standard", "source", "classification"})
_AOU_PREFERRED_LINK_HOSTS = frozenset(
    {
        "athena.ohdsi.org",
        "databrowser.researchallofus.org",
        "docs.google.com",
        "github.com",
        "public.api.researchallofus.org",
        "support.researchallofus.org",
        "www.researchallofus.org",
    }
)
_DBGAP_STUDY_URL = "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/study.cgi"
_DBGAP_VARIABLE_URL = "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/variable.cgi"
_DBGAP_DATASET_URL = "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/dataset.cgi"
_DBGAP_DOCUMENT_URL = "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/document.cgi"
_UKB_FIELD_URL = "https://biobank.ndph.ox.ac.uk/ukb/field.cgi"


class EmbeddingProvider(Protocol):
    """Injectable query embedding provider used only when explicitly requested."""

    def __call__(self, text: str) -> Sequence[float]: ...


Rows = Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class RetrievalDependencies:
    """Repository function dependencies, injectable for isolated tests."""

    search_studies: Callable[..., Rows]
    get_study_metadata: Callable[[str], Mapping[str, Any] | None]
    search_variables: Callable[..., Rows]
    search_datasets: Callable[..., Rows]
    search_documents: Callable[..., Rows]
    search_ukb_keyword: Callable[..., Rows]
    search_ukb_hybrid: Callable[..., Rows]
    get_ukb_field_details: Callable[..., Mapping[str, Any] | None]
    get_ukb_field_instance_summaries: Callable[[int], Mapping[str, Any] | None]
    normalize_study_query_alias: Callable[[str], str] = lambda value: value
    resolve_study_accession: Callable[[str], Mapping[str, Any] | None] | None = None
    search_aou: Callable[..., Rows] | None = None
    get_aou_details: Callable[[str], Mapping[str, Any] | None] | None = None


class QueryGaPRetrievalService:
    """Stable structured facade over the repository's fixed read functions."""

    def __init__(
        self,
        dependencies: RetrievalDependencies,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._deps = dependencies
        self._embedding_provider = embedding_provider

    def resolve_dbgap_study(self, query: str, limit: int = 6) -> JsonObject:
        query = _validate_text(query, field="query", max_length=300)
        limit = _validate_limit(limit, maximum=6)
        accession = _FULL_STUDY_ID_SEARCH_RE.search(query)
        normalized_query = accession.group(0).lower() if accession else _validate_text(
            self._call(self._deps.normalize_study_query_alias, query),
            field="normalized_query",
            max_length=300,
        )

        if accession and self._deps.resolve_study_accession is not None:
            exact = self._call(self._deps.resolve_study_accession, normalized_query)
            rows = [exact] if exact else []
        else:
            rows = self._call(
                self._deps.search_studies,
                query=normalized_query,
                limit=limit,
            )
        candidates = [
            self._study_candidate(row)
            for row in (rows or [])
            if _is_full_study_id(row.get("study_id"))
        ][:limit]
        resolution_status = "ranked_candidates" if candidates else "not_found"
        return {
            "query": query,
            "normalized_query": normalized_query,
            "recommended": candidates[0] if candidates else None,
            "candidates": candidates,
            "resolution": {
                "status": resolution_status,
                "authoritative": False,
                "policy": "name_metadata_match_then_variable_count_dataset_count_accession",
            },
            "warnings": [
                "The recommended accession is heuristic; inspect alternatives and upstream study pages."
            ],
        }

    def get_dbgap_study(self, study_id: str) -> JsonObject:
        study_id = _validate_full_study_id(study_id)
        metadata_record = self._call(self._deps.get_study_metadata, study_id)
        if not metadata_record:
            raise ServiceError(
                "not_found",
                "The requested dbGaP study was not found.",
                details={"study_id": study_id},
            )

        # The current catalog can store exact-version datasets/variables while
        # the study metadata row itself is base-scoped. That fallback is
        # permitted only after the exact requested accession has been verified
        # by the repository, and it is disclosed rather than relabeled.
        if "metadata" in metadata_record:
            metadata = metadata_record.get("metadata") or {}
            metadata_scope = metadata_record.get("metadata_scope") or "unknown"
            metadata_source_study_id = metadata_record.get("metadata_source_study_id")
        else:
            metadata = metadata_record
            metadata_scope = "exact"
            metadata_source_study_id = study_id

        source_url = _dbgap_study_url(study_id)
        warnings = [
            "Study metadata lacks a retained source snapshot, checksum, and parser version."
        ]
        if metadata_scope == "base":
            warnings.append(
                "The exact catalog accession was verified, but its descriptive study metadata comes from a base-scoped row."
            )
        return {
            "study": {
                "source": "dbgap",
                "study_id": study_id,
                "name": metadata.get("study_name"),
                "metadata": _bounded_json(metadata),
                "metadata_scope": metadata_scope,
                "metadata_source_study_id": metadata_source_study_id,
                "canonical_url": source_url,
            },
            "provenance": _provenance(_DBGAP_SOURCE, source_url),
            "warnings": warnings,
        }

    def search_dbgap_catalog(
        self,
        *,
        kind: str,
        query: str,
        study_id: str,
        method: str = "hybrid",
        limit: int = 10,
        include_stats: bool = False,
    ) -> JsonObject:
        if kind not in {"variable", "dataset", "document"}:
            raise ServiceError(
                "invalid_scope",
                "dbGaP kind must be variable, dataset, or document.",
                details={"kind": kind},
            )
        query = _validate_text(query, field="query", max_length=500)
        study_id = _validate_full_study_id(study_id)
        method = _validate_method(method, allowed={"keyword", "semantic", "hybrid"})
        limit = _validate_limit(limit, maximum=20)
        if not isinstance(include_stats, bool):
            raise ServiceError(
                "invalid_request",
                "include_stats must be a boolean.",
                details={"field": "include_stats"},
            )
        if include_stats and kind != "variable":
            raise ServiceError(
                "invalid_scope",
                "include_stats is available only for dbGaP variable search.",
                details={"kind": kind},
            )
        effective_method = method
        degraded_reason = None
        if method == "keyword":
            query_embedding = None
        else:
            try:
                query_embedding = self._embedding_for(query)
            except ServiceError as error:
                if method != "hybrid":
                    raise
                query_embedding = None
                effective_method = "keyword"
                degraded_reason = error.code

        function = {
            "variable": self._deps.search_variables,
            "dataset": self._deps.search_datasets,
            "document": self._deps.search_documents,
        }[kind]
        kwargs: JsonObject = {
            "query": query,
            "query_embedding": query_embedding,
            "method": effective_method,
            "study_id": study_id,
            "limit": limit,
        }
        if kind == "variable":
            kwargs["include_stats"] = include_stats
        rows = self._call(function, **kwargs)
        items = [
            self._dbgap_item(kind, row, expected_study_id=study_id)
            for row in (rows or [])[:limit]
        ]

        warnings = [
            "Scores are ranking signals and are not comparable across queries or entity kinds.",
            "Rows lack complete source snapshot, checksum, and parser/build-version lineage.",
        ]
        if kind == "document":
            warnings.append(
                "Document bodies are not indexed; keyword and semantic retrieval use title metadata only."
            )

        return {
            "source": "dbgap",
            "kind": kind,
            "study_id": study_id,
            "query": query,
            "items": items,
            "retrieval": _retrieval_metadata(
                method=method,
                effective_method=effective_method,
                embedding_used=query_embedding is not None,
                keyword_weight=0.3 if effective_method == "hybrid" else None,
                semantic_weight=0.7 if effective_method == "hybrid" else None,
                degraded_reason=degraded_reason,
            ),
            "include_stats": include_stats,
            "provenance_warnings": warnings,
        }

    def search_ukb_fields(
        self,
        *,
        query: str,
        method: str = "keyword",
        limit: int = 10,
    ) -> JsonObject:
        query = _validate_text(query, field="query", max_length=500)
        method = _validate_method(method, allowed={"keyword", "hybrid"})
        limit = _validate_limit(limit, maximum=20)

        if method == "hybrid":
            try:
                query_embedding = self._embedding_for(query)
            except ServiceError as error:
                query_embedding = None
                effective_method = "keyword"
                degraded_reason = error.code
                rows = self._call(
                    self._deps.search_ukb_keyword,
                    query=query,
                    limit=limit,
                )
            else:
                effective_method = "hybrid"
                degraded_reason = None
                rows = self._call(
                    self._deps.search_ukb_hybrid,
                    query=query,
                    query_embedding=query_embedding,
                    limit=limit,
                )
        else:
            query_embedding = None
            effective_method = "keyword"
            degraded_reason = None
            rows = self._call(self._deps.search_ukb_keyword, query=query, limit=limit)

        items = [self._ukb_item(row) for row in (rows or [])[:limit]]
        return {
            "source": "ukb",
            "kind": "field",
            "query": query,
            "items": items,
            "retrieval": _retrieval_metadata(
                method=method,
                effective_method=effective_method,
                embedding_used=query_embedding is not None,
                keyword_weight=0.6 if effective_method == "hybrid" else None,
                semantic_weight=0.4 if effective_method == "hybrid" else None,
                degraded_reason=degraded_reason,
            ),
            "provenance_warnings": [
                "Scores are ranking signals and are not comparable across queries or sources.",
                "Exact field IDs and stored aliases receive ranking boosts.",
                "Stored instance/array aliases may be bounded samples rather than every valid combination.",
                "Displayed category paths may select one parent from a multi-parent source graph.",
                "Database rows do not retain the downloader checksum or complete source-snapshot lineage.",
            ],
        }

    def get_ukb_field(
        self,
        field_id: int,
        include_instance_summaries: bool = False,
    ) -> JsonObject:
        field_id = _validate_field_id(field_id)
        if not isinstance(include_instance_summaries, bool):
            raise ServiceError(
                "invalid_request",
                "include_instance_summaries must be a boolean.",
                details={"field": "include_instance_summaries"},
            )
        details = self._call(self._deps.get_ukb_field_details, field_id)
        if not details:
            raise ServiceError(
                "not_found",
                "The requested UK Biobank field was not found.",
                details={"field_id": field_id},
            )

        source_url = _ukb_field_url(field_id)
        field = {
            "source": "ukb",
            "kind": "field",
            "id": field_id,
            "title": _bounded_text(details.get("title"), 500),
            "notes": _bounded_text(details.get("notes"), 3000),
            "units": _bounded_text(details.get("units"), 200),
            "value_type": _bounded_text(details.get("value_type"), 200),
            "item_type": _bounded_text(details.get("item_type"), 200),
            "category_path": _bounded_text(details.get("category_path"), 1000),
            "encoding_id": details.get("encoding_id"),
            "encoding_title": _bounded_text(details.get("encoding_title"), 500),
            "canonical_url": source_url,
            "category_url": _allowed_ukb_url(details.get("category_url")),
            "encoding_url": _allowed_ukb_url(details.get("encoding_url")),
            "content_indexed": True,
            "indexed_content": "title_and_notes",
            "provenance": _provenance(_UKB_SOURCE, source_url),
        }
        instance_summary = None
        if include_instance_summaries:
            instance_summary = self._call(
                self._deps.get_ukb_field_instance_summaries,
                field_id,
            )

        return {
            "field": _json_value(field),
            "instance_summary": _bounded_json(instance_summary),
            "warnings": [
                "Category paths may select one parent from a multi-parent source graph.",
                "Database rows lack retained downloader checksums and complete source-snapshot lineage.",
            ],
        }

    def search_aou_catalog(
        self,
        *,
        query: str,
        method: str = "hybrid",
        limit: int = 10,
        variable_type: str | None = None,
        ehr_domain: str | None = None,
        ehr_role: str | None = None,
        ehr_vocabulary: str | None = None,
    ) -> JsonObject:
        """Search public AoU variables and related navigation metadata."""

        if self._deps.search_aou is None:
            raise ServiceError(
                "source_unavailable",
                "The All of Us catalog is not configured for this server.",
            )
        query = _validate_text(query, field="query", max_length=500)
        method = _validate_method(method, allowed={"keyword", "semantic", "hybrid"})
        limit = _validate_limit(limit, maximum=20)
        variable_type = _validate_optional_choice(
            variable_type,
            field="variable_type",
            allowed=_AOU_VARIABLE_TYPES,
        )
        ehr_domain = _validate_optional_choice(
            ehr_domain,
            field="ehr_domain",
            allowed=_AOU_EHR_DOMAINS,
        )
        ehr_role = _validate_optional_choice(
            ehr_role,
            field="ehr_role",
            allowed=_AOU_EHR_ROLES,
        )
        ehr_vocabulary = _validate_optional_text(
            ehr_vocabulary,
            field="ehr_vocabulary",
            max_length=100,
        )
        if variable_type != "ehr" and any(
            value is not None for value in (ehr_domain, ehr_role, ehr_vocabulary)
        ):
            raise ServiceError(
                "invalid_request",
                "ehr_domain, ehr_role, and ehr_vocabulary require variable_type='ehr'.",
                details={"field": "variable_type"},
            )

        applied_filters = {
            key: value
            for key, value in {
                "variable_type": variable_type,
                "ehr_domain": ehr_domain,
                "ehr_role": ehr_role,
                "ehr_vocabulary": ehr_vocabulary,
            }.items()
            if value is not None
        }

        effective_method = method
        degraded_reason = None
        if method == "keyword":
            query_embedding = None
        else:
            try:
                query_embedding = self._embedding_for(query)
            except ServiceError as error:
                if method == "semantic":
                    raise
                query_embedding = None
                effective_method = "keyword"
                degraded_reason = error.code

        rows = self._call(
            self._deps.search_aou,
            query=query,
            query_embedding=query_embedding,
            method=effective_method,
            limit=limit,
            variable_type=variable_type,
            ehr_domain=ehr_domain,
            ehr_role=ehr_role,
            ehr_vocabulary=ehr_vocabulary,
        )
        items = [self._aou_item(row) for row in (rows or [])[:limit]]
        return {
            "source": "aou",
            "query": query,
            "applied_filters": applied_filters,
            "items": items,
            "retrieval": _retrieval_metadata(
                method=method,
                effective_method=effective_method,
                embedding_used=query_embedding is not None,
                keyword_weight=None,
                semantic_weight=None,
                degraded_reason=degraded_reason,
            ),
            "interpretation": {
                "variable_rule": "is_variable is true for primary and grouped search records",
                "support_records": (
                    "navigation and support records provide context but are not variables"
                ),
            },
            "warnings": [
                "This searches public metadata only; it does not query "
                "participant-level Workbench data.",
                "OMOP concepts are variable-like EHR data elements, not physical database columns.",
                "Scores are ranking signals and are not comparable across queries or sources.",
            ],
        }

    def get_aou_item(self, result_id: str) -> JsonObject:
        """Hydrate one AoU search result with identifiers and relationships."""

        if self._deps.get_aou_details is None:
            raise ServiceError(
                "source_unavailable",
                "The All of Us catalog is not configured for this server.",
            )
        result_id = _validate_aou_result_id(result_id)
        row = self._call(self._deps.get_aou_details, result_id)
        if not row:
            raise ServiceError(
                "not_found",
                "The requested All of Us catalog item was not found in the active snapshot.",
                details={"result_id": result_id},
            )
        details = _bounded_json(row.get("details") or {})
        _sanitize_aou_detail_links(details)
        return {
            "item": self._aou_item(row),
            "details": details,
            "warnings": [
                "This is public catalog metadata, not participant-level Workbench data.",
                "Related collections are bounded to keep MCP responses small.",
                "A source link documents or locates a data element; it does not grant data access.",
            ],
        }

    def _embedding_for(self, query: str) -> list[float]:
        if self._embedding_provider is None:
            raise ServiceError(
                "embeddings_unavailable",
                "Semantic and hybrid retrieval require an explicitly configured embedding provider.",
            )
        try:
            raw = self._embedding_provider(query)
        except EmbeddingBudgetExceeded:
            raise ServiceError(
                "embedding_budget_exhausted",
                "The query-embedding budget is exhausted; hybrid retrieval can use "
                "keyword fallback.",
            ) from None
        except Exception:
            raise ServiceError(
                "embedding_provider_unavailable",
                "The configured embedding provider is unavailable.",
            ) from None
        if isinstance(raw, (str, bytes)):
            raise ServiceError(
                "invalid_embedding",
                "The embedding provider returned an invalid vector.",
            )
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError, OverflowError):
            raise ServiceError(
                "invalid_embedding",
                "The embedding provider returned an invalid vector.",
            ) from None
        if len(vector) != 1536 or not all(math.isfinite(value) for value in vector):
            raise ServiceError(
                "invalid_embedding",
                "The embedding provider must return 1536 finite numeric values.",
            )
        return vector

    @staticmethod
    def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except ServiceError:
            raise
        except Exception as exc:
            code = "schema_missing" if type(exc).__name__ == "MissingSchemaError" else "database_unavailable"
            message = (
                "The retrieval database schema is incomplete."
                if code == "schema_missing"
                else "The retrieval backend is unavailable."
            )
            raise ServiceError(code, message) from None

    @staticmethod
    def _study_candidate(row: Mapping[str, Any]) -> JsonObject:
        study_id = _optional_text(row.get("study_id"))
        source_url = _dbgap_study_url(study_id) if study_id else None
        return {
            "study_id": study_id,
            "name": row.get("name"),
            "dataset_count": _optional_int(row.get("dataset_count")),
            "variable_count": _optional_int(row.get("variable_count")),
            "canonical_url": source_url,
            "provenance": _provenance(_DBGAP_SOURCE, source_url),
        }

    @staticmethod
    def _dbgap_item(
        kind: str,
        row: Mapping[str, Any],
        *,
        expected_study_id: str,
    ) -> JsonObject:
        row_study_id = _optional_text(row.get("study_id")) or expected_study_id
        if row_study_id.lower() != expected_study_id.lower():
            raise ServiceError(
                "retrieval_contract_violation",
                "The retrieval backend returned a result outside the requested study scope.",
            )
        identifier_key = {
            "variable": "variable_id",
            "dataset": "dataset_id",
            "document": "document_id",
        }[kind]
        identifier = _optional_text(row.get(identifier_key))
        source_url = _dbgap_entity_url(kind, identifier, row_study_id, row.get("url"))
        item: JsonObject = {
            "source": "dbgap",
            "kind": kind,
            "id": identifier,
            "study_id": row_study_id,
            "parent_id": _optional_text(row.get("dataset_id")) if kind == "variable" else None,
            "title": _bounded_text(row.get("name"), 500),
            "description": _bounded_text(row.get("description"), 3000),
            "document_type": (
                _bounded_text(row.get("document_type"), 200)
                if kind == "document"
                else None
            ),
            "canonical_url": source_url,
            "scores": _scores(row),
            "content_indexed": kind != "document",
            "indexed_content": "title_only" if kind == "document" else "name_and_description",
            "provenance": _provenance(_DBGAP_SOURCE, source_url),
        }
        if kind == "variable":
            item["summary_statistics"] = {
                "n": _optional_int(row.get("stat_n")),
                "mean": _optional_float(row.get("stat_mean")),
                "median": _optional_float(row.get("stat_median")),
                "standard_deviation": _optional_float(row.get("stat_sd")),
                "minimum": _optional_float(row.get("stat_min")),
                "maximum": _optional_float(row.get("stat_max")),
            }
        return _json_value(item)

    @staticmethod
    def _ukb_item(row: Mapping[str, Any]) -> JsonObject:
        field_id = _optional_int(row.get("field_id"))
        source_url = _ukb_field_url(field_id) if field_id is not None else None
        item: JsonObject = {
            "source": "ukb",
            "kind": "field",
            "id": field_id,
            "title": _bounded_text(row.get("title"), 500),
            "notes": _bounded_text(row.get("notes"), 3000),
            "category_path": _bounded_text(row.get("category_path"), 1000),
            "aliases": _bounded_text(row.get("aliases"), 2000),
            "canonical_url": source_url,
            "scores": _scores(row),
            "content_indexed": True,
            "indexed_content": "title_and_notes",
            "provenance": _provenance(_UKB_SOURCE, source_url),
        }
        return _json_value(item)

    @staticmethod
    def _aou_item(row: Mapping[str, Any]) -> JsonObject:
        result_id = _optional_text(row.get("doc_key"))
        search_role = _optional_text(row.get("search_role"))
        item_kind = _optional_text(row.get("item_kind"))
        source_url = _allowed_aou_preferred_url(row.get("preferred_url"))
        is_variable = search_role in {"primary", "grouped"}
        provenance = _provenance(_AOU_SOURCE, source_url)
        snapshot_id = _optional_text(row.get("snapshot_key"))
        if snapshot_id:
            provenance["snapshot_id"] = snapshot_id
            provenance["status"] = "snapshot_and_source_locator"
        item: JsonObject = {
            "source": "aou",
            "id": result_id,
            "kind": item_kind,
            "search_role": search_role,
            "is_variable": is_variable,
            "variable_class": _aou_variable_class(row) if is_variable else None,
            "ehr_variable_role": _optional_text(row.get("ehr_variable_role")),
            "ehr_search_layer": _optional_text(row.get("ehr_search_layer")),
            "title": _bounded_text(row.get("title"), 500),
            "subtitle": _bounded_text(row.get("subtitle"), 1500),
            "domain": _bounded_text(row.get("domain_id"), 200),
            "vocabulary": _bounded_text(row.get("vocabulary_id"), 200),
            "concept_code": _bounded_text(row.get("concept_code"), 500),
            "native_id": _bounded_text(row.get("native_id"), 500),
            "standard_concept": _bounded_text(row.get("standard_concept"), 20),
            "mapping_status": _bounded_text(row.get("mapping_status"), 200),
            "canonical_url": source_url,
            "link_specificity": _bounded_text(
                row.get("preferred_link_specificity"), 200
            ),
            "link_label": _bounded_text(row.get("preferred_link_label"), 500),
            "match_reasons": _bounded_json(row.get("match_reasons") or []),
            "scores": {
                "lexical": _optional_float(row.get("lexical_score")),
                "semantic": _optional_float(row.get("semantic_score")),
                "combined": _optional_float(row.get("score")),
            },
            "provenance": provenance,
        }
        return _json_value(item)


def repository_dependencies() -> RetrievalDependencies:
    """Load only the standalone MCP repository's fixed read adapters."""

    from querygap_mcp import aou_repository, repository

    return RetrievalDependencies(
        search_studies=repository.search_studies,
        get_study_metadata=repository.get_study_metadata,
        search_variables=repository.search_variables_flexible,
        search_datasets=repository.search_datasets_flexible,
        search_documents=repository.search_documents_flexible,
        search_ukb_keyword=repository.search_ukb_fields_keyword,
        search_ukb_hybrid=repository.search_ukb_fields_hybrid,
        get_ukb_field_details=repository.get_ukb_field_details,
        get_ukb_field_instance_summaries=repository.get_ukb_field_instance_summaries,
        normalize_study_query_alias=repository.normalize_study_query_alias,
        resolve_study_accession=repository.resolve_study_accession,
        search_aou=aou_repository.search_aou_catalog,
        get_aou_details=aou_repository.get_aou_item,
    )


def create_repository_service(
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> QueryGaPRetrievalService:
    if embedding_provider is None:
        from .embedding import OpenAIEmbeddingProvider

        embedding_provider = OpenAIEmbeddingProvider.from_environment()
    return QueryGaPRetrievalService(
        dependencies=repository_dependencies(),
        embedding_provider=embedding_provider,
    )


# Concise compatibility name for transport adapters.
QueryGaPService = QueryGaPRetrievalService


def _validate_text(value: Any, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ServiceError(
            "invalid_request",
            f"{field} must be a string.",
            details={"field": field},
        )
    normalized = " ".join(value.split()).strip()
    if not normalized or len(normalized) > max_length:
        raise ServiceError(
            "invalid_request",
            f"{field} must contain between 1 and {max_length} characters.",
            details={"field": field},
        )
    return normalized


def _validate_limit(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ServiceError(
            "invalid_request",
            f"limit must be an integer between 1 and {maximum}.",
            details={"field": "limit"},
        )
    return value


def _validate_field_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServiceError(
            "invalid_request",
            "field_id must be a positive integer.",
            details={"field": "field_id"},
        )
    return value


def _validate_aou_result_id(value: Any) -> str:
    if not isinstance(value, str) or not _AOU_DOC_KEY_RE.fullmatch(value.strip()):
        raise ServiceError(
            "invalid_scope",
            "result_id must be an All of Us ID returned by search_aou_catalog.",
            details={"field": "result_id"},
        )
    return value.strip()


def _validate_method(value: Any, *, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ServiceError(
            "invalid_request",
            f"method must be one of: {', '.join(sorted(allowed))}.",
            details={"field": "method"},
        )
    return value


def _validate_optional_choice(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ServiceError(
            "invalid_request",
            f"{field} must be one of: {', '.join(sorted(allowed))}.",
            details={"field": field},
        )
    return value


def _validate_optional_text(
    value: Any,
    *,
    field: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field=field, max_length=max_length)


def _validate_full_study_id(value: Any) -> str:
    if not isinstance(value, str) or not _FULL_STUDY_ID_RE.fullmatch(value.strip()):
        raise ServiceError(
            "invalid_scope",
            "study_id must be a full versioned dbGaP accession such as phs000007.v35.p16.",
            details={"field": "study_id"},
        )
    return value.strip().lower()


def _is_full_study_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_STUDY_ID_RE.fullmatch(value.strip()))


def _dbgap_study_url(study_id: str) -> str:
    return f"{_DBGAP_STUDY_URL}?study_id={quote(study_id, safe='')}"


def _dbgap_entity_url(
    kind: str,
    identifier: str | None,
    study_id: str,
    stored_url: Any = None,
) -> str | None:
    expected_prefix = {"variable": "phv", "dataset": "pht", "document": "phd"}[kind]
    if identifier and identifier.lower().startswith(expected_prefix):
        match = _ACCESSION_NUMBER_RE.match(identifier)
        if match:
            number = str(int(match.group(1)))
            base = {
                "variable": _DBGAP_VARIABLE_URL,
                "dataset": _DBGAP_DATASET_URL,
                "document": _DBGAP_DOCUMENT_URL,
            }[kind]
            parameter = {"variable": "phv", "dataset": "pht", "document": "phd"}[kind]
            return (
                f"{base}?study_id={quote(study_id, safe='')}"
                f"&{parameter}={quote(number, safe='')}"
            )
    if kind == "document":
        candidate = _optional_text(stored_url)
        if candidate and candidate.startswith("https://www.ncbi.nlm.nih.gov/"):
            return candidate
    return None


def _ukb_field_url(field_id: int) -> str:
    return f"{_UKB_FIELD_URL}?id={field_id}"


def _allowed_ukb_url(value: Any) -> str | None:
    candidate = _optional_text(value)
    if candidate and candidate.startswith("https://biobank.ndph.ox.ac.uk/ukb/"):
        return candidate
    return None


def _safe_https_url(value: Any) -> str | None:
    candidate = _optional_text(value)
    if candidate is None or len(candidate) > 2048:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 0x20 for character in candidate)
    ):
        return None
    return candidate


def _allowed_aou_preferred_url(value: Any) -> str | None:
    candidate = _safe_https_url(value)
    if candidate is None:
        return None
    try:
        hostname = urlsplit(candidate).hostname
    except ValueError:
        return None
    return candidate if hostname in _AOU_PREFERRED_LINK_HOSTS else None


def _aou_variable_class(row: Mapping[str, Any]) -> str | None:
    item_kind = _optional_text(row.get("item_kind"))
    if item_kind == "omop_concept" and _optional_text(
        row.get("program_measurement")
    ) == "true":
        return "physical_measurement_variable"
    return {
        "omop_concept": "ehr_concept_variable",
        "survey_variable": "survey_variable",
        "program_measurement_definition": "physical_measurement_variable",
        "fitbit_metric": "fitbit_variable",
    }.get(item_kind)


def _sanitize_aou_detail_links(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in {"url", "variable_preferred_url"}:
                value[key] = _safe_https_url(item)
            else:
                _sanitize_aou_detail_links(item)
    elif isinstance(value, list):
        for item in value:
            _sanitize_aou_detail_links(item)


def _provenance(source_system: str, source_url: str | None) -> Provenance:
    return {
        "source_system": source_system,
        "source_url": source_url,
        "snapshot_id": None,
        "fetched_at": None,
        "checksum": None,
        "status": "source_locator_only",
    }


def _retrieval_metadata(
    *,
    method: str,
    effective_method: str,
    embedding_used: bool,
    keyword_weight: float | None,
    semantic_weight: float | None,
    degraded_reason: str | None,
) -> JsonObject:
    return {
        "method": method,
        "effective_method": effective_method,
        "embedding_provider_used": embedding_used,
        "keyword_weight": keyword_weight,
        "semantic_weight": semantic_weight,
        "degraded": effective_method != method,
        "degraded_reason": degraded_reason,
        "scores_calibrated": False,
        "scores_cross_query_comparable": False,
    }


def _scores(row: Mapping[str, Any]) -> JsonObject:
    combined = row.get("score")
    if combined is None:
        combined = row.get("combined_score")
    return {
        "keyword": _optional_float(row.get("keyword_score")),
        "semantic": _optional_float(row.get("semantic_score")),
        "combined": _optional_float(combined),
        "alias_boost": _optional_float(row.get("alias_boost")),
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_value(value: Any) -> Any:
    """Convert common DB-returned values into JSON-safe primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    return str(value)


def _bounded_text(value: Any, maximum: int) -> str | None:
    text = _optional_text(value)
    if text is None or len(text) <= maximum:
        return text
    return text[: max(0, maximum - 3)].rstrip() + "..."


def _bounded_json(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 6,
    max_items: int = 100,
    max_string: int = 4000,
) -> Any:
    """Convert database metadata to JSON while bounding MCP response growth."""
    if depth >= max_depth:
        return "[nested metadata omitted]"
    if isinstance(value, str):
        return _bounded_text(value, max_string)
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for item in value[:max_items]
        ]
    return _json_value(value)
