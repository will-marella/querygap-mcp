from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone

import pytest

from querygap_mcp.quota import EmbeddingBudgetExceeded, SQLiteDailyEmbeddingBudget


def test_sqlite_daily_budget_is_durable_and_resets_by_utc_day(tmp_path) -> None:
    current = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    path = tmp_path / "budget.sqlite"
    budget = SQLiteDailyEmbeddingBudget(path, 2, now=lambda: current)

    budget.acquire()
    budget.acquire()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(EmbeddingBudgetExceeded):
        SQLiteDailyEmbeddingBudget(path, 2, now=lambda: current).acquire()

    next_day = current + timedelta(days=1)
    SQLiteDailyEmbeddingBudget(path, 2, now=lambda: next_day).acquire()


def test_availability_check_does_not_consume_daily_budget(tmp_path) -> None:
    current = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    budget = SQLiteDailyEmbeddingBudget(
        tmp_path / "budget.sqlite",
        1,
        now=lambda: current,
    )

    budget.check_available()
    budget.check_available()
    budget.acquire()

    with pytest.raises(EmbeddingBudgetExceeded):
        budget.check_available()


def test_budget_store_failure_is_fail_closed(tmp_path) -> None:
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    budget = SQLiteDailyEmbeddingBudget(directory, 1)

    with pytest.raises(EmbeddingBudgetExceeded):
        budget.acquire()


def test_environment_budget_requires_bounded_limit_and_absolute_path(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("QG_MCP_EMBEDDING_DAILY_LIMIT", "10")
    monkeypatch.setenv("QG_MCP_EMBEDDING_BUDGET_PATH", str(tmp_path / "budget.sqlite"))

    budget = SQLiteDailyEmbeddingBudget.from_environment()

    assert budget is not None
    assert budget.daily_limit == 10
