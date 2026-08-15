from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from querygap_mcp import hosted
from querygap_mcp.local import PreflightError, PreflightReport
from querygap_mcp.request_limit import RequestLimitExceeded, RequestLimitUnavailable
from querygap_mcp.service import QueryGaPRetrievalService


TOKEN = "test-token-that-is-longer-than-thirty-two-characters"


def _settings(**overrides: object) -> hosted.HostedSettings:
    values: dict[str, object] = {
        "allowed_hosts": ("testserver",),
        "allowed_origins": ("https://allowed.example",),
        "bearer_token": TOKEN,
        "max_in_flight": 4,
        "request_timeout_seconds": 30,
    }
    values.update(overrides)
    return hosted.HostedSettings(**values)  # type: ignore[arg-type]


def _app(
    service: QueryGaPRetrievalService,
    *,
    settings: hosted.HostedSettings | None = None,
    preflight: Callable[[], PreflightReport] | None = None,
    pool_closer: Callable[[], None] | None = None,
):
    return hosted.create_hosted_app(
        settings=settings or _settings(),
        service=service,
        preflight=preflight or (lambda: PreflightReport()),
        pool_closer=pool_closer or (lambda: None),
    )


def _mcp_headers(**overrides: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(overrides)
    return headers


def test_hosted_settings_require_exact_hosts_origins_and_strong_token() -> None:
    environment = {
        "QG_MCP_ALLOWED_HOSTS": "mcp.example.org,railway.example.org",
        "QG_MCP_ALLOWED_ORIGINS": "https://mcp.example.org",
        "QG_MCP_BEARER_TOKEN": TOKEN,
        "QG_MCP_MAX_IN_FLIGHT": "3",
        "QG_MCP_REQUEST_TIMEOUT_SECONDS": "25",
    }

    settings = hosted.HostedSettings.from_environment(environment)

    assert settings.allowed_hosts == ("mcp.example.org", "railway.example.org")
    assert settings.allowed_origins == ("https://mcp.example.org",)
    assert settings.max_in_flight == 3
    assert settings.request_timeout_seconds == 25
    assert TOKEN not in repr(settings)

    with pytest.raises(hosted.HostedConfigurationError, match="without wildcards"):
        hosted.HostedSettings.from_environment(
            {**environment, "QG_MCP_ALLOWED_HOSTS": "*.example.org"}
        )
    with pytest.raises(hosted.HostedConfigurationError, match="without paths"):
        hosted.HostedSettings.from_environment(
            {
                **environment,
                "QG_MCP_ALLOWED_ORIGINS": "https://mcp.example.org/path",
            }
        )
    with pytest.raises(hosted.HostedConfigurationError, match="32 to 512"):
        hosted.HostedSettings.from_environment(
            {**environment, "QG_MCP_BEARER_TOKEN": "short"}
        )
    with pytest.raises(hosted.HostedConfigurationError, match="exactly one"):
        hosted.HostedSettings.from_environment(
            {
                key: value
                for key, value in environment.items()
                if key != "QG_MCP_BEARER_TOKEN"
            }
        )
    with pytest.raises(hosted.HostedConfigurationError, match="exactly one"):
        hosted.HostedSettings.from_environment(
            {**environment, "QG_MCP_ALLOW_ANONYMOUS": "1"}
        )


def test_request_limit_settings_are_required_for_anonymous_and_all_or_none(
    tmp_path,
) -> None:
    base = {
        "QG_MCP_ALLOWED_HOSTS": "mcp.example.org",
        "QG_MCP_ALLOWED_ORIGINS": "https://mcp.example.org",
        "QG_MCP_ALLOW_ANONYMOUS": "1",
    }

    with pytest.raises(hosted.HostedConfigurationError, match="Anonymous mode"):
        hosted.HostedSettings.from_environment(base)
    with pytest.raises(hosted.HostedConfigurationError, match="together"):
        hosted.HostedSettings.from_environment(
            {**base, "QG_MCP_REQUESTS_PER_MINUTE": "30"}
        )

    settings = hosted.HostedSettings.from_environment(
        {
            **base,
            "QG_MCP_REQUESTS_PER_MINUTE": "30",
            "QG_MCP_REQUEST_DAILY_LIMIT": "2000",
            "QG_MCP_REQUEST_BUDGET_PATH": str(tmp_path / "requests.sqlite"),
        }
    )

    assert settings.requests_per_minute == 30
    assert settings.request_daily_limit == 2000
    assert settings.request_budget_path == tmp_path / "requests.sqlite"

    token_settings = hosted.HostedSettings.from_environment(
        {
            "QG_MCP_ALLOWED_HOSTS": "mcp.example.org",
            "QG_MCP_ALLOWED_ORIGINS": "https://mcp.example.org",
            "QG_MCP_BEARER_TOKEN": TOKEN,
        }
    )
    assert token_settings.requests_per_minute is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QG_MCP_REQUESTS_PER_MINUTE", "0"),
        ("QG_MCP_REQUESTS_PER_MINUTE", "10001"),
        ("QG_MCP_REQUEST_DAILY_LIMIT", "0"),
        ("QG_MCP_REQUEST_DAILY_LIMIT", "10000001"),
    ],
)
def test_request_limit_environment_values_are_bounded(
    tmp_path,
    name: str,
    value: str,
) -> None:
    environment = {
        "QG_MCP_ALLOWED_HOSTS": "mcp.example.org",
        "QG_MCP_ALLOWED_ORIGINS": "https://mcp.example.org",
        "QG_MCP_ALLOW_ANONYMOUS": "1",
        "QG_MCP_REQUESTS_PER_MINUTE": "30",
        "QG_MCP_REQUEST_DAILY_LIMIT": "2000",
        "QG_MCP_REQUEST_BUDGET_PATH": str(tmp_path / "requests.sqlite"),
        name: value,
    }

    with pytest.raises(hosted.HostedConfigurationError, match="between"):
        hosted.HostedSettings.from_environment(environment)


