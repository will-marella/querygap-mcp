# QueryGaP MCP public beta and local development

The QueryGaP MCP server exposes the project's scientific retrieval layer to
ChatGPT, Codex, and other MCP clients. The client performs the reasoning;
QueryGaP returns structured, read-only metadata and source links.

This is deliberately separate from the QueryGaP website. It does not invoke
the existing LangGraph agent, consume QueryGaP credits, read chat history, or
accept a user's model-provider key.

## Public beta

Connect any MCP client that supports remote Streamable HTTP to:

```text
https://mcp.querygap.org/mcp
```

The beta is anonymous: there is no QueryGaP account, credit purchase, bearer
token, or user-supplied API key. The client pays for its own reasoning model;
QueryGaP funds only query embeddings. Keyword retrieval does not contact a
model provider. Semantic and hybrid retrieval send the normalized search text
to OpenAI for embedding, and return explicit retrieval metadata if hybrid
search falls back to keyword.

The public service uses global request, concurrency, and daily embedding caps.
Clients must handle bounded `429`, `503`, and `504` responses. It exposes no
generic SQL, arbitrary URL fetch, ingestion, user, billing, chat, or deployment
tool. A useful first request is: “Resolve the Framingham Heart Study, then find
variables related to systolic blood pressure.”

## Local setup

Use Python 3.10 or newer from the repository root (3.12 and 3.14 are tested):

```bash
python -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt
```

Create a dedicated secret file from the template. It may contain only
`QG_MCP_*` settings and `OPENAI_API_KEY`, must be owned by the current user,
and must have mode `0600` or stricter:

```bash
mkdir -p ~/.config/querygap
cp .env.mcp.local.example ~/.config/querygap/mcp.env
chmod 600 ~/.config/querygap/mcp.env
# Edit ~/.config/querygap/mcp.env with the dedicated role URL and embedding key.
```

`QG_MCP_DATABASE_URL` is deliberately separate and mandatory. The MCP server
never reads the web app's `DATABASE_URL` or repository `.env`; use a dedicated
catalog role even for local testing. A database administrator creates the login
with a generated password, applies `ops/querygap_mcp_ro.sql`, and runs
`ops/querygap_mcp_ro_audit.sql`. Any `FAIL` blocks startup. A local database may
still report inherited `TEMP` or trusted security-invoker routine warnings; the
isolated hosted catalog resolves those findings and starts with zero warnings.

Optional bounded database settings are `QG_MCP_DB_POOL_MIN` (default 1),
`QG_MCP_DB_POOL_MAX` (default 4), `QG_MCP_DB_CONNECT_TIMEOUT_SECONDS` (default
5), `QG_MCP_DB_STATEMENT_TIMEOUT_MS` (default 10000), and
`QG_MCP_DB_LOCK_TIMEOUT_MS` (default 1000).

The hosted design uses QueryGaP's embedding key, never a key supplied by the
person using the MCP client. Query text is sent to OpenAI only for `semantic`
or `hybrid` retrieval; `keyword` retrieval has no model-provider egress. Set
`QG_MCP_EMBEDDINGS_ENABLED=0` as an immediate embedding-spend kill switch.

The recommended internal command runs the security/schema/embedding preflight
before listening and binds only to localhost:

```bash
./scripts/querygap-mcp-local --env-file ~/.config/querygap/mcp.env
```

The local endpoint is `http://127.0.0.1:8000/mcp`. A current Codex CLI can
register it while the server is running:

```bash
codex mcp add querygap --url http://127.0.0.1:8000/mcp
codex mcp get querygap
```

Start a fresh Codex task after registration so it loads the server. Do not
expose this development server publicly without the production controls below.

The repository plugin is configured for the anonymous hosted endpoint and
packages the same research-use instructions with its MCP connection metadata.
It remains source-only until the public repository is published; no marketplace
entry is included.

## Tools

- `resolve_dbgap_study`
- `get_dbgap_study`
- `search_dbgap_catalog`
- `search_ukb_fields`
- `get_ukb_field`

All tools are read-only, idempotent, bounded, and return structured content.
dbGaP catalog searches require a full versioned study accession. UK Biobank is
kept as a separate field-centric source.

Retrieval is entity-specific. Variables and datasets use PostgreSQL full-text
search for `keyword`; UK Biobank adds exact field-ID and stored-alias boosts.
Document retrieval is title-only: keyword search uses a case-insensitive title
substring match, while semantic and hybrid search use title embeddings. QueryGaP
does not index or embed linked document bodies.

Static resources at `querygap://ontology/v0` and
`querygap://retrieval-contract/v0` document identity, relationships,
normalization, retrieval scoring, provenance gaps, and known limitations.

See [mcp-security.md](mcp-security.md) for the threat model, implemented hosted
controls, and remaining gates before broad promotion or marketplace submission.

## Testing

Install the development requirements and run:

```bash
.venv-mcp/bin/pip install -r requirements-mcp-dev.txt
.venv-mcp/bin/python -m pytest -q tests/mcp
```

The unit tests use an in-memory fake backend and never connect to the live
database or OpenAI. The opt-in command below reruns preflight, launches the real
Streamable HTTP server, and exercises keyword and hybrid retrieval through the
official MCP client:

```bash
./scripts/querygap-mcp-local \
  --env-file ~/.config/querygap/mcp.env \
  --live-tests
```

MCP Inspector can be used after the contract tests pass:

```bash
.venv-mcp/bin/mcp dev querygap_mcp/server.py
```

The fixed host-behavior prompts and first internal Codex baseline are in
[`evaluations/mcp`](../evaluations/mcp) and [mcp-evaluation.md](mcp-evaluation.md).
That baseline is a local, manually reviewed contract check on a pre-release
candidate. It is not a hosted interoperability result, retrieval-relevance
benchmark, model-accuracy estimate, or reliability claim.

## Hosted boundary

The beta runs as a separate Railway project, service, volume, database, and
database identity. It does not modify or share credentials with the existing
QueryGaP web deployment. The hosted runtime:

- uses a `NOINHERIT`, connection-limited PostgreSQL login with `SELECT` only on
  the eight required catalog relations;
- enforces read-only transactions, exact relation and role preflight, timeouts,
  bounded inputs/results, four in-flight requests, global request caps, and a
  persistent daily embedding budget;
- uses exact Host and Origin allowlists and a 64 KiB request-body ceiling;
- retains only fixed route/method/status/duration application logs, not query
  text, results, authorization headers, client IPs, or exception details;
- keeps credentials, arbitrary SQL, ingestion, users, billing, chats, and
  deployment operations outside the MCP surface.

Before broad promotion, publish short privacy, support, and terms pages; run
the versioned multi-study evaluation; and decide whether the documented global
beta limits need an edge-level control.

No Railway or QueryGaP website configuration is required for local development.
