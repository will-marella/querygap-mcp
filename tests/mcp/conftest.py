from __future__ import annotations

from typing import Any

import pytest

from querygap_mcp.service import QueryGaPRetrievalService, RetrievalDependencies


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def function(self, name: str, result: Any):
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if isinstance(result, Exception):
                raise result
            return result

        return call


def make_dependencies(
    recorder: Recorder,
    **overrides: Any,
) -> RetrievalDependencies:
    defaults: dict[str, Any] = {
        "search_studies": recorder.function(
            "search_studies",
            [
                {
                    "study_id": "phs000007.v35.p16",
                    "name": "Framingham Cohort",
                    "dataset_count": 42,
                    "variable_count": 1234,
                }
            ],
        ),
        "get_study_metadata": recorder.function(
            "get_study_metadata",
            {
                "metadata": {
                    "study_name": "Framingham Cohort",
                    "description": "Heart study",
                },
                "metadata_scope": "exact",
                "metadata_source_study_id": "phs000007.v35.p16",
            },
        ),
        "search_variables": recorder.function(
            "search_variables",
            [
                {
                    "variable_id": "phv000001.v1",
                    "dataset_id": "pht000001.v1",
                    "study_id": "phs000007.v35.p16",
                    "name": "Systolic blood pressure",
                    "description": "Measured blood pressure",
                    "keyword_score": 0.9,
                    "semantic_score": 0.8,
                    "score": 0.83,
                    "stat_n": 100,
                }
            ],
        ),
        "search_datasets": recorder.function("search_datasets", []),
        "search_documents": recorder.function("search_documents", []),
        "search_ukb_keyword": recorder.function(
            "search_ukb_keyword",
            [
                {
                    "field_id": 21022,
                    "title": "Age at recruitment",
                    "notes": "Age in years",
                    "source_url": "https://biobank.ndph.ox.ac.uk/ukb/field.cgi?id=21022",
                    "score": 1.0,
                }
            ],
        ),
        "search_ukb_hybrid": recorder.function("search_ukb_hybrid", []),
        "get_ukb_field_details": recorder.function(
            "get_ukb_field_details",
            {
                "field_id": 21022,
                "title": "Age at recruitment",
                "notes": "Age in years",
                "category_url": "https://biobank.ndph.ox.ac.uk/ukb/label.cgi?id=100",
                "encoding_url": "https://evil.invalid/injected",
            },
        ),
        "get_ukb_field_instance_summaries": recorder.function(
            "get_ukb_field_instance_summaries", {"instances": []}
        ),
        "normalize_study_query_alias": recorder.function(
            "normalize_study_query_alias", "Framingham Heart Study"
        ),
        "resolve_study_accession": recorder.function(
            "resolve_study_accession",
            {
                "study_id": "phs000007.v35.p16",
                "name": "Framingham Cohort",
                "dataset_count": 42,
                "variable_count": 1234,
            },
        ),
        "search_aou": recorder.function(
            "search_aou",
            [
                {
                    "doc_key": "aou.doc.0123456789abcdef01234567",
                    "search_role": "primary",
                    "item_kind": "omop_concept",
                    "title": "Diastolic blood pressure",
                    "subtitle": "LOINC 8462-4",
                    "domain_id": "Measurement",
                    "vocabulary_id": "LOINC",
                    "concept_code": "8462-4",
                    "ehr_variable_role": "standard",
                    "preferred_url": "https://databrowser.researchallofus.org/ehr/measurement/123",
                    "preferred_link_specificity": "concept",
                    "match_reasons": ["lexical", "semantic"],
                    "semantic_score": 0.8,
                    "score": 0.04,
                }
            ],
        ),
        "get_aou_details": recorder.function(
            "get_aou_details",
            {
                "doc_key": "aou.doc.0123456789abcdef01234567",
                "search_role": "primary",
                "item_kind": "omop_concept",
                "title": "Diastolic blood pressure",
                "domain_id": "Measurement",
                "vocabulary_id": "LOINC",
                "concept_code": "8462-4",
                "preferred_url": "https://databrowser.researchallofus.org/ehr/measurement/123",
                "details": {
                    "identifiers": [
                        {
                            "identifier_system": "loinc",
                            "identifier_value": "8462-4",
                        }
                    ],
                    "links": [
                        {
                            "url": "https://databrowser.researchallofus.org/ehr/measurement/123"
                        },
                        {"url": "javascript:alert(1)"},
                    ],
                    "relationships": [],
                },
            },
        ),
    }
    defaults.update(overrides)
    return RetrievalDependencies(**defaults)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def service(recorder: Recorder) -> QueryGaPRetrievalService:
    return QueryGaPRetrievalService(
        make_dependencies(recorder),
        embedding_provider=lambda _query: [0.0] * 1536,
    )
