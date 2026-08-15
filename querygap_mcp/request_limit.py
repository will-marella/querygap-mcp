"""Durable, privacy-free request limits for the hosted MCP boundary."""

from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol


class RequestLimitExceeded(RuntimeError):
    """Raised when accepting a request would exceed a configured bucket."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("The hosted MCP request limit is exhausted.")
        self.retry_after = max(1, retry_after)


class RequestLimitUnavailable(RuntimeError):
    """Raised when the durable counter cannot safely account for a request."""


class RequestLimiter(Protocol):
    """Account for a request without receiving any request or client data."""

    def acquire(self) -> None: ...

    def check_available(self) -> None: ...


@dataclass(frozen=True)
class SQLiteRequestLimiter:
    """Atomic global minute/day counters for one service volume.

    The store contains only UTC bucket identifiers and aggregate counts. No IP,
    query, header, method, path, body, or request identifier reaches this API.
    """

    path: Path
    requests_per_minute: int
    daily_limit: int
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("request budget path must be absolute")
        if not 1 <= self.requests_per_minute <= 10_000:
            raise ValueError("requests_per_minute must be between 1 and 10000")
        if not 1 <= self.daily_limit <= 10_000_000:
            raise ValueError("daily_limit must be between 1 and 10000000")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            database.execute("PRAGMA busy_timeout = 2000")
            database.execute(
                "CREATE TABLE IF NOT EXISTS request_usage ("
                "period TEXT NOT NULL, bucket TEXT NOT NULL, "
                "requests INTEGER NOT NULL, PRIMARY KEY(period, bucket))"
            )
        except BaseException:
            database.close()
            raise
        return database

    def check_available(self) -> None:
        """Verify that the durable store is writable without consuming quota."""
        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                database.execute("ROLLBACK")
        except (OSError, sqlite3.Error):
            raise RequestLimitUnavailable(
                "The hosted MCP request counter is unavailable."
            ) from None

    def acquire(self) -> None:
        current = self.now().astimezone(UTC)
        minute_start = current.replace(second=0, microsecond=0)
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        minute_bucket = minute_start.isoformat()
        day_bucket = day_start.date().isoformat()

        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                minute_row = database.execute(
                    "SELECT requests FROM request_usage "
                    "WHERE period = 'minute' AND bucket = ?",
                    (minute_bucket,),
                ).fetchone()
                day_row = database.execute(
                    "SELECT requests FROM request_usage "
                    "WHERE period = 'day' AND bucket = ?",
                    (day_bucket,),
                ).fetchone()

                retry_after: list[int] = []
                if minute_row is not None and int(minute_row[0]) >= (
                    self.requests_per_minute
                ):
                    retry_after.append(
                        math.ceil(
                            (
                                minute_start + timedelta(minutes=1) - current
                            ).total_seconds()
                        )
                    )
                if day_row is not None and int(day_row[0]) >= self.daily_limit:
                    retry_after.append(
                        math.ceil(
                            (day_start + timedelta(days=1) - current).total_seconds()
                        )
                    )
                if retry_after:
                    database.execute("ROLLBACK")
                    raise RequestLimitExceeded(max(retry_after))

                database.execute(
                    "INSERT INTO request_usage(period, bucket, requests) "
                    "VALUES ('minute', ?, 1) "
                    "ON CONFLICT(period, bucket) DO UPDATE "
                    "SET requests = requests + 1",
                    (minute_bucket,),
                )
                database.execute(
                    "INSERT INTO request_usage(period, bucket, requests) "
                    "VALUES ('day', ?, 1) "
                    "ON CONFLICT(period, bucket) DO UPDATE "
                    "SET requests = requests + 1",
                    (day_bucket,),
                )
                database.execute(
                    "DELETE FROM request_usage WHERE "
                    "(period = 'minute' AND bucket <> ?) OR "
                    "(period = 'day' AND bucket <> ?)",
                    (minute_bucket, day_bucket),
                )
                database.execute("COMMIT")
        except RequestLimitExceeded:
            raise
        except (OSError, sqlite3.Error):
            raise RequestLimitUnavailable(
                "The hosted MCP request counter is unavailable."
            ) from None


__all__ = [
    "RequestLimiter",
    "RequestLimitExceeded",
    "RequestLimitUnavailable",
    "SQLiteRequestLimiter",
]
