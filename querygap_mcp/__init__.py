"""Structured, read-only QueryGaP retrieval contracts and service."""

from .contracts import (
    ONTOLOGY_RESOURCE_URI,
    RETRIEVAL_CONTRACT_RESOURCE_URI,
    ServiceError,
)
from .embedding import OpenAIEmbeddingProvider
from .database import DatabaseConfigurationError
from .service import (
    EmbeddingProvider,
    QueryGaPService,
    QueryGaPRetrievalService,
    RetrievalDependencies,
    create_repository_service,
    repository_dependencies,
)

__all__ = [
    "EmbeddingProvider",
    "DatabaseConfigurationError",
    "ONTOLOGY_RESOURCE_URI",
    "OpenAIEmbeddingProvider",
    "QueryGaPService",
    "QueryGaPRetrievalService",
    "RETRIEVAL_CONTRACT_RESOURCE_URI",
    "RetrievalDependencies",
    "ServiceError",
    "create_repository_service",
    "repository_dependencies",
]
