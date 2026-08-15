"""Hardened Streamable HTTP runtime for a hosted QueryGaP MCP beta.

This module is deliberately separate from the local launcher. It exposes only
the MCP endpoint and non-diagnostic health endpoints, requires the database
preflight to succeed, and permits transient embedding failures to start in
keyword-only mode. It requires either a temporary server-issued bearer token
or an explicit anonymous-mode opt in.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from querygap_mcp.database import close_pool
from querygap_mcp.local import PreflightReport, run_preflight
from querygap_mcp.request_limit import (
    RequestLimiter,
    RequestLimitExceeded,
    RequestLimitUnavailable,
    SQLiteRequestLimiter,
)
from querygap_mcp.server import QueryGaPService, create_server


MCP_PATH = "/mcp"
HEALTH_LIVE_PATH = "/health/live"
HEALTH_READY_PATH = "/health/ready"
DEFAULT_MAX_REQUEST_BODY_SIZE = 65_536

_LOGGER = logging.getLogger("querygap_mcp.hosted")
_TRANSPORT_SECURITY_LOGGER = logging.getLogger("mcp.server.transport_security")
_SAFE_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "POST"})


class HostedConfigurationError(RuntimeError):
    """Raised when the public HTTP boundary is not configured safely."""


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HostedConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}."
        ) from None
    if not minimum <= value <= maximum:
        raise HostedConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}."
        )
    return value


def _comma_separated(
    environment: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    raw = environment.get(name, "")
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if required and not values:
        raise HostedConfigurationError(f"{name} is required.")
    if len(set(values)) != len(values):
        raise HostedConfigurationError(f"{name} must not contain duplicate values.")
    return values


def _validate_host(host: str) -> None:
    if (
        not isinstance(host, str)
        or not host
        or len(host) > 255
        or "*" in host
        or "://" in host
        or "/" in host
        or "@" in host
        or any(character.isspace() for character in host)
    ):
        raise HostedConfigurationError(
            "QG_MCP_ALLOWED_HOSTS must contain exact host values without wildcards."
        )


def _validate_origin(origin: str) -> None:
    if not isinstance(origin, str):
        raise HostedConfigurationError(
            "QG_MCP_ALLOWED_ORIGINS must contain exact HTTP(S) origins without paths or wildcards."
        )
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise HostedConfigurationError(
            "QG_MCP_ALLOWED_ORIGINS must contain exact HTTP(S) origins without paths or wildcards."
        ) from None
    canonical = f"{parsed.scheme}://{parsed.netloc}"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != canonical
        or "*" in origin
    ):
        raise HostedConfigurationError(
            "QG_MCP_ALLOWED_ORIGINS must contain exact HTTP(S) origins without paths or wildcards."
        )


@dataclass(frozen=True)
class HostedSettings:
    """Validated public HTTP settings, intentionally excluding database secrets."""

    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer_token: str | None = field(repr=False)
    allow_anonymous: bool = False
    max_in_flight: int = 4
    request_timeout_seconds: int = 30
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE
    requests_per_minute: int | None = None
    request_daily_limit: int | None = None
    request_budget_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allow_anonymous, bool):
            raise HostedConfigurationError(
                "QG_MCP_ALLOW_ANONYMOUS must be exactly 1 when enabled."
            )
        if not self.allowed_hosts or len(set(self.allowed_hosts)) != len(
            self.allowed_hosts
        ):
            raise HostedConfigurationError(
                "QG_MCP_ALLOWED_HOSTS must contain unique exact host values."
            )
        if not self.allowed_origins or len(set(self.allowed_origins)) != len(
            self.allowed_origins
        ):
            raise HostedConfigurationError(
                "QG_MCP_ALLOWED_ORIGINS must contain unique exact origins."
            )
        for host in self.allowed_hosts:
            _validate_host(host)
        for origin in self.allowed_origins:
            _validate_origin(origin)
        if isinstance(self.max_in_flight, bool) or not 1 <= self.max_in_flight <= 16:
            raise HostedConfigurationError(
                "QG_MCP_MAX_IN_FLIGHT must be an integer between 1 and 16."
            )
        if (
            isinstance(self.request_timeout_seconds, bool)
            or not 5 <= self.request_timeout_seconds <= 60
        ):
            raise HostedConfigurationError(
                "QG_MCP_REQUEST_TIMEOUT_SECONDS must be an integer between 5 and 60."
            )
        if (
            isinstance(self.max_request_body_size, bool)
            or not 1
            <= self.max_request_body_size
            <= DEFAULT_MAX_REQUEST_BODY_SIZE
        ):
            raise HostedConfigurationError(
                "The hosted MCP request body limit must be between 1 and 65536 bytes."
            )
        has_token = self.bearer_token is not None
        if has_token == self.allow_anonymous:
            raise HostedConfigurationError(
                "Configure exactly one of QG_MCP_BEARER_TOKEN or "
                "QG_MCP_ALLOW_ANONYMOUS=1."
            )
        if self.bearer_token is not None:
            if not isinstance(self.bearer_token, str) or (
                not 32 <= len(self.bearer_token) <= 512
                or any(
                    character.isspace() or ord(character) < 0x20
                    for character in self.bearer_token
                )
            ):
                raise HostedConfigurationError(
                    "QG_MCP_BEARER_TOKEN must be a non-whitespace secret "
                    "of 32 to 512 characters."
                )
        request_limit_values = (
            self.requests_per_minute,
            self.request_daily_limit,
            self.request_budget_path,
        )
        has_request_limit = all(value is not None for value in request_limit_values)
        if any(value is not None for value in request_limit_values) and not (
            has_request_limit
        ):
            raise HostedConfigurationError(
                "Configure QG_MCP_REQUESTS_PER_MINUTE, "
                "QG_MCP_REQUEST_DAILY_LIMIT, and QG_MCP_REQUEST_BUDGET_PATH "
                "together."
            )
        if self.allow_anonymous and not has_request_limit:
            raise HostedConfigurationError(
                "Anonymous mode requires QG_MCP_REQUESTS_PER_MINUTE, "
                "QG_MCP_REQUEST_DAILY_LIMIT, and QG_MCP_REQUEST_BUDGET_PATH."
            )
        if has_request_limit:
            if (
                isinstance(self.requests_per_minute, bool)
                or not isinstance(self.requests_per_minute, int)
                or not 1 <= self.requests_per_minute <= 10_000
            ):
                raise HostedConfigurationError(
                    "QG_MCP_REQUESTS_PER_MINUTE must be an integer between "
                    "1 and 10000."
                )
            if (
                isinstance(self.request_daily_limit, bool)
                or not isinstance(self.request_daily_limit, int)
                or not 1 <= self.request_daily_limit <= 10_000_000
            ):
                raise HostedConfigurationError(
                    "QG_MCP_REQUEST_DAILY_LIMIT must be an integer between "
                    "1 and 10000000."
                )
            if not isinstance(self.request_budget_path, Path) or not (
                self.request_budget_path.is_absolute()
            ):
                raise HostedConfigurationError(
                    "QG_MCP_REQUEST_BUDGET_PATH must be an absolute path."
                )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> HostedSettings:
        values = os.environ if environment is None else environment
        allowed_hosts = _comma_separated(
            values, "QG_MCP_ALLOWED_HOSTS", required=True
        )
        allowed_origins = _comma_separated(
            values, "QG_MCP_ALLOWED_ORIGINS", required=True
        )
        for host in allowed_hosts:
            _validate_host(host)
        for origin in allowed_origins:
            _validate_origin(origin)

        raw_anonymous = values.get("QG_MCP_ALLOW_ANONYMOUS", "")
        if raw_anonymous not in {"", "0", "1"}:
            raise HostedConfigurationError(
                "QG_MCP_ALLOW_ANONYMOUS must be exactly 1 when enabled."
            )
        token = values.get("QG_MCP_BEARER_TOKEN") or None

        request_limit_names = (
            "QG_MCP_REQUESTS_PER_MINUTE",
            "QG_MCP_REQUEST_DAILY_LIMIT",
            "QG_MCP_REQUEST_BUDGET_PATH",
        )
        configured_request_limits = tuple(
            bool(values.get(name, "").strip()) for name in request_limit_names
        )
        if any(configured_request_limits) and not all(configured_request_limits):
            raise HostedConfigurationError(
                "Configure QG_MCP_REQUESTS_PER_MINUTE, "
                "QG_MCP_REQUEST_DAILY_LIMIT, and QG_MCP_REQUEST_BUDGET_PATH "
                "together."
            )
        requests_per_minute = None
        request_daily_limit = None
        request_budget_path = None
        if all(configured_request_limits):
            requests_per_minute = _bounded_integer(
                values,
                "QG_MCP_REQUESTS_PER_MINUTE",
                default=0,
                minimum=1,
                maximum=10_000,
            )
            request_daily_limit = _bounded_integer(
                values,
                "QG_MCP_REQUEST_DAILY_LIMIT",
                default=0,
                minimum=1,
                maximum=10_000_000,
            )
            request_budget_path = Path(values["QG_MCP_REQUEST_BUDGET_PATH"].strip())

        return cls(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            bearer_token=token,
            allow_anonymous=raw_anonymous == "1",
            max_in_flight=_bounded_integer(
                values,
                "QG_MCP_MAX_IN_FLIGHT",
                default=4,
                minimum=1,
                maximum=16,
            ),
            request_timeout_seconds=_bounded_integer(
                values,
                "QG_MCP_REQUEST_TIMEOUT_SECONDS",
                default=30,
                minimum=5,
                maximum=60,
            ),
            requests_per_minute=requests_per_minute,
            request_daily_limit=request_daily_limit,
            request_budget_path=request_budget_path,
        )


def _route_label(path: str) -> str:
    """Map arbitrary paths to a fixed label so logs cannot retain user input."""
    return {
        MCP_PATH: "mcp",
        HEALTH_LIVE_PATH: "health_live",
        HEALTH_READY_PATH: "health_ready",
    }.get(path, "other")


def _method_label(method: object) -> str:
    """Map an untrusted HTTP method token to a fixed log label."""
    normalized = method.upper() if isinstance(method, str) else ""
    return normalized if normalized in _SAFE_METHODS else "other"


class PrivacySafeLoggingMiddleware:
    """Emit bounded structured request metadata without headers, bodies, or queries."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status_code = 500

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            _LOGGER.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "method": _method_label(scope.get("method")),
                        "route": _route_label(str(scope.get("path", ""))),
                        "status": status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    },
                    separators=(",", ":"),
                )
            )


