from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from querygap_mcp import repository


class FakeCursor:
    def __init__(
        self,
        *,
        one: dict[str, Any] | None = None,
        many: list[dict[str, Any]] | None = None,
    ) -> None:
        self.one = one
        self.many = many or []
        self.executions: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        self.executions.append((statement, params))

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


def install_cursor(monkeypatch: pytest.MonkeyPatch, cursor: FakeCursor) -> None:
    @contextmanager
    def fake_read_cursor() -> Iterator[FakeCursor]:
        yield cursor

    monkeypatch.setattr(repository, "read_cursor", fake_read_cursor)


def compact_sql(statement: str) -> str:
    return " ".join(statement.split()).lower()


@pytest.mark.parametrize("query", ["FHS", "Framingham Heart Study"])
def test_study_aliases_match_the_catalogs_canonical_framingham_name(
    query: str,
) -> None:
    assert repository.normalize_study_query_alias(query) == "Framingham Cohort"


def test_study_resolution_ranks_exact_name_above_prefix_and_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(many=[])
    install_cursor(monkeypatch, cursor)

    repository.search_studies("Framingham Cohort", limit=6)

    statement, params = cursor.executions[0]
    sql = compact_sql(statement)
    exact_rank = "= lower(btrim(%s)) then 4"
    prefix_rank = "ilike %s then 3"
    assert exact_rank in sql
    assert sql.index(exact_rank) < sql.index(prefix_rank)
    assert params == (
        "Framingham Cohort",
        "Framingham Cohort%",
        "%Framingham Cohort%",
        "%Framingham Cohort%",
        "Framingham Cohort",
        6,
    )


def test_metadata_fallback_is_labelled_and_gated_by_exact_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(
        one={
            "metadata": {"study_name": "Base metadata"},
            "metadata_scope": "base",
            "metadata_source_study_id": "phs000007",
        }
    )
    install_cursor(monkeypatch, cursor)

    result = repository.get_study_metadata("phs000007.v35.p16")

    assert result == {
        "metadata": {"study_name": "Base metadata"},
        "metadata_scope": "base",
        "metadata_source_study_id": "phs000007",
    }
    statement, params = cursor.executions[0]
    sql = compact_sql(statement)
    assert "exists (select 1 from studies where study_id = %s)" in sql
    assert "exists (select 1 from datasets where study_id = %s)" in sql
    assert "exists (select 1 from variables where study_id = %s)" in sql
    assert "selected.study_id in (%s, %s)" in sql
    assert params == (
        "phs000007.v35.p16",
        "phs000007.v35.p16",
        "phs000007",
        "phs000007.v35.p16",
        "phs000007.v35.p16",
        "phs000007.v35.p16",
        "phs000007.v35.p16",
    )


def test_variable_statistics_join_uses_variable_and_exact_study(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(
        many=[
            {
                "variable_id": "phv000001.v1",
                "study_id": "phs000007.v35.p16",
                "score": 0.5,
                "stat_n": 100,
            }
        ]
    )
    install_cursor(monkeypatch, cursor)

    rows = repository.search_variables_flexible(
        query="blood pressure",
        method="keyword",
        study_id="phs000007.v35.p16",
        limit=10,
        include_stats=True,
    )

    assert rows[0]["stat_n"] == 100
    statement, params = cursor.executions[0]
    sql = compact_sql(statement)
    assert "left join variable_reports vr" in sql
    assert "vr.variable_id = v.variable_id" in sql
    assert "vr.study_id = v.study_id" in sql
    assert "where v.study_id = %s" in sql
    assert params == (
        "blood pressure",
        "phs000007.v35.p16",
        "blood pressure",
        10,
    )


def test_repository_rejects_base_study_scope_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    @contextmanager
    def forbidden_cursor() -> Iterator[FakeCursor]:
        nonlocal called
        called = True
        yield FakeCursor()

    monkeypatch.setattr(repository, "read_cursor", forbidden_cursor)

    with pytest.raises(ValueError, match="full versioned"):
        repository.search_documents_flexible(
            query="protocol",
            method="keyword",
            study_id="phs000007",
        )

    assert called is False


def test_semantic_vectors_are_bounded_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    @contextmanager
    def forbidden_cursor() -> Iterator[FakeCursor]:
        nonlocal called
        called = True
        yield FakeCursor()

    monkeypatch.setattr(repository, "read_cursor", forbidden_cursor)

    with pytest.raises(ValueError, match="1536"):
        repository.search_datasets_flexible(
            query="blood pressure",
            query_embedding=[0.0, 1.0],
            method="semantic",
            study_id="phs000007.v35.p16",
        )

    assert called is False
