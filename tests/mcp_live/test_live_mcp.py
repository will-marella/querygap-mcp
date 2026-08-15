"""Opt-in MCP smoke tests against a real QueryGaP metadata database.

These tests never discover application configuration or load ``.env`` files.
They are collected but skipped unless the operator explicitly supplies both the
live-test flag and the MCP-specific database URL.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

STUDY_ID = "phs000007.v35.p16"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {
    "resolve_dbgap_study",
    "get_dbgap_study",
    "search_dbgap_catalog",
    "search_ukb_fields",
    "get_ukb_field",
}
EXPECTED_RESOURCES = {
    "querygap://ontology/v0",
    "querygap://retrieval-contract/v0",
}


def _live_enabled() -> bool:
    return os.getenv("QG_MCP_LIVE_TESTS", "").strip() == "1" and bool(
        os.getenv("QG_MCP_DATABASE_URL", "").strip()
    )


def _embeddings_enabled() -> bool:
    enabled = os.getenv("QG_MCP_EMBEDDINGS_ENABLED", "1").strip().lower()
    return enabled not in {"0", "false", "no", "off"} and bool(
        os.getenv("OPENAI_API_KEY", "").strip()
    )


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not _live_enabled(),
        reason=(
            "live MCP smoke requires QG_MCP_LIVE_TESTS=1 and an explicit "
            "QG_MCP_DATABASE_URL"
        ),
    ),
]


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Keep the live smoke sequential on one asyncio event loop."""
    return "asyncio"


