from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest
from pglast import parse_sql

from querygap_mcp import aou_repository, repository


class CapturingCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        self.executions.append((statement, params))

    def fetchone(self):
        return None

    def fetchall(self) -> list[dict]:
        return []


def test_all_fixed_repository_queries_parse_and_bind(monkeypatch) -> None:
    cursor = CapturingCursor()

    @contextmanager
    def capture() -> Iterator[CapturingCursor]:
        yield cursor

    monkeypatch.setattr(repository, "read_cursor", capture)
    study_id = "phs000007.v35.p16"
    vector = [0.0] * 1536

    operations = [
        lambda: repository.resolve_study_accession(study_id),
        lambda: repository.get_study_metadata(study_id),
        lambda: repository.search_studies("heart"),
        *[
            lambda method=method: repository.search_variables_flexible(
                "blood",
                vector if method != "keyword" else None,
                method,
                study_id,
                include_stats=True,
            )
            for method in ("keyword", "semantic", "hybrid")
        ],
        *[
            lambda method=method: repository.search_datasets_flexible(
                "blood",
                vector if method != "keyword" else None,
                method,
                study_id,
            )
            for method in ("keyword", "semantic", "hybrid")
        ],
        *[
            lambda method=method: repository.search_documents_flexible(
                "protocol",
                vector if method != "keyword" else None,
                method,
                study_id,
            )
            for method in ("keyword", "semantic", "hybrid")
        ],
        lambda: repository.search_ukb_fields_keyword("kidney"),
        lambda: repository.search_ukb_fields_hybrid("kidney", vector),
        lambda: repository.get_ukb_field_details(21022),
        lambda: repository.get_ukb_field_instance_summaries(21022),
    ]

    for operation in operations:
        operation()

    assert len(cursor.executions) == 16
    for statement, params in cursor.executions:
        assert statement.count("%s") == len(params or ())
        parse_sql(statement.replace("%s", "NULL"))


def test_aou_fixed_repository_queries_parse_and_bind(monkeypatch) -> None:
    class AouCapturingCursor(CapturingCursor):
        description: list = []

        def fetchone(self):
            return ("aou.snapshot.test",)

    cursor = AouCapturingCursor()

    @contextmanager
    def capture() -> Iterator[AouCapturingCursor]:
        yield cursor

    monkeypatch.setattr(aou_repository, "read_cursor", capture)
    vector = [0.0] * 1536

    aou_repository.search_aou_catalog(
        query="diastolic blood pressure",
        query_embedding=None,
        method="keyword",
        limit=10,
    )
    aou_repository.search_aou_catalog(
        query="diastolic blood pressure",
        query_embedding=vector,
        method="hybrid",
        limit=10,
        variable_type="ehr",
        ehr_domain="Measurement",
        ehr_role="standard",
        ehr_vocabulary="LOINC",
    )
    aou_repository.search_aou_catalog(
        query="diastolic blood pressure",
        query_embedding=vector,
        method="semantic",
        limit=10,
    )
    aou_repository.get_aou_item("aou.doc.0123456789abcdef01234567")

    assert cursor.executions
    for statement, params in cursor.executions:
        assert statement.count("%s") == len(params or ())
        parse_sql(statement.replace("%s", "NULL"))

    filtered_statements = [
        statement
        for statement, params in cursor.executions
        if params and "requested_filters" in statement
    ]
    assert filtered_statements
    for statement in filtered_statements:
        compact = " ".join(statement.split()).lower()
        assert "join aou.items as item" in compact
        assert "filters.variable_type" in compact
        assert "filters.ehr_domain" in compact
        assert "filters.ehr_role" in compact
        assert "filters.ehr_vocabulary" in compact


def test_aou_repository_rejects_ehr_filters_without_ehr_type() -> None:
    with pytest.raises(ValueError, match="require variable_type='ehr'"):
        aou_repository.search_aou_catalog(
            query="blood pressure",
            query_embedding=None,
            method="keyword",
            limit=10,
            ehr_domain="Measurement",
        )
