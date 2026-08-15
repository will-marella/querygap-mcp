"""Durable, query-free embedding budget controls for a single hosted replica."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol


class EmbeddingBudgetExceeded(RuntimeError):
    """Raised before provider egress when the configured daily cap is spent."""


class EmbeddingBudget(Protocol):
    """Inspect or reserve provider capacity without receiving query text."""

    def acquire(self) -> None: ...

    def check_available(self) -> None: ...


@dataclass(frozen=True)
class SQLiteDailyEmbeddingBudget:
    """Atomic UTC-day counter suitable for one Railway service and volume."""

    path: Path
    daily_limit: int
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if self.daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        if not self.path.is_absolute():
            raise ValueError("embedding budget path must be absolute")

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        day = self.now().astimezone(timezone.utc).date().isoformat()
        try:
            with sqlite3.connect(self.path, timeout=2.0, isolation_level=None) as db:
                os.chmod(self.path, 0o600)
                db.execute("PRAGMA busy_timeout = 2000")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS daily_embedding_usage "
                    "(day TEXT PRIMARY KEY, calls INTEGER NOT NULL)"
                )
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT calls FROM daily_embedding_usage WHERE day = ?",
                    (day,),
                ).fetchone()
                calls = int(row[0]) if row else 0
                if calls >= self.daily_limit:
                    db.execute("ROLLBACK")
                    raise EmbeddingBudgetExceeded(
                        "The daily query-embedding budget is exhausted."
                    )
                db.execute(
                    "INSERT INTO daily_embedding_usage(day, calls) VALUES (?, 1) "
                    "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
                    (day,),
                )
                db.execute("COMMIT")
        except EmbeddingBudgetExceeded:
            raise
        except (OSError, sqlite3.Error):
            # A broken budget store must never fail open into provider spend.
            raise EmbeddingBudgetExceeded(
                "The query-embedding budget store is unavailable."
            ) from None

    def check_available(self) -> None:
        """Fail when spent without incrementing the durable usage counter."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        day = self.now().astimezone(timezone.utc).date().isoformat()
        try:
            with sqlite3.connect(self.path, timeout=2.0) as db:
                os.chmod(self.path, 0o600)
                db.execute("PRAGMA busy_timeout = 2000")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS daily_embedding_usage "
                    "(day TEXT PRIMARY KEY, calls INTEGER NOT NULL)"
                )
                row = db.execute(
                    "SELECT calls FROM daily_embedding_usage WHERE day = ?",
                    (day,),
                ).fetchone()
                calls = int(row[0]) if row else 0
                if calls >= self.daily_limit:
                    raise EmbeddingBudgetExceeded(
                        "The daily query-embedding budget is exhausted."
                    )
        except EmbeddingBudgetExceeded:
            raise
        except (OSError, sqlite3.Error):
            raise EmbeddingBudgetExceeded(
                "The query-embedding budget store is unavailable."
            ) from None

    @classmethod
    def from_environment(cls) -> SQLiteDailyEmbeddingBudget | None:
        raw_limit = os.getenv("QG_MCP_EMBEDDING_DAILY_LIMIT", "").strip()
        if not raw_limit:
            return None
        try:
            daily_limit = int(raw_limit)
        except ValueError:
            raise ValueError("QG_MCP_EMBEDDING_DAILY_LIMIT must be an integer") from None
        if not 1 <= daily_limit <= 1_000_000:
            raise ValueError(
                "QG_MCP_EMBEDDING_DAILY_LIMIT must be between 1 and 1000000"
            )
        raw_path = os.getenv("QG_MCP_EMBEDDING_BUDGET_PATH", "").strip()
        if not raw_path:
            raise ValueError(
                "QG_MCP_EMBEDDING_BUDGET_PATH is required when a daily limit is set"
            )
        return cls(path=Path(raw_path), daily_limit=daily_limit)


__all__ = [
    "EmbeddingBudget",
    "EmbeddingBudgetExceeded",
    "SQLiteDailyEmbeddingBudget",
]