@pytest.fixture(scope="module")
async def live_client() -> AsyncIterator[Client]:
    """Launch real Streamable HTTP and connect with the official MCP client."""
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "querygap_mcp",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_loopback(process, port)
        async with Client(
            f"http://127.0.0.1:{port}/mcp",
            raise_exceptions=False,
        ) as client:
            yield client
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_loopback(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("the local MCP smoke server exited before accepting connections")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("the local MCP smoke server did not accept connections within 10 seconds")


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, _safe_result_summary(result)
    assert isinstance(result.structured_content, Mapping)
    return dict(result.structured_content)


def _safe_result_summary(result: Any) -> str:
    """Return only MCP error text; never include configuration or environment."""
    return "\n".join(
        str(content.text)
        for content in result.content
        if hasattr(content, "text")
    )


def _assert_exact_scope(items: Any, *, kind: str) -> None:
    assert isinstance(items, list)
    for item in items:
        assert item["source"] == "dbgap"
        assert item["kind"] == kind
        assert item["study_id"].lower() == STUDY_ID


async def test_live_protocol_and_read_only_retrieval(live_client: Client) -> None:
    """Exercise the bounded public contract with a small, sequential query set."""
    assert live_client.protocol_version is not None
    assert live_client.server_info is not None
    assert live_client.server_info.name == "querygap"

    tools = await live_client.list_tools()
    resources = await live_client.list_resources()
    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    assert {str(resource.uri) for resource in resources.resources} == EXPECTED_RESOURCES
    for tool in tools.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False

    ontology = await live_client.read_resource("querygap://ontology/v0")
    retrieval_contract = await live_client.read_resource(
        "querygap://retrieval-contract/v0"
    )
    assert "participant-level data" in ontology.contents[0].text
    assert STUDY_ID in ontology.contents[0].text
    assert "full versioned study accession" in retrieval_contract.contents[0].text

    resolved = _structured(
        await live_client.call_tool(
            "resolve_dbgap_study",
            {"query": STUDY_ID},
        )
    )
    assert resolved["recommended"]["study_id"].lower() == STUDY_ID
    assert all(
        candidate["study_id"].lower() == STUDY_ID
        for candidate in resolved["candidates"]
    )
    assert resolved["recommended"]["variable_count"] > 0
    assert resolved["recommended"]["dataset_count"] > 0

    resolved_name = _structured(
        await live_client.call_tool(
            "resolve_dbgap_study",
            {"query": "Framingham Heart Study"},
        )
    )
    assert resolved_name["recommended"]["study_id"].lower() == STUDY_ID
    assert resolved_name["resolution"]["authoritative"] is False
    assert len(resolved_name["candidates"]) > 1

    study = _structured(
        await live_client.call_tool("get_dbgap_study", {"study_id": STUDY_ID})
    )
    assert study["study"]["study_id"].lower() == STUDY_ID
    assert study["study"]["canonical_url"].startswith("https://www.ncbi.nlm.nih.gov/")
    assert study["provenance"]["source_system"] == "NCBI dbGaP"

    variables = _structured(
        await live_client.call_tool(
            "search_dbgap_catalog",
            {
                "study_id": STUDY_ID,
                "kind": "variable",
                "query": "blood pressure",
                "method": "keyword",
                "limit": 3,
            },
        )
    )
    assert variables["retrieval"]["method"] == "keyword"
    assert variables["retrieval"]["embedding_provider_used"] is False
    assert variables["items"]
    _assert_exact_scope(variables["items"], kind="variable")

    datasets = _structured(
        await live_client.call_tool(
            "search_dbgap_catalog",
            {
                "study_id": STUDY_ID,
                "kind": "dataset",
                "query": "blood pressure",
                "method": "keyword",
                "limit": 3,
            },
        )
    )
    assert datasets["study_id"].lower() == STUDY_ID
    assert datasets["retrieval"]["method"] == "keyword"
    _assert_exact_scope(datasets["items"], kind="dataset")

    documents = _structured(
        await live_client.call_tool(
            "search_dbgap_catalog",
            {
                "study_id": STUDY_ID,
                "kind": "document",
                "query": "consent",
                "method": "keyword",
                "limit": 3,
            },
        )
    )
    assert documents["study_id"].lower() == STUDY_ID
    assert documents["retrieval"]["method"] == "keyword"
    _assert_exact_scope(documents["items"], kind="document")

    ukb_search = _structured(
        await live_client.call_tool(
            "search_ukb_fields",
            {"query": "21022", "method": "keyword", "limit": 3},
        )
    )
    assert any(item["id"] == 21022 for item in ukb_search["items"])
    assert all(item["source"] == "ukb" for item in ukb_search["items"])

    ukb_field = _structured(
        await live_client.call_tool(
            "get_ukb_field",
            {"field_id": 21022, "include_instance_summaries": False},
        )
    )
    assert ukb_field["field"]["id"] == 21022
    assert ukb_field["field"]["canonical_url"].endswith("?id=21022")
    assert ukb_field["instance_summary"] is None

    unknown = await live_client.call_tool(
        "execute_sql",
        {"query": "LIVE_SMOKE_SENTINEL"},
    )
    invalid_scope = await live_client.call_tool(
        "get_dbgap_study",
        {"study_id": "phs000007"},
    )
    assert unknown.is_error is True
    assert invalid_scope.is_error is True
    failure_text = _safe_result_summary(unknown) + _safe_result_summary(invalid_scope)
    assert "LIVE_SMOKE_SENTINEL" not in failure_text
    assert "postgresql://" not in failure_text.lower()
    assert "invalid_scope" in failure_text
    assert "execute_sql" in json.dumps(unknown.model_dump())


@pytest.mark.skipif(
    not _embeddings_enabled(),
    reason=(
        "hybrid smoke requires embeddings enabled and an explicit OPENAI_API_KEY; "
        "the keyword/database smoke still runs without it"
    ),
)
async def test_live_hybrid_retrieval_when_configured(live_client: Client) -> None:
    hybrid = _structured(
        await live_client.call_tool(
            "search_dbgap_catalog",
            {
                "study_id": STUDY_ID,
                "kind": "variable",
                "query": "blood pressure",
                "method": "hybrid",
                "limit": 3,
            },
        )
    )
    assert hybrid["retrieval"]["method"] == "hybrid"
    assert hybrid["retrieval"]["embedding_provider_used"] is True
    assert hybrid["items"]
    _assert_exact_scope(hybrid["items"], kind="variable")