def test_request_budget_path_must_be_absolute() -> None:
    with pytest.raises(hosted.HostedConfigurationError, match="absolute"):
        _settings(
            requests_per_minute=30,
            request_daily_limit=2000,
            request_budget_path=Path("requests.sqlite"),
        )


def test_health_is_public_and_readiness_follows_successful_preflight(
    service: QueryGaPRetrievalService,
) -> None:
    events: list[str] = []

    def preflight() -> PreflightReport:
        events.append("preflight")
        return PreflightReport(warnings=("bounded warning",))

    app = _app(
        service,
        preflight=preflight,
        pool_closer=lambda: events.append("closed"),
    )
    assert app.state.ready is False

    with TestClient(app) as client:
        assert events == ["preflight"]
        assert client.get(hosted.HEALTH_LIVE_PATH).json() == {"status": "live"}
        ready = client.get(hosted.HEALTH_READY_PATH)
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

    assert app.state.ready is False
    assert events == ["preflight", "closed"]


def test_failed_preflight_prevents_startup_and_closes_pool(
    service: QueryGaPRetrievalService,
) -> None:
    closed: list[bool] = []

    def fail() -> PreflightReport:
        raise PreflightError("sanitized failure")

    app = _app(service, preflight=fail, pool_closer=lambda: closed.append(True))

    with pytest.raises(PreflightError, match="sanitized failure"):
        with TestClient(app):
            pass

    assert app.state.ready is False
    assert closed == [True]


def test_default_hosted_preflight_does_not_consume_embedding_budget(
    monkeypatch: pytest.MonkeyPatch,
    service: QueryGaPRetrievalService,
) -> None:
    calls: list[tuple[bool, bool]] = []

    def preflight(
        *,
        consume_embedding_budget: bool,
        allow_transient_embedding_failure: bool,
    ) -> PreflightReport:
        calls.append(
            (consume_embedding_budget, allow_transient_embedding_failure)
        )
        return PreflightReport(
            embeddings="daily budget unavailable or exhausted; keyword retrieval only"
        )

    monkeypatch.setattr(hosted, "run_preflight", preflight)
    app = hosted.create_hosted_app(
        settings=_settings(),
        service=service,
        pool_closer=lambda: None,
    )

    with TestClient(app) as client:
        assert client.get(hosted.HEALTH_READY_PATH).status_code == 200

    assert calls == [(False, True)]


