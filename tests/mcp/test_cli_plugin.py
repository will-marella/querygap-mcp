from __future__ import annotations

import json
from pathlib import Path

from querygap_mcp import __main__ as cli


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def run(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def test_cli_defaults_to_stdio(monkeypatch) -> None:
    fake = FakeServer()
    monkeypatch.setattr(cli, "mcp", fake)

    cli.main([])

    assert fake.calls == [((), {})]


def test_http_cli_is_local_stateless_and_body_bounded(monkeypatch) -> None:
    fake = FakeServer()
    monkeypatch.setattr(cli, "mcp", fake)

    cli.main(["--transport", "streamable-http", "--port", "8765"])

    _, options = fake.calls[0]
    assert options == {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 8765,
        "streamable_http_path": "/mcp",
        "stateless_http": True,
        "json_response": True,
        "max_request_body_size": 65_536,
    }


def test_plugin_manifest_contains_no_secret_values() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "plugins/querygap/.codex-plugin/plugin.json").read_text()
    )
    mcp_config = json.loads((root / "plugins/querygap/.mcp.json").read_text())

    assert manifest["name"] == "querygap"
    assert manifest["mcpServers"] == "./.mcp.json"
    server = mcp_config["mcpServers"]["querygap"]
    assert server == {
        "type": "http",
        "url": "https://mcp.querygap.org/mcp",
    }
    assert not any("sk-" in str(value) for value in server.values())
