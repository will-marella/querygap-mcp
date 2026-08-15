# QueryGaP MCP

QueryGaP MCP provides read-only retrieval over scientific-documentation
metadata from dbGaP and UK Biobank. It exposes structured tools for resolving
studies, searching variables, datasets, and document metadata, and inspecting
UK Biobank fields. The MCP client supplies the reasoning layer; QueryGaP
supplies bounded retrieval and source links.

The canonical service is:

```text
https://mcp.querygap.org/mcp
```

Connect that URL from any MCP client that supports remote Streamable HTTP. A
useful first request is:

> Resolve the Framingham Heart Study, then find variables related to systolic
> blood pressure.

## What is included

- The standalone, read-only MCP runtime and its five typed tools.
- Exact entity-identity, provenance, normalization, and retrieval contracts.
- Keyword, semantic, and hybrid retrieval implementation.
- Security controls, database-role policy, tests, and evaluation prompts.
- A QueryGaP research skill for MCP-capable clients.

## What is not included

- The hosted QueryGaP catalog or any database snapshot.
- Scraping, ingestion, normalization-job, or catalog-update code.
- Production credentials, deployment state, user data, chats, billing, or
  observability data.

The open-source MCP uses QueryGaP's maintained hosted catalog. Its source is
published so the retrieval contract and implementation can be inspected and
improved.

## Tool contract

- `resolve_dbgap_study`
- `get_dbgap_study`
- `search_dbgap_catalog`
- `search_ukb_fields`
- `get_ukb_field`

All tools are read-only, idempotent, bounded, and return structured data.
dbGaP searches require a full versioned study accession. UK Biobank remains a
separate, field-centric source. QueryGaP indexes documentation metadata, not
participant-level records, and document search currently covers titles rather
than document bodies.

Keyword retrieval does not call a model provider. Semantic and hybrid searches
send normalized search text to OpenAI for embedding using QueryGaP's server
key; users do not provide API keys to QueryGaP. Hybrid results report when they
fall back to keyword retrieval.

See `docs/mcp.md`, `docs/mcp-security.md`, and the MCP resources
`querygap://ontology/v0` and `querygap://retrieval-contract/v0` for the complete
contract and limitations.

## Development

Development uses Python 3.10 or newer. Running the server against a real catalog
also requires a schema-compatible PostgreSQL database accessed through the
dedicated read-only role described in `ops/querygap_mcp_ro.sql`.

```bash
python -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp-dev.txt
.venv-mcp/bin/python -m pytest -q tests/mcp
```

Unit tests use fakes and do not contact the hosted catalog or OpenAI. Live tests
are opt-in and require explicitly supplied MCP-specific credentials.

## Status

The hosted endpoint is a best-effort public beta, not a reliability SLA. Its
global request, concurrency, and embedding budgets may return bounded `429`,
`503`, or `504` responses.

Use GitHub Issues for questions and bug reports. Report vulnerabilities through
the process in `SECURITY.md`.

## License

The focused public MCP release is licensed under Apache License 2.0. See
`LICENSE`.