def test_mcp_requires_bearer_and_exact_host_and_origin(
    service: QueryGaPRetrievalService,
) -> None:
    app = _app(service)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    with TestClient(app) as client:
        unauthenticated = client.post(hosted.MCP_PATH, json=body)
        wrong_host = client.post(
            hosted.MCP_PATH,
            json=body,
            headers=_mcp_headers(Host="evil.example"),
        )
        wrong_origin = client.post(
            hosted.MCP_PATH,
            json=body,
            headers=_mcp_headers(Origin="https://evil.example"),
        )
        accepted_boundary = client.post(
            hosted.MCP_PATH,
            json=body,
            headers=_mcp_headers(Origin="https://allowed.example"),
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"].startswith("Bearer")
    assert wrong_host.status_code == 421
    assert wrong_origin.status_code == 403
    assert accepted_boundary.status_code not in {401, 403, 421}


def test_authentication_precedes_request_accounting(
    service: QueryGaPRetrievalService,
    tmp_path,
) -> None:
    app = _app(
        service,
        settings=_settings(
            requests_per_minute=1,
            request_daily_limit=10,
            request_budget_path=tmp_path / "requests.sqlite",
        ),
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    with TestClient(app) as client:
        assert client.post(hosted.MCP_PATH, json=body).status_code == 401
        accepted = client.post(hosted.MCP_PATH, json=body, headers=_mcp_headers())
        limited = client.post(hosted.MCP_PATH, json=body, headers=_mcp_headers())

    assert accepted.status_code != 429
    assert limited.status_code == 429
    assert limited.json() == {"error": "rate_limit_exceeded"}
    assert int(limited.headers["retry-after"]) >= 1


def test_unavailable_request_counter_fails_startup_closed(
    service: QueryGaPRetrievalService,
    tmp_path,
) -> None:
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    app = _app(
        service,
        settings=_settings(
            requests_per_minute=10,
            request_daily_limit=100,
            request_budget_path=directory,
        ),
    )

    with pytest.raises(RequestLimitUnavailable):
        with TestClient(app):
            pass


def test_mcp_rejects_oversized_request_before_protocol_handling(
    service: QueryGaPRetrievalService,
) -> None:
    app = _app(service, settings=_settings(max_request_body_size=64))

    with TestClient(app) as client:
        response = client.post(
            hosted.MCP_PATH,
            content=b"x" * 65,
            headers=_mcp_headers(),
        )

    assert response.status_code == 413


def test_explicit_anonymous_mode_omits_bearer_requirement(
    service: QueryGaPRetrievalService,
    tmp_path,
) -> None:
    app = _app(
        service,
        settings=_settings(
            bearer_token=None,
            allow_anonymous=True,
            requests_per_minute=30,
            request_daily_limit=2000,
            request_budget_path=tmp_path / "requests.sqlite",
        ),
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    with TestClient(app) as client:
        response = client.post(
            hosted.MCP_PATH,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code != 401


def test_mcp_slash_variant_does_not_redirect(
    service: QueryGaPRetrievalService,
) -> None:
    app = _app(service)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/mcp/", json={})

    assert response.status_code == 404
    assert "location" not in response.headers


def test_concurrency_precedes_request_accounting(
    service: QueryGaPRetrievalService,
    tmp_path,
) -> None:
    app = _app(
        service,
        settings=_settings(
            requests_per_minute=30,
            request_daily_limit=2000,
            request_budget_path=tmp_path / "requests.sqlite",
        ),
    )

    middleware = [item.cls for item in app.user_middleware]

    assert middleware.index(hosted.ConcurrencyLimitMiddleware) < middleware.index(
        hosted.RequestLimitMiddleware
    )


@pytest.mark.anyio
async def test_request_limit_counts_every_mcp_method_but_excludes_health() -> None:
    calls: list[str] = []

    class Limiter:
        def acquire(self) -> None:
            calls.append("acquire")

    async def ok_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = hosted.RequestLimitMiddleware(ok_app, limiter=Limiter())

    async def invoke(path: str, method: str) -> list[Message]:
        messages: list[Message] = []
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"private-query",
            "root_path": "",
            "headers": [(b"authorization", b"private-token")],
            "server": ("testserver", 443),
            "client": ("192.0.2.4", 1234),
        }

        async def receive() -> Message:
            return {"type": "http.request", "body": b"private-body"}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    for method in ("GET", "POST", "DELETE", "PUT"):
        assert (await invoke(hosted.MCP_PATH, method))[0]["status"] == 204
    assert (await invoke(hosted.HEALTH_LIVE_PATH, "GET"))[0]["status"] == 204
    assert (await invoke(hosted.HEALTH_READY_PATH, "GET"))[0]["status"] == 204

    assert calls == ["acquire"] * 4


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "status", "body", "retry_after"),
    [
        (
            RequestLimitExceeded(17),
            429,
            b'{"error":"rate_limit_exceeded"}',
            b"17",
        ),
        (
            RequestLimitUnavailable("unavailable"),
            503,
            b'{"error":"rate_limit_unavailable"}',
            b"5",
        ),
    ],
)
async def test_request_limit_returns_stable_privacy_safe_errors(
    error: Exception,
    status: int,
    body: bytes,
    retry_after: bytes,
) -> None:
    class Limiter:
        def acquire(self) -> None:
            raise error

    async def unreachable_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise AssertionError("limited request reached the application")

    middleware = hosted.RequestLimitMiddleware(unreachable_app, limiter=Limiter())
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": hosted.MCP_PATH,
        "raw_path": hosted.MCP_PATH.encode(),
        "query_string": b"private-query",
        "root_path": "",
        "headers": [(b"authorization", b"private-token")],
        "server": ("testserver", 443),
        "client": ("192.0.2.4", 1234),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"private-body"}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)

    assert messages[0]["status"] == status
    assert (b"retry-after", retry_after) in messages[0]["headers"]
    assert messages[1]["body"] == body


