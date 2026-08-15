# QueryGaP MCP

QueryGaP gives MCP-capable assistants structured access to dbGaP and UK Biobank
documentation metadata. Use it to resolve dbGaP studies, find variables,
datasets, and document metadata within an exact study accession, and search or
inspect UK Biobank fields—with source identifiers and canonical links in the
results.

The MCP client supplies the reasoning and decides which tools to call. QueryGaP
handles study resolution, scoped retrieval, ranking, and provenance.

## Connect

The hosted MCP endpoint is:

```text
https://mcp.querygap.org/mcp
```

For Codex:

```bash
codex mcp add querygap --url https://mcp.querygap.org/mcp
```

For another MCP client, add the endpoint as a remote Streamable HTTP server.
The hosted public beta requires no QueryGaP account, access token, or
user-supplied OpenAI key. Then try:

> Resolve the Framingham Heart Study, then find variables related to systolic
> blood pressure. Return their exact dbGaP IDs, descriptions, parent datasets,
> and source links.

## Tools

| Tool | Purpose |
| --- | --- |
| `resolve_dbgap_study` | Resolve a study name, acronym, accession, or dbGaP URL to ranked candidates. |
| `get_dbgap_study` | Retrieve metadata for an exact versioned dbGaP accession. |
| `search_dbgap_catalog` | Search a resolved study's variables, datasets, or document-title metadata using keyword, semantic, or hybrid retrieval. |
| `search_ukb_fields` | Search the UK Biobank field dictionary by concept, field ID, or stored aliases. |
| `get_ukb_field` | Retrieve an exact UK Biobank field and optional instance summaries. |

For dbGaP, QueryGaP resolves the study first and preserves the full versioned
accession throughout retrieval. UK Biobank remains a separate, field-centric
source.

## Scope

QueryGaP searches public documentation metadata, not participant-level records.
The hosted MCP is read-only and uses a catalog-only database separate from
QueryGaP accounts, chats, credits, and billing data. Keyword search does not
call a model provider; semantic and hybrid search use QueryGaP-funded query
embeddings.

This repository contains the MCP runtime, retrieval implementation, ontology,
tests, evaluation material, and security controls. It connects to the hosted
QueryGaP catalog; the catalog snapshot and ingestion pipeline are separate.

## Development

QueryGaP MCP supports Python 3.10 and newer. Unit tests use fakes, so they
require neither a catalog database nor OpenAI access.

```bash
python -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp-dev.txt
.venv-mcp/bin/python -m pytest -q tests/mcp
```

Running a server against a real catalog requires a schema-compatible PostgreSQL
database and the dedicated read-only role described in
[`ops/querygap_mcp_ro.sql`](ops/querygap_mcp_ro.sql).

## Documentation

- [`docs/mcp.md`](docs/mcp.md): local setup and complete tool contract
- `querygap://ontology/v0`: entities, identity, relationships, and provenance
- `querygap://retrieval-contract/v0`: study scoping and retrieval rules
- [`docs/mcp-security.md`](docs/mcp-security.md): hosted security boundary
- [`docs/mcp-evaluation.md`](docs/mcp-evaluation.md): evaluation protocol

## Status and license

The hosted endpoint is live as a best-effort public beta with deliberate usage
limits. QueryGaP MCP is licensed under the Apache License 2.0; see
[`LICENSE`](LICENSE). Use GitHub Issues for questions and follow
[`SECURITY.md`](SECURITY.md) for vulnerability reports.
