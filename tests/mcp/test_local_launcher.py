from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from querygap_mcp import local
from querygap_mcp.quota import EmbeddingBudgetExceeded


def _write_secret_file(root: Path, contents: str) -> Path:
    path = root / local.MCP_ENV_FILENAME
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_loader_reads_only_dedicated_file_without_overriding_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "QG_MCP_DATABASE_URL=must-not-load\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    _write_secret_file(
        tmp_path,
        "QG_MCP_DATABASE_URL=from-dedicated-file\n"  # pragma: allowlist secret
        "QG_MCP_EXPECTED_DB_ROLE=querygap_mcp_ro\n"  # pragma: allowlist secret
        "OPENAI_API_KEY='from-dedicated-file'\n",  # pragma: allowlist secret
    )
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    monkeypatch.delenv("QG_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("QG_MCP_EXPECTED_DB_ROLE", raising=False)

    loaded = local.load_local_environment(tmp_path / ".env.mcp.local")

    assert loaded == tmp_path / ".env.mcp.local"
    assert os.environ["QG_MCP_DATABASE_URL"] == "from-dedicated-file"  # pragma: allowlist secret
    assert os.environ["QG_MCP_EXPECTED_DB_ROLE"] == "querygap_mcp_ro"
    assert os.environ["OPENAI_API_KEY"] == "from-shell"  # pragma: allowlist secret


def test_loader_never_falls_back_to_application_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "QG_MCP_DATABASE_URL=must-not-load\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    monkeypatch.delenv("QG_MCP_DATABASE_URL", raising=False)

    with pytest.raises(local.LocalConfigurationError, match="Missing .env.mcp.local"):
        local.load_local_environment(tmp_path / ".env.mcp.local")

    assert "QG_MCP_DATABASE_URL" not in os.environ


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission check")
def test_loader_rejects_group_or_other_access(tmp_path: Path) -> None:
    path = _write_secret_file(tmp_path, "QG_MCP_DATABASE_URL=secret\n")
    path.chmod(0o644)

    with pytest.raises(local.LocalConfigurationError, match="permissions are too broad"):
        local.load_local_environment(path)


def test_loader_rejects_application_settings(tmp_path: Path) -> None:
    _write_secret_file(tmp_path, "DATABASE_URL=must-not-load\n")

    with pytest.raises(local.LocalConfigurationError, match="only QG_MCP"):
        local.load_local_environment(tmp_path / ".env.mcp.local")


def test_loader_rejects_repository_application_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local, "repository_root", lambda: Path("/repo"))

    with pytest.raises(local.LocalConfigurationError, match="application .env"):
        local.load_local_environment(Path("/repo/.env"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink check")
def test_loader_rejects_symlink(tmp_path: Path) -> None:
    target = _write_secret_file(tmp_path, "QG_MCP_DATABASE_URL=secret\n")
    target.rename(tmp_path / "secret-target")
    target.symlink_to(tmp_path / "secret-target")

    with pytest.raises(local.LocalConfigurationError, match="not a symlink"):
        local.load_local_environment(target)


class FakeCursor:
    def __init__(self, responses: list[object], statements: list[str]) -> None:
        self.responses = iter(responses)
        self.statements = statements
        self.current: object = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, _params: object = None) -> None:
        self.statements.append(statement)
        self.current = next(self.responses)

    def fetchone(self) -> object:
        return self.current

    def fetchall(self) -> object:
        return self.current


class FakeConnection:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.statements: list[str] = []
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.responses, self.statements)

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


def _passing_database_responses() -> list[object]:
    columns = [
        ("public", relation, column)
        for relation, names in local._REQUIRED_RELATION_COLUMNS.items()
        for column in names
    ]
    privileges = [
        ("public", relation, True, False)
        for relation in local._REQUIRED_RELATION_COLUMNS
    ]
    indexed_columns = [
        ("public", relation, column)
        for relation, names in local._REQUIRED_INDEXED_COLUMNS.items()
        for column in names
    ]
    return [
        ("on",),
        (
            "querygap_mcp_ro",
            "querygap_mcp_ro",
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            4,
            [
                "default_transaction_read_only=on",
                "statement_timeout=10s",
                "lock_timeout=1s",
                "idle_in_transaction_session_timeout=15s",
                "search_path=pg_catalog, public",
            ],
        ),
        (False,),
        (False, False, False),
        columns,
        [
            ("public", relation, "r")
            for relation in local._REQUIRED_RELATION_COLUMNS
        ],
        indexed_columns,
        privileges,
        None,
        None,
        (0,),
        None,
        None,
        [("vector", False, False), ("pg_trgm", False, False)],
        (True,),
    ]


