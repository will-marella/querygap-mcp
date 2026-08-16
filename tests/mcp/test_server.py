from __future__ import annotations

import json

import pytest
from mcp import Client

from querygap_mcp.contracts import ServiceError
from querygap_mcp.server import create_server


class FakeService:
    def resolve_dbgap_study(self, query: str, limit: int = 6) -> dict:
        return {"query": query, "limit": limit, "candidates": []}

    def get_dbgap_study(self, study_id: str) -> dict:
        return {"study": {"study_id": study_id}}

    def search_dbgap_catalog(
        self,
        *,
        study_id: str,
        kind: str,
        query: str,
        method: str = "hybrid",
        limit: int = 10,
        include_stats: bool = False,
    ) -> dict:
        return {
            "study_id": study_id,
            "kind": kind,
            "query": query,
            "method": method,
            "limit": limit,
            "include_stats": include_stats,
            "items": [],
        }

    async def search_ukb_fields(
        self, *, query: str, method: str = "hybrid", limit: int = 10
    ) -> dict:
        return {"query": query, "method": method, "limit": limit, "items": []}

    def get_ukb_field(
        self, field_id: int, include_instance_summaries: bool = False
    ) -> dict:
        return {
            "field": {"id": field_id},
            "include_instance_summaries": include_instance_summaries,
        }


class FakeAouService(FakeService):
    def search_aou_catalog(
        self, *, query: str, method: str = "hybrid", limit: int = 10
    ) -> dict:
        return {"query": query, "method": method, "limit": limit, "items": []}

    def get_aou_item(self, result_id: str) -> dict:
        return {"item": {"id": result_id}}


@pytest.mark.anyio
async def test_protocol_lists_exact_tools_resources_and_annotations() -> None:
    async with Client(create_server(FakeService())) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert [tool.name for tool in tools.tools] == [
        "resolve_dbgap_study",
        "get_dbgap_study",
        "search_dbgap_catalog",
        "search_ukb_fields",
        "get_ukb_field",
    ]
    for tool in tools.tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is True
        assert tool.output_schema["type"] == "object"

    catalog = next(tool for tool in tools.tools if tool.name == "search_dbgap_catalog")
    resolver = next(tool for tool in tools.tools if tool.name == "resolve_dbgap_study")
    assert "limit" not in resolver.input_schema["properties"]
    assert catalog.input_schema["properties"]["kind"]["enum"] == [
        "variable",
        "dataset",
        "document",
    ]
    assert catalog.input_schema["properties"]["method"]["default"] == "hybrid"
    assert catalog.input_schema["properties"]["limit"]["maximum"] == 20
    assert {str(resource.uri) for resource in resources.resources} == {
        "querygap://ontology/v0",
        "querygap://retrieval-contract/v0",
    }


@pytest.mark.anyio
async def test_protocol_returns_structured_content_and_static_resources() -> None:
    async with Client(create_server(FakeService())) as client:
        result = await client.call_tool(
            "search_dbgap_catalog",
            {
                "study_id": "phs000007.v35.p16",
                "kind": "variable",
                "query": "blood pressure",
                "include_stats": True,
            },
        )
        ontology = await client.read_resource("querygap://ontology/v0")

    assert result.is_error is False
    assert result.structured_content["include_stats"] is True
    assert "participant-level data" in ontology.contents[0].text


@pytest.mark.anyio
async def test_protocol_adds_aou_tools_only_when_enabled() -> None:
    async with Client(create_server(FakeAouService())) as client:
        tools = await client.list_tools()
        search = await client.call_tool(
            "search_aou_catalog",
            {"query": "diastolic blood pressure", "method": "keyword"},
        )
        detail = await client.call_tool(
            "get_aou_item",
            {"result_id": "aou.doc.0123456789abcdef01234567"},
        )

    assert [tool.name for tool in tools.tools][-2:] == [
        "search_aou_catalog",
        "get_aou_item",
    ]
    aou_search_tool = next(
        tool for tool in tools.tools if tool.name == "search_aou_catalog"
    )
    assert aou_search_tool.input_schema["properties"]["limit"]["maximum"] == 20
    assert search.structured_content["method"] == "keyword"
    assert detail.structured_content["item"]["id"].startswith("aou.doc.")


@pytest.mark.anyio
async def test_service_errors_are_mcp_errors_without_dependency_text() -> None:
    class BrokenService(FakeService):
        def get_dbgap_study(self, study_id: str) -> dict:
            raise ServiceError(
                "database_unavailable", "The retrieval backend is unavailable."
            )

    async with Client(create_server(BrokenService())) as client:
        result = await client.call_tool(
            "get_dbgap_study", {"study_id": "phs000007.v35.p16"}
        )

    body = "\n".join(content.text for content in result.content if hasattr(content, "text"))
    assert result.is_error is True
    assert "database_unavailable" in body
    assert "postgres" not in body.lower()


@pytest.mark.anyio
async def test_oversized_service_results_are_rejected() -> None:
    class OversizedService(FakeService):
        def get_dbgap_study(self, study_id: str) -> dict:
            return {"study_id": study_id, "payload": "x" * (129 * 1024)}

    async with Client(create_server(OversizedService())) as client:
        result = await client.call_tool(
            "get_dbgap_study", {"study_id": "phs000007.v35.p16"}
        )

    body = "\n".join(content.text for content in result.content if hasattr(content, "text"))
    assert result.is_error is True
    assert "result_too_large" in body


@pytest.mark.anyio
async def test_unknown_tool_and_invalid_arguments_are_rejected() -> None:
    async with Client(create_server(FakeService())) as client:
        unknown = await client.call_tool("execute_sql", {"sql": "DROP TABLE studies"})
        invalid = await client.call_tool("resolve_dbgap_study", {})

    assert unknown.is_error is True
    assert invalid.is_error is True
    assert "DROP TABLE" not in json.dumps(unknown.model_dump())
