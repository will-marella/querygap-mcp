"""Public, JSON-serializable contracts for QueryGaP retrieval.

The contracts deliberately describe the repository's current retrieval model. They
do not imply that QueryGaP stores participant-level data, document bodies, or a
fully normalized cross-source ontology.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


DbgapCatalogKind = Literal["variable", "dataset", "document"]
DbgapSearchMethod = Literal["keyword", "semantic", "hybrid"]
UkbSearchMethod = Literal["keyword", "hybrid"]
AouSearchMethod = Literal["keyword", "semantic", "hybrid"]
AouVariableType = Literal["ehr", "survey", "physical_measurement", "fitbit"]
AouEhrDomain = Literal["Condition", "Drug", "Measurement", "Procedure"]
AouEhrRole = Literal["standard", "source", "classification"]
JsonObject = dict[str, Any]


ONTOLOGY_RESOURCE_URI = "querygap://ontology/v0"
RETRIEVAL_CONTRACT_RESOURCE_URI = "querygap://retrieval-contract/v0"


class Provenance(TypedDict):
    source_system: str
    source_url: str | None
    snapshot_id: str | None
    fetched_at: str | None
    checksum: str | None
    status: str


class ResolutionPolicy(TypedDict):
    status: str
    authoritative: bool
    policy: str


class ServiceError(Exception):
    """A safe, stable error for transport adapters to serialize.

    Dependency exception messages are intentionally never copied into this
    object. ``details`` is reserved for validated request data.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: JsonObject | None = None,
    ) -> None:
        # Include the stable code in the exception text because MCP transports
        # represent raised tool failures as text. Neither dependency exception
        # text nor request data is included here.
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> JsonObject:
        error: JsonObject = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = dict(self.details)
        return {"error": error}
