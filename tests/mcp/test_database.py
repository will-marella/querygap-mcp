from __future__ import annotations

from pathlib import Path

import pytest

from querygap_mcp import database


def test_package_has_no_application_database_or_dotenv_imports() -> None:
    package = Path(__file__).resolve().parents[2] / "querygap_mcp"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package.glob("*.py")
    )

    assert "from db" not in source
    assert "import db" not in source
    assert "load_dotenv" not in source
    assert 'os.getenv("DATABASE_URL")' not in source
    assert 'os.environ.get("DATABASE_URL")' not in source


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.closed = False

    def execute(self, statement: str, params: object = None) -> None:
        self.statements.append((statement, params))

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_value = FakeCursor()
        self.rollback_count = 0

    def cursor(self, **_kwargs: object) -> FakeCursor:
        return self.cursor_value

    def rollback(self) -> None:
        self.rollback_count += 1


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.returned: list[tuple[FakeConnection, bool]] = []

    def getconn(self) -> FakeConnection:
        return self.connection

    def putconn(self, connection: FakeConnection, close: bool = False) -> None:
        self.returned.append((connection, close))


def test_database_url_is_mcp_only_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QG_MCP_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-be-used.invalid/app")

    with pytest.raises(database.DatabaseConfigurationError, match="QG_MCP_DATABASE_URL"):
        database.settings_from_environment()


def test_pool_receives_only_dedicated_url_and_small_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_constructor(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("QG_MCP_DATABASE_URL", "postgresql://mcp.invalid/catalog")
    monkeypatch.setenv("DATABASE_URL", "postgresql://application.invalid/app")
    monkeypatch.setattr(
        database.psycopg2.pool,
        "ThreadedConnectionPool",
        fake_constructor,
    )

    settings = database.settings_from_environment()
    database._create_pool(settings)

    assert captured["dsn"] == "postgresql://mcp.invalid/catalog"
    assert captured["minconn"] == 1
    assert captured["maxconn"] == 4
    assert captured["options"] == "-c default_transaction_read_only=on"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QG_MCP_DB_POOL_MAX", "100"),
        ("QG_MCP_DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("QG_MCP_DB_STATEMENT_TIMEOUT_MS", "unbounded"),
        ("QG_MCP_DB_LOCK_TIMEOUT_MS", "6000"),
    ],
)
def test_unsafe_database_bounds_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("QG_MCP_DATABASE_URL", "postgresql://mcp.invalid/catalog")
    monkeypatch.setenv(name, value)

    with pytest.raises(database.DatabaseConfigurationError):
        database.settings_from_environment()


def test_read_cursor_applies_guards_and_always_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pool = FakePool()
    monkeypatch.setenv("QG_MCP_DATABASE_URL", "postgresql://mcp.invalid/catalog")
    monkeypatch.setenv("QG_MCP_DB_STATEMENT_TIMEOUT_MS", "4321")
    monkeypatch.setenv("QG_MCP_DB_LOCK_TIMEOUT_MS", "321")
    monkeypatch.setattr(database, "_pool", fake_pool)

    with pytest.raises(RuntimeError, match="stop"):
        with database.read_cursor() as cursor:
            assert cursor is fake_pool.connection.cursor_value
            raise RuntimeError("stop")

    statements = [statement for statement, _ in fake_pool.connection.cursor_value.statements]
    assert statements == [
        "BEGIN READ ONLY",
        "SET LOCAL search_path = pg_catalog, public",
        "SET LOCAL statement_timeout = '4321ms'",
        "SET LOCAL lock_timeout = '321ms'",
    ]
    assert fake_pool.connection.rollback_count == 1
    assert fake_pool.connection.cursor_value.closed is True
    assert fake_pool.returned == [(fake_pool.connection, False)]
