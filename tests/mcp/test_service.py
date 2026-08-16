from __future__ import annotations

import json

import pytest

from querygap_mcp.contracts import ServiceError
from querygap_mcp.quota import EmbeddingBudgetExceeded
from querygap_mcp.service import QueryGaPRetrievalService

from conftest import Recorder, make_dependencies


def test_resolve_name_normalizes_and_preserves_candidates(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(make_dependencies(recorder))

    result = service.resolve_dbgap_study("FHS")

    assert result["normalized_query"] == "Framingham Heart Study"
    assert result["recommended"]["study_id"] == "phs000007.v35.p16"
    assert result["resolution"]["authoritative"] is False
    search_call = next(call for call in recorder.calls if call[0] == "search_studies")
    assert search_call[2] == {"query": "Framingham Heart Study", "limit": 6}


def test_resolve_exact_accession_uses_fixed_accession_lookup(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(make_dependencies(recorder))

    result = service.resolve_dbgap_study(
        "https://www.ncbi.nlm.nih.gov/gap/?study=phs000007.v35.p16"
    )

    assert result["recommended"]["study_id"] == "phs000007.v35.p16"
    assert any(call[0] == "resolve_study_accession" for call in recorder.calls)
    assert not any(call[0] == "search_studies" for call in recorder.calls)


def test_keyword_catalog_search_never_calls_embedding_provider(recorder: Recorder) -> None:
    def fail_if_called(_query: str) -> list[float]:
        raise AssertionError("embedding provider should not be called")

    service = QueryGaPRetrievalService(
        make_dependencies(recorder), embedding_provider=fail_if_called
    )
    result = service.search_dbgap_catalog(
        study_id="phs000007.v35.p16",
        kind="variable",
        query="blood pressure",
        method="keyword",
        include_stats=True,
    )

    assert result["retrieval"]["embedding_provider_used"] is False
    assert result["items"][0]["summary_statistics"]["n"] == 100
    call = next(call for call in recorder.calls if call[0] == "search_variables")
    assert call[2]["query_embedding"] is None
    assert call[2]["include_stats"] is True


def test_base_scoped_study_metadata_is_disclosed(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(
        make_dependencies(
            recorder,
            get_study_metadata=recorder.function(
                "get_study_metadata",
                {
                    "metadata": {"study_name": "Framingham Cohort"},
                    "metadata_scope": "base",
                    "metadata_source_study_id": "phs000007",
                },
            ),
        )
    )

    result = service.get_dbgap_study("phs000007.v35.p16")

    assert result["study"]["metadata_scope"] == "base"
    assert result["study"]["metadata_source_study_id"] == "phs000007"
    assert any("base-scoped" in warning for warning in result["warnings"])


def test_hybrid_search_passes_validated_embedding(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(
        make_dependencies(recorder), embedding_provider=lambda _query: [0.25] * 1536
    )

    result = service.search_dbgap_catalog(
        study_id="phs000007.v35.p16",
        kind="variable",
        query="blood pressure",
        method="hybrid",
    )

    call = next(call for call in recorder.calls if call[0] == "search_variables")
    assert len(call[2]["query_embedding"]) == 1536
    assert result["retrieval"] == {
        "method": "hybrid",
        "effective_method": "hybrid",
        "embedding_provider_used": True,
        "keyword_weight": 0.3,
        "semantic_weight": 0.7,
        "degraded": False,
        "degraded_reason": None,
        "scores_calibrated": False,
        "scores_cross_query_comparable": False,
    }


@pytest.mark.parametrize(
    ("provider_error", "reason"),
    [
        (RuntimeError("provider down"), "embedding_provider_unavailable"),
        (EmbeddingBudgetExceeded("spent"), "embedding_budget_exhausted"),
    ],
)
def test_hybrid_catalog_search_falls_back_to_keyword(
    recorder: Recorder, provider_error: Exception, reason: str
) -> None:
    def unavailable(_query: str) -> list[float]:
        raise provider_error

    service = QueryGaPRetrievalService(
        make_dependencies(recorder), embedding_provider=unavailable
    )

    result = service.search_dbgap_catalog(
        study_id="phs000007.v35.p16",
        kind="variable",
        query="blood pressure",
        method="hybrid",
    )

    call = next(call for call in recorder.calls if call[0] == "search_variables")
    assert call[2]["method"] == "keyword"
    assert call[2]["query_embedding"] is None
    assert result["retrieval"]["method"] == "hybrid"
    assert result["retrieval"]["effective_method"] == "keyword"
    assert result["retrieval"]["degraded"] is True
    assert result["retrieval"]["degraded_reason"] == reason


def test_ukb_hybrid_falls_back_to_keyword(recorder: Recorder) -> None:
    def unavailable(_query: str) -> list[float]:
        raise RuntimeError("provider down")

    service = QueryGaPRetrievalService(
        make_dependencies(recorder), embedding_provider=unavailable
    )

    result = service.search_ukb_fields(query="age", method="hybrid")

    assert any(call[0] == "search_ukb_keyword" for call in recorder.calls)
    assert not any(call[0] == "search_ukb_hybrid" for call in recorder.calls)
    assert result["retrieval"]["effective_method"] == "keyword"


def test_semantic_search_does_not_silently_fallback(recorder: Recorder) -> None:
    def unavailable(_query: str) -> list[float]:
        raise RuntimeError("provider down")

    service = QueryGaPRetrievalService(
        make_dependencies(recorder), embedding_provider=unavailable
    )

    with pytest.raises(ServiceError, match="embedding_provider_unavailable"):
        service.search_dbgap_catalog(
            study_id="phs000007.v35.p16",
            kind="variable",
            query="blood pressure",
            method="semantic",
        )


@pytest.mark.parametrize(
    ("study_id", "kind", "query", "limit"),
    [
        ("phs000007", "variable", "pressure", 10),
        ("phs000007.v35.p16", "unknown", "pressure", 10),
        ("phs000007.v35.p16", "variable", "", 10),
        ("phs000007.v35.p16", "variable", "pressure", 21),
    ],
)
def test_catalog_boundaries(
    service: QueryGaPRetrievalService,
    study_id: str,
    kind: str,
    query: str,
    limit: int,
) -> None:
    with pytest.raises(ServiceError):
        service.search_dbgap_catalog(
            study_id=study_id,
            kind=kind,
            query=query,
            method="keyword",
            limit=limit,
        )


def test_out_of_scope_rows_are_rejected(recorder: Recorder) -> None:
    outside = [
        {
            "variable_id": "phv999999.v1",
            "study_id": "phs999999.v1.p1",
            "name": "Wrong study",
        }
    ]
    service = QueryGaPRetrievalService(
        make_dependencies(
            recorder,
            search_variables=recorder.function("search_variables", outside),
        )
    )

    with pytest.raises(ServiceError, match="retrieval_contract_violation"):
        service.search_dbgap_catalog(
            study_id="phs000007.v35.p16",
            kind="variable",
            query="pressure",
            method="keyword",
        )


def test_dependency_exception_is_sanitized(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(
        make_dependencies(
            recorder,
            search_documents=recorder.function(
                "search_documents", RuntimeError("postgres://secret-host/private")
            ),
        )
    )

    with pytest.raises(ServiceError) as caught:
        service.search_dbgap_catalog(
            study_id="phs000007.v35.p16",
            kind="document",
            query="protocol",
            method="keyword",
        )

    assert caught.value.code == "database_unavailable"
    assert "secret-host" not in str(caught.value)


def test_document_content_and_urls_are_not_overclaimed(recorder: Recorder) -> None:
    rows = [
        {
            "document_id": "phd000123.v1",
            "study_id": "phs000007.v35.p16",
            "name": "Protocol",
            "document_type": "Study protocol",
            "url": "https://evil.invalid/instructions",
            "score": 1,
        }
    ]
    service = QueryGaPRetrievalService(
        make_dependencies(
            recorder,
            search_documents=recorder.function("search_documents", rows),
        )
    )
    result = service.search_dbgap_catalog(
        study_id="phs000007.v35.p16",
        kind="document",
        query="protocol",
        method="keyword",
    )

    item = result["items"][0]
    assert item["content_indexed"] is False
    assert item["indexed_content"] == "title_only"
    assert item["canonical_url"].startswith("https://www.ncbi.nlm.nih.gov/")


def test_ukb_detail_allowlists_links_and_bounds_payload(
    service: QueryGaPRetrievalService,
) -> None:
    result = service.get_ukb_field(21022, include_instance_summaries=True)

    assert result["field"]["category_url"].startswith(
        "https://biobank.ndph.ox.ac.uk/ukb/"
    )
    assert result["field"]["encoding_url"] is None
    assert result["instance_summary"] == {"instances": []}


def test_aou_search_marks_variables_and_preserves_catalog_identity(
    service: QueryGaPRetrievalService,
    recorder: Recorder,
) -> None:
    result = service.search_aou_catalog(
        query="diastolic blood pressure",
        method="hybrid",
        variable_type="ehr",
        ehr_domain="Measurement",
        ehr_role="standard",
        ehr_vocabulary="LOINC",
    )

    item = result["items"][0]
    assert item["id"] == "aou.doc.0123456789abcdef01234567"
    assert item["is_variable"] is True
    assert item["variable_class"] == "ehr_concept_variable"
    assert item["concept_code"] == "8462-4"
    assert item["canonical_url"].startswith(
        "https://databrowser.researchallofus.org/"
    )
    call = next(call for call in recorder.calls if call[0] == "search_aou")
    assert len(call[2]["query_embedding"]) == 1536
    assert call[2]["variable_type"] == "ehr"
    assert call[2]["ehr_domain"] == "Measurement"
    assert call[2]["ehr_role"] == "standard"
    assert call[2]["ehr_vocabulary"] == "LOINC"
    assert result["applied_filters"] == {
        "variable_type": "ehr",
        "ehr_domain": "Measurement",
        "ehr_role": "standard",
        "ehr_vocabulary": "LOINC",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ehr_domain", "Measurement"),
        ("ehr_role", "standard"),
        ("ehr_vocabulary", "LOINC"),
    ],
)
def test_aou_ehr_filters_require_explicit_ehr_variable_type(
    service: QueryGaPRetrievalService,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ServiceError, match="require variable_type='ehr'"):
        service.search_aou_catalog(
            query="blood pressure",
            method="keyword",
            **{field: value},
        )


def test_aou_unfiltered_search_reports_no_applied_filters(
    service: QueryGaPRetrievalService,
) -> None:
    result = service.search_aou_catalog(query="blood pressure", method="keyword")

    assert result["applied_filters"] == {}


def test_aou_hybrid_falls_back_to_keyword(recorder: Recorder) -> None:
    service = QueryGaPRetrievalService(
        make_dependencies(recorder),
        embedding_provider=lambda _query: (_ for _ in ()).throw(RuntimeError("down")),
    )

    result = service.search_aou_catalog(query="highest education", method="hybrid")

    call = next(call for call in recorder.calls if call[0] == "search_aou")
    assert call[2]["method"] == "keyword"
    assert call[2]["query_embedding"] is None
    assert result["retrieval"]["degraded_reason"] == "embedding_provider_unavailable"


def test_aou_detail_is_bounded_and_sanitizes_links(
    service: QueryGaPRetrievalService,
) -> None:
    result = service.get_aou_item("aou.doc.0123456789abcdef01234567")

    assert result["item"]["is_variable"] is True
    assert result["details"]["identifiers"][0]["identifier_value"] == "8462-4"
    assert result["details"]["links"][0]["url"].startswith("https://")
    assert result["details"]["links"][1]["url"] is None


def test_aou_detail_requires_opaque_search_result_id(
    service: QueryGaPRetrievalService,
) -> None:
    with pytest.raises(ServiceError, match="invalid_scope"):
        service.get_aou_item("8462-4")


def test_result_payload_is_bounded(recorder: Recorder) -> None:
    huge = "x" * 100_000
    rows = [
        {
            "variable_id": f"phv{index:06d}.v1",
            "study_id": "phs000007.v35.p16",
            "name": huge,
            "description": huge,
        }
        for index in range(20)
    ]
    service = QueryGaPRetrievalService(
        make_dependencies(
            recorder,
            search_variables=recorder.function("search_variables", rows),
        )
    )
    result = service.search_dbgap_catalog(
        study_id="phs000007.v35.p16",
        kind="variable",
        query="pressure",
        method="keyword",
        limit=20,
    )

    assert len(json.dumps(result).encode()) < 128 * 1024
