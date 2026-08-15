# Contributing

Contributions should preserve QueryGaP's narrow public contract: bounded,
read-only scientific-metadata retrieval with exact identifiers, explicit
provenance limitations, and no participant-level data.

Before proposing a change:

1. Keep ingestion, production data, credentials, deployment state, user data,
   billing, chats, and private traces outside the contribution.
2. Preserve full versioned dbGaP accessions and the separate UK Biobank entity
   model.
3. Add or update unit tests for contract, query, security, and failure-mode
   changes.
4. Add a new evaluation version instead of silently changing a completed
   baseline.
5. Run:

   ```bash
   python -m pytest -q tests/mcp
   python -m compileall -q querygap_mcp tests/mcp
   git diff --check
   ```

Unit tests must not contact the hosted service, a production database, or
OpenAI. Do not submit captured metadata, prompts, credentials, or live service
logs as fixtures.

By submitting a contribution, you agree that it is licensed under Apache
License 2.0 as described in `LICENSE`, and that you have the right to submit it.
Security reports should follow `SECURITY.md`, not a public pull request or
detailed public issue.
