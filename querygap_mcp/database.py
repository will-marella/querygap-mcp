"""Fail-closed, read-only PostgreSQL boundary for the standalone MCP server.

This module intentionally does not import the application's database module or
load ``.env`` files.  The MCP process must receive its own database URL and
opens every transaction read-only before repository SQL runs.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg2.pool
from psycopg2.extras import RealDictCursor


class DatabaseConfigurationError(RuntimeError):
    """Raised when the dedicated MCP database configuration is unsafe."""


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str
    pool_min: int
    pool_max: int
    connect_timeout_seconds: int
    statement_timeout_ms: int
    lock_timeout_ms: int


_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _bounded_integer(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DatabaseConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}."
        ) from None
    if not minimum <= value <= maximum:
        raise DatabaseConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}."
        )
    return value


def settings_from_environment() -> DatabaseSettings:
    """Read only MCP-prefixed settings; never fall back to application config."""
    database_url = os.environ.get("QG_MCP_DATABASE_URL", "").strip()
    if not database_url:
        raise DatabaseConfigurationError(
            "QG_MCP_DATABASE_URL is required for repository-backed MCP tools."
        )

    pool_min = _bounded_integer(
        "QG_MCP_DB_POOL_MIN", default=1, minimum=1, maximum=2
    )
    pool_max = _bounded_integer(
        "QG_MCP_DB_POOL_MAX", default=4, minimum=1, maximum=8
    )
    if pool_min > pool_max:
        raise DatabaseConfigurationError(
            "QG_MCP_DB_POOL_MIN cannot exceed QG_MCP_DB_POOL_MAX."
        )

    return DatabaseSettings(
        database_url=database_url,
        pool_min=pool_min,
        pool_max=pool_max,
        connect_timeout_seconds=_bounded_integer(
            "QG_MCP_DB_CONNECT_TIMEOUT_SECONDS",
            default=5,
            minimum=1,
            maximum=15,
        ),
        statement_timeout_ms=_bounded_integer(
            "QG_MCP_DB_STATEMENT_TIMEOUT_MS",
            default=10_000,
            minimum=100,
            maximum=30_000,
        ),
        lock_timeout_ms=_bounded_integer(
            "QG_MCP_DB_LOCK_TIMEOUT_MS",
            default=1_000,
            minimum=1,
            maximum=5_000,
        ),
    )


def _create_pool(settings: DatabaseSettings) -> psycopg2.pool.ThreadedConnectionPool:
    # These libpq options are a second line of defense. Repository transactions
    # still issue explicit SET TRANSACTION/SET LOCAL guards below.
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=settings.pool_min,
        maxconn=settings.pool_max,
        dsn=settings.database_url,
        connect_timeout=settings.connect_timeout_seconds,
        options="-c default_transaction_read_only=on",
    )


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = _create_pool(settings_from_environment())
    return _pool


@contextmanager
def read_cursor() -> Iterator[RealDictCursor]:
    """Yield a guarded cursor and roll its transaction back unconditionally."""
    settings = settings_from_environment()
    pool = _get_pool()
    connection = pool.getconn()
    cursor = None
    close_connection = False
    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        cursor.execute("BEGIN READ ONLY")
        cursor.execute("SET LOCAL search_path = pg_catalog, public")
        # Values are validated bounded integers, so interpolation cannot add SQL.
        cursor.execute(
            f"SET LOCAL statement_timeout = '{settings.statement_timeout_ms}ms'"
        )
        cursor.execute(f"SET LOCAL lock_timeout = '{settings.lock_timeout_ms}ms'")
        yield cursor
    finally:
        try:
            connection.rollback()
        except Exception:
            close_connection = True
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                close_connection = True
        pool.putconn(connection, close=close_connection)


def close_pool() -> None:
    """Close the process-local MCP pool, primarily for graceful shutdown/tests."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.closeall()


__all__ = [
    "DatabaseConfigurationError",
    "DatabaseSettings",
    "close_pool",
    "read_cursor",
    "settings_from_environment",
]
