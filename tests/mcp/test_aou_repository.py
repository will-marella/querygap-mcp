from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from querygap_mcp import aou_repository


class EmptyCursor:
    pass


def test_standard_ehr_records_receive_no_unconditional_rank_boost(monkeypatch) -> None:
    cursor = EmptyCursor()

    @contextmanager
    def read_cursor() -> Iterator[EmptyCursor]:
        yield cursor

    monkeypatch.setattr(aou_repository, "read_cursor", read_cursor)
    monkeypatch.setattr(aou_repository, "_active_snapshot", lambda _cursor: "snapshot")
    monkeypatch.setattr(aou_repository, "_exact_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        aou_repository,
        "_keyword_candidates",
        lambda *args, **kwargs: [
            {
                "doc_key": "survey",
                "lexical_score": 1.0,
                "title_similarity": 0.9,
            },
            {
                "doc_key": "ehr",
                "lexical_score": 0.5,
                "title_similarity": 0.2,
            },
        ],
    )
    monkeypatch.setattr(aou_repository, "_semantic_candidates", lambda *args, **kwargs: [])
    documents: dict[str, dict[str, Any]] = {
        "survey": {
            "item_kind": "survey_variable",
            "title": "Feeling nervous, anxious, or on edge",
            "domain_id": None,
        },
        "ehr": {
            "item_kind": "omop_concept",
            "ehr_variable_role": "standard",
            "title": "Unrelated standard EHR concept",
            "domain_id": "Procedure",
        },
    }
    monkeypatch.setattr(
        aou_repository,
        "_document_summaries",
        lambda _cursor, _doc_keys: documents,
    )
    monkeypatch.setattr(aou_repository, "_intent_boost", lambda *args: 0.0)

    results = aou_repository.search_aou_catalog(
        query="natural language question",
        query_embedding=None,
        method="keyword",
        limit=2,
    )

    assert [result["title"] for result in results] == [
        "Feeling nervous, anxious, or on edge",
        "Unrelated standard EHR concept",
    ]