def _passing_aou_database_responses() -> list[object]:
    responses = _passing_database_responses()
    columns = responses[4] + [
        ("aou", relation, column)
        for relation, names in local._AOU_REQUIRED_RELATION_COLUMNS.items()
        for column in names
    ]
    relation_kinds = responses[5] + [
        ("aou", relation, "r")
        for relation in local._AOU_REQUIRED_RELATION_COLUMNS
    ]
    indexed_columns = responses[6] + [
        ("aou", relation, column)
        for relation, names in local._AOU_REQUIRED_INDEXED_COLUMNS.items()
        for column in names
    ]
    privileges = responses[7] + [
        ("aou", relation, True, False)
        for relation in local._AOU_REQUIRED_RELATION_COLUMNS
    ]
    return [
        *responses[:4],
        (True, False),
        columns,
        relation_kinds,
        indexed_columns,
        [(name,) for name in local._AOU_REQUIRED_INDEX_NAMES],
        (True, True, True, True),
        privileges,
        *responses[8:],
    ]


def test_preflight_checks_database_and_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(_passing_database_responses())
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: lambda _text: [0.0] * local.EMBEDDING_DIMENSIONS,
    )

    report = local.run_preflight()

    assert report.embeddings == "enabled and reachable"
    assert connection.rollback_count == 1
    assert connection.close_count == 1
    executed_sql = "\n".join(connection.statements).lower()
    assert "session_user" in executed_sql
    assert "current_user" in executed_sql
    assert executed_sql.count("has_column_privilege(") == 2
    assert "'select'" in executed_sql
    assert "'insert,update,references'" in executed_sql
    assert "has_sequence_privilege(" in executed_sql
    assert "'select,usage,update'" in executed_sql
    assert "has_database_privilege(current_user, current_database(), 'create')" in executed_sql


def test_preflight_checks_enabled_aou_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(_passing_aou_database_responses())
    monkeypatch.setenv("QG_MCP_AOU_ENABLED", "1")
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    report = local.run_preflight()

    assert report.embeddings == "disabled explicitly; keyword retrieval only"
    executed_sql = "\n".join(connection.statements).lower()
    assert "has_schema_privilege(current_user, 'aou', 'usage')" in executed_sql
    assert "from aou.active_snapshots" in executed_sql


def test_preflight_requires_current_document_embeddings_for_aou_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_aou_database_responses()
    responses[9] = (True, True, False, True)
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_AOU_ENABLED", "1")
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="current embeddings"):
        local.run_preflight()


def test_preflight_warns_but_does_not_fail_on_inherited_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[3] = (False, True, False)
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    report = local.run_preflight()

    assert report.warnings == (
        "Database role inherits TEMP privilege from PUBLIC; the MCP exposes no "
        "arbitrary SQL and keeps connection and statement limits enforced.",
    )


def test_preflight_allows_explicit_keyword_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(_passing_database_responses())
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    report = local.run_preflight()

    assert report.embeddings == "disabled explicitly; keyword retrieval only"


def test_hosted_embedding_probe_treats_spent_budget_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SpentProvider:
        def startup_probe(self, _text: str) -> list[float]:
            raise EmbeddingBudgetExceeded("spent")

    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: SpentProvider(),
    )

    status = local._check_embeddings(consume_budget=False)

    assert status == "daily budget unavailable or exhausted; keyword retrieval only"


def _status_error(
    error_type: type[APIStatusError], status_code: int
) -> APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(status_code, request=request)
    return error_type("provider error", response=response, body=None)


@pytest.mark.parametrize(
    "error",
    [
        APIConnectionError(
            request=httpx.Request("POST", "https://api.openai.com/v1/embeddings")
        ),
        APITimeoutError(
            request=httpx.Request("POST", "https://api.openai.com/v1/embeddings")
        ),
        _status_error(RateLimitError, 429),
        _status_error(InternalServerError, 503),
    ],
)
def test_hosted_embedding_probe_degrades_on_transient_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FailingProvider:
        def startup_probe(self, _text: str) -> list[float]:
            raise error

    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: FailingProvider(),
    )

    status = local._check_embeddings(
        consume_budget=False,
        allow_transient_failure=True,
    )

    assert status == "provider temporarily unavailable; keyword retrieval only"


