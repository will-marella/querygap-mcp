from __future__ import annotations

from types import SimpleNamespace

from querygap_mcp.embedding import OpenAIEmbeddingProvider


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.5] * kwargs["dimensions"])]
        )


class FakeBudget:
    def __init__(self) -> None:
        self.acquisitions = 0
        self.availability_checks = 0

    def acquire(self) -> None:
        self.acquisitions += 1

    def check_available(self) -> None:
        self.availability_checks += 1


def test_provider_uses_fixed_dimensions_and_bounded_cache() -> None:
    embeddings = FakeEmbeddings()
    client = SimpleNamespace(embeddings=embeddings)
    provider = OpenAIEmbeddingProvider(
        api_key="test-only",  # pragma: allowlist secret
        model="text-embedding-3-small",
        cache_size=1,
        client=client,
    )

    assert len(provider("  blood   pressure ")) == 1536
    provider("blood pressure")
    provider("kidney function")
    provider("blood pressure")

    assert [call["input"] for call in embeddings.calls] == [
        ["blood pressure"],
        ["kidney function"],
        ["blood pressure"],
    ]
    assert all(call["dimensions"] == 1536 for call in embeddings.calls)
    assert "blood pressure" not in repr(provider._cache)


def test_environment_factory_requires_server_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("QG_MCP_EMBEDDINGS_ENABLED", raising=False)
    assert OpenAIEmbeddingProvider.from_environment() is None

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "0")
    assert OpenAIEmbeddingProvider.from_environment() is None

    monkeypatch.setenv("QG_MCP_EMBEDDINGS_ENABLED", "1")
    assert isinstance(
        OpenAIEmbeddingProvider.from_environment(), OpenAIEmbeddingProvider
    )


def test_startup_probe_checks_provider_without_consuming_budget() -> None:
    embeddings = FakeEmbeddings()
    budget = FakeBudget()
    provider = OpenAIEmbeddingProvider(
        api_key="test-only",  # pragma: allowlist secret
        budget=budget,
        client=SimpleNamespace(embeddings=embeddings),
    )

    vector = provider.startup_probe("fixed startup probe")

    assert len(vector) == 1536
    assert budget.availability_checks == 1
    assert budget.acquisitions == 0
    assert embeddings.calls[0]["input"] == ["fixed startup probe"]