class BearerTokenMiddleware:
    """Require one constant-time-compared bearer token on the MCP endpoint."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return

        authorization_values = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == b"authorization"
        ]
        valid = False
        if len(authorization_values) == 1:
            scheme, separator, supplied = authorization_values[0].partition(" ")
            valid = (
                separator == " "
                and scheme.lower() == "bearer"
                and bool(supplied)
                and hmac.compare_digest(supplied, self._token)
            )
        if not valid:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="querygap-mcp"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ConcurrencyLimitMiddleware:
    """Reject excess MCP work immediately instead of creating an unbounded queue."""

    def __init__(self, app: ASGIApp, *, maximum: int) -> None:
        self.app = app
        self._capacity = anyio.Semaphore(maximum)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        try:
            self._capacity.acquire_nowait()
        except anyio.WouldBlock:
            response = JSONResponse(
                {"error": "server_busy"},
                status_code=429,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._capacity.release()


class RequestLimitMiddleware:
    """Apply one global privacy-free budget to every MCP HTTP method."""

    def __init__(self, app: ASGIApp, *, limiter: RequestLimiter) -> None:
        self.app = app
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        try:
            await anyio.to_thread.run_sync(self._limiter.acquire)
        except RequestLimitExceeded as error:
            response = JSONResponse(
                {"error": "rate_limit_exceeded"},
                status_code=429,
                headers={"Retry-After": str(error.retry_after)},
            )
            await response(scope, receive, send)
            return
        except RequestLimitUnavailable:
            response = JSONResponse(
                {"error": "rate_limit_unavailable"},
                status_code=503,
                headers={"Retry-After": "5"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequestTimeoutMiddleware:
    """Cancel MCP requests that exceed the bounded public request window."""

    def __init__(self, app: ASGIApp, *, seconds: int) -> None:
        self.app = app
        self._seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return

        response_started = False

        async def track_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        with anyio.move_on_after(self._seconds) as cancel_scope:
            await self.app(scope, receive, track_start)
        if cancel_scope.cancel_called and not response_started:
            response = JSONResponse({"error": "request_timeout"}, status_code=504)
            await response(scope, receive, send)


async def _health_live(_request: Request) -> Response:
    return JSONResponse({"status": "live"})


async def _health_ready(request: Request) -> Response:
    ready = bool(getattr(request.app.state, "ready", False))
    return JSONResponse(
        {"status": "ready" if ready else "unready"},
        status_code=200 if ready else 503,
    )


def create_hosted_app(
    *,
    settings: HostedSettings | None = None,
    service: QueryGaPService | None = None,
    preflight: Callable[[], PreflightReport] | None = None,
    pool_closer: Callable[[], None] | None = None,
) -> Starlette:
    """Create the production ASGI app without reading application ``.env`` files."""
    # MCP 2.0 logs rejected Host and Origin values verbatim. Our own bounded
    # status log is sufficient, so suppress that dependency logger entirely.
    _TRANSPORT_SECURITY_LOGGER.disabled = True
    hosted = settings or HostedSettings.from_environment()
    startup_preflight = preflight or (
        lambda: run_preflight(
            consume_embedding_budget=False,
            allow_transient_embedding_failure=True,
        )
    )
    close_database_pool = pool_closer or close_pool
    request_limiter: SQLiteRequestLimiter | None = None
    if hosted.requests_per_minute is not None:
        assert hosted.request_daily_limit is not None
        assert hosted.request_budget_path is not None
        request_limiter = SQLiteRequestLimiter(
            path=hosted.request_budget_path,
            requests_per_minute=hosted.requests_per_minute,
            daily_limit=hosted.request_daily_limit,
        )
    server = create_server(service)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(hosted.allowed_hosts),
        allowed_origins=list(hosted.allowed_origins),
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=hosted.max_request_body_size,
        transport_security=transport_security,
        host="0.0.0.0",
    )
    app.state.ready = False
    # Do not generate an absolute redirect for near-miss paths such as /mcp/.
    # The public protocol endpoint is exactly /mcp.
    app.router.redirect_slashes = False
    app.router.routes.extend(
        [
            Route(HEALTH_LIVE_PATH, _health_live, methods=["GET"]),
            Route(HEALTH_READY_PATH, _health_ready, methods=["GET"]),
        ]
    )

    transport_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def hosted_lifespan(application: Starlette):
        application.state.ready = False
        try:
            if request_limiter is not None:
                await anyio.to_thread.run_sync(request_limiter.check_available)
            report = await anyio.to_thread.run_sync(startup_preflight)
            async with transport_lifespan(application):
                application.state.ready = True
                _LOGGER.info(
                    json.dumps(
                        {
                            "event": "startup_ready",
                            "preflight_warning_count": len(report.warnings),
                            "embedding_degraded": report.embeddings
                            != "enabled and reachable",
                        },
                        separators=(",", ":"),
                    )
                )
                try:
                    yield
                finally:
                    application.state.ready = False
        except BaseException as error:
            _LOGGER.error(
                json.dumps(
                    {
                        "event": "startup_failed",
                        "error_type": type(error).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            raise
        finally:
            application.state.ready = False
            await anyio.to_thread.run_sync(close_database_pool)

    app.router.lifespan_context = hosted_lifespan

    # add_middleware inserts at the front. This order produces:
    # logging -> authentication -> concurrency -> request limit -> timeout ->
    # MCP transport.
    app.add_middleware(
        RequestTimeoutMiddleware,
        seconds=hosted.request_timeout_seconds,
    )
    if request_limiter is not None:
        app.add_middleware(RequestLimitMiddleware, limiter=request_limiter)
    app.add_middleware(
        ConcurrencyLimitMiddleware,
        maximum=hosted.max_in_flight,
    )
    if hosted.bearer_token is not None:
        app.add_middleware(BearerTokenMiddleware, token=hosted.bearer_token)
    app.add_middleware(PrivacySafeLoggingMiddleware)
    return app


def main() -> None:
    """Run the hosted service with access logs disabled to avoid query retention."""
    environment = os.environ
    port = _bounded_integer(
        environment,
        "PORT",
        default=8000,
        minimum=1,
        maximum=65_535,
    )
    import uvicorn

    uvicorn.run(
        create_hosted_app(),
        host="0.0.0.0",
        port=port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "HostedConfigurationError",
    "HostedSettings",
    "create_hosted_app",
    "main",
]