@pytest.mark.parametrize(
    "error",
    [
        _status_error(AuthenticationError, 401),
        _status_error(PermissionDeniedError, 403),
        _status_error(BadRequestError, 400),
        _status_error(NotFoundError, 404),
    ],
)
def test_hosted_embedding_probe_rejects_fatal_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FailingProvider:
        def startup_probe(self, _text: str) -> list[float]:
            raise error

    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: FailingProvider(),
    )

    with pytest.raises(local.PreflightError, match="provider check failed"):
        local._check_embeddings(
            consume_budget=False,
            allow_transient_failure=True,
        )


def test_hosted_embedding_probe_still_requires_key_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: None,
    )

    with pytest.raises(local.PreflightError, match="OPENAI_API_KEY is absent"):
        local._check_embeddings(
            consume_budget=False,
            allow_transient_failure=True,
        )


def test_local_embedding_preflight_remains_strict_on_transient_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(_passing_database_responses())

    class FailingProvider:
        def __call__(self, _text: str) -> list[float]:
            raise APIConnectionError(
                request=httpx.Request(
                    "POST", "https://api.openai.com/v1/embeddings"
                )
            )

    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)
    monkeypatch.setattr(
        local.OpenAIEmbeddingProvider,
        "from_environment",
        lambda: FailingProvider(),
    )

    with pytest.raises(local.PreflightError, match="provider check failed"):
        local.run_preflight()

    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_preflight_rejects_role_with_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[7] = [
        ("public", relation, True, relation == "studies")
        for relation in local._REQUIRED_RELATION_COLUMNS
    ]
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="not SELECT-only"):
        local.run_preflight()


@pytest.mark.parametrize("role_index", [0, 1])
def test_preflight_rejects_set_role_or_proxy_identity(
    monkeypatch: pytest.MonkeyPatch,
    role_index: int,
) -> None:
    responses = _passing_database_responses()
    role = list(responses[1])
    role[role_index] = "database_owner"
    responses[1] = tuple(role)
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="session and current roles"):
        local.run_preflight()


def test_preflight_rejects_column_select_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[9] = ("public", "private_table", "private_column")
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="columns outside"):
        local.run_preflight()


def test_preflight_rejects_column_level_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[11] = ("public", "studies", "name")
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="column-level write"):
        local.run_preflight()


def test_preflight_rejects_any_non_system_sequence_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[12] = ("public", "internal_sequence")
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="sequence privileges"):
        local.run_preflight()


def test_preflight_rejects_database_create_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[3] = (True, False, False)
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="creation privileges"):
        local.run_preflight()


def test_preflight_rejects_security_definer_routine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    responses[13] = [("", True, True)]
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="unsafe routine"):
        local.run_preflight()


def test_preflight_rejects_incomplete_role_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _passing_database_responses()
    role = list(responses[1])
    role[10] = []
    responses[1] = tuple(role)
    connection = FakeConnection(responses)
    monkeypatch.setenv("QG_MCP_EXPECTED_DB_ROLE", "querygap_mcp_ro")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(local, "_connect_for_preflight", lambda: connection)

    with pytest.raises(local.PreflightError, match="defaults are incomplete"):
        local.run_preflight()


def test_launcher_runs_local_http_only_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeServer:
        def run(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr(
        local, "load_local_environment", lambda _path=None: Path(".env.mcp.local")
    )
    monkeypatch.setattr(local, "run_preflight", lambda: local.PreflightReport())
    monkeypatch.setattr("querygap_mcp.server.mcp", FakeServer())

    assert local.main(["--port", "8765"]) == 0
    assert calls == [
        (
            (),
            {
                "transport": "streamable-http",
                "host": "127.0.0.1",
                "port": 8765,
                "streamable_http_path": "/mcp",
                "stateless_http": True,
                "json_response": True,
                "max_request_body_size": 65_536,
            },
        )
    ]


def test_launcher_runs_opt_in_live_suite_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path, str]] = []
    monkeypatch.setattr(
        local, "load_local_environment", lambda _path=None: Path(".env.mcp.local")
    )
    monkeypatch.setattr(local, "run_preflight", lambda: local.PreflightReport())
    monkeypatch.setattr(local, "repository_root", lambda: Path("/repo"))

    def fake_run(command: list[str], *, cwd: Path, env: dict[str, str], check: bool):
        assert check is False
        calls.append((command, cwd, env["QG_MCP_LIVE_TESTS"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(local.subprocess, "run", fake_run)

    assert local.main(["--live-tests"]) == 0
    assert calls == [
        ([sys.executable, "-m", "pytest", "-q", "tests/mcp_live"], Path("/repo"), "1")
    ]