@pytest.mark.anyio
async def test_concurrency_limit_rejects_instead_of_queueing() -> None:
    entered = anyio.Event()
    release = anyio.Event()

    async def blocking_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    middleware = hosted.ConcurrencyLimitMiddleware(blocking_app, maximum=1)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": hosted.MCP_PATH,
        "raw_path": hosted.MCP_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    first_messages: list[Message] = []
    second_messages: list[Message] = []

    async def send_first(message: Message) -> None:
        first_messages.append(message)

    async def send_second(message: Message) -> None:
        second_messages.append(message)

    async def first() -> None:
        await middleware(scope, receive, send_first)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first)
        await entered.wait()
        await middleware(scope, receive, send_second)
        release.set()

    assert first_messages[0]["status"] == 200
    assert second_messages[0]["status"] == 429


@pytest.mark.anyio
async def test_request_timeout_returns_bounded_error_before_response() -> None:
    async def never_respond(
        _scope: Scope,
        _receive: Receive,
        _send: Send,
    ) -> None:
        await anyio.sleep_forever()

    middleware = hosted.RequestTimeoutMiddleware(never_respond, seconds=0)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": hosted.MCP_PATH,
        "raw_path": hosted.MCP_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)

    assert messages[0]["status"] == 504


@pytest.mark.anyio
async def test_structured_log_does_not_retain_query_headers_or_unknown_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ok_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = hosted.PrivacySafeLoggingMiddleware(ok_app)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "private-method-value",
        "scheme": "https",
        "path": "/private-user-value",
        "raw_path": b"/private-user-value",
        "query_string": b"query=private-query-value",
        "root_path": "",
        "headers": [(b"authorization", b"Bearer private-token-value")],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    with caplog.at_level(logging.INFO, logger="querygap_mcp.hosted"):
        await middleware(scope, receive, lambda _message: _no_op())

    log_text = caplog.text
    assert '"route":"other"' in log_text
    assert '"method":"other"' in log_text
    assert '"status":204' in log_text
    assert "private-method-value" not in log_text
    assert "private-user-value" not in log_text
    assert "private-query-value" not in log_text
    assert "private-token-value" not in log_text


def test_transport_security_rejections_do_not_log_raw_headers(
    service: QueryGaPRetrievalService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(service)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            response = client.post(
                hosted.MCP_PATH,
                json=body,
                headers=_mcp_headers(
                    Host="private-host-value.example",
                    Origin="https://private-origin-value.example",
                ),
            )

    assert response.status_code == 421
    assert "private-host-value" not in caplog.text
    assert "private-origin-value" not in caplog.text


async def _no_op() -> None:
    return None
