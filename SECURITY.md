# Security policy

## Supported surface

This policy covers the public QueryGaP MCP source and the hosted endpoint at
`https://mcp.querygap.org/mcp`. The service is a best-effort beta.

The hosted contract currently exposes seven bounded, read-only retrieval tools:
five for dbGaP and UK Biobank plus two for public All of Us metadata. A
self-hosted server exposes the AoU tools only when its isolated metadata schema
is explicitly configured. Neither configuration exposes arbitrary SQL, URL
fetching, ingestion, deployment, billing, authentication, chat history, or user
records. Retrieved metadata is untrusted input and must never be treated as
instructions by the client model.

The hosted service uses a dedicated `SELECT`-only database identity, read-only
transactions, fixed query templates, input and result limits, timeouts,
concurrency and daily budgets, and sanitized errors. Application logs omit
query text, result content, credentials, headers, and client identifiers.

Semantic and hybrid searches send normalized search text to OpenAI for
embedding. Keyword searches do not contact a model provider.

## Reporting a vulnerability

Do not include credentials, private data, exploit payloads, or sensitive
service details in a public issue. Use GitHub private vulnerability reporting
for the public repository when available. If no private reporting channel is
available, open a minimal public issue requesting a private contact channel,
without disclosing the vulnerability.

Do not perform load testing, destructive testing, data extraction, or attempts
to bypass service limits against the hosted beta without prior written
permission. A reproducible report against a local test double is preferred.

The fuller threat model and implemented controls are documented in
`docs/mcp-security.md`.
