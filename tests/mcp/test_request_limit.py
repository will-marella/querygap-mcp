from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from querygap_mcp.request_limit import (
    RequestLimitExceeded,
    RequestLimitUnavailable,
    SQLiteRequestLimiter,
)


def test_request_limits_are_durable_and_reset_in_utc(tmp_path) -> None:
    current = datetime(2026, 8, 13, 12, 34, 30, tzinfo=UTC)
    path = tmp_path / "requests.sqlite"
    limiter = SQLiteRequestLimiter(path, 2, 3, now=lambda: current)

    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RequestLimitExceeded) as minute_error:
        SQLiteRequestLimiter(path, 2, 3, now=lambda: current).acquire()
    assert minute_error.value.retry_after == 30

    next_minute = current + timedelta(minutes=1)
    SQLiteRequestLimiter(path, 2, 3, now=lambda: next_minute).acquire()
    with pytest.raises(RequestLimitExceeded) as day_error:
        SQLiteRequestLimiter(path, 2, 3, now=lambda: next_minute).acquire()
    assert day_error.value.retry_after > 60

    next_day = current + timedelta(days=1)
    SQLiteRequestLimiter(path, 2, 3, now=lambda: next_day).acquire()


def test_concurrent_acquire_is_atomic(tmp_path) -> None:
    current = datetime(2026, 8, 13, 12, tzinfo=UTC)
    limiter = SQLiteRequestLimiter(
        tmp_path / "requests.sqlite",
        requests_per_minute=7,
        daily_limit=100,
        now=lambda: current,
    )

    def attempt() -> bool:
        try:
            limiter.acquire()
        except RequestLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = list(executor.map(lambda _index: attempt(), range(20)))

    assert sum(accepted) == 7


def test_store_contains_only_aggregate_utc_buckets(tmp_path) -> None:
    path = tmp_path / "requests.sqlite"
    limiter = SQLiteRequestLimiter(path, 2, 10)

    limiter.check_available()
    limiter.acquire()

    with sqlite3.connect(path) as database:
        columns = {
            row[1] for row in database.execute("PRAGMA table_info(request_usage)")
        }
        rows = database.execute(
            "SELECT period, bucket, requests FROM request_usage ORDER BY period"
        ).fetchall()

    assert columns == {"period", "bucket", "requests"}
    assert {row[0] for row in rows} == {"day", "minute"}
    assert all(row[2] == 1 for row in rows)


def test_store_failure_is_fail_closed(tmp_path) -> None:
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    limiter = SQLiteRequestLimiter(directory, 1, 1)

    with pytest.raises(RequestLimitUnavailable):
        limiter.check_available()
    with pytest.raises(RequestLimitUnavailable):
        limiter.acquire()


@pytest.mark.parametrize(
    ("minute_limit", "daily_limit"),
    [(0, 1), (10_001, 1), (1, 0), (1, 10_000_001)],
)
def test_limits_are_bounded(tmp_path, minute_limit: int, daily_limit: int) -> None:
    with pytest.raises(ValueError):
        SQLiteRequestLimiter(
            tmp_path / "requests.sqlite",
            minute_limit,
            daily_limit,
        )


def test_path_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        SQLiteRequestLimiter(
            path=Path("requests.sqlite"),
            requests_per_minute=1,
            daily_limit=1,
        )
