-- QueryGaP MCP least-privilege role policy.
--
-- Run in the catalog database as its owner *after* creating querygap_mcp_ro.
-- Create LOGIN and set its generated password through a parameterized admin
-- helper; never interpolate a password into this file or a shell command.
-- Re-running this file is safe: GRANT, REVOKE, and ALTER ROLE are idempotent.

ALTER ROLE querygap_mcp_ro
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 4;

ALTER ROLE querygap_mcp_ro SET default_transaction_read_only = 'on';
ALTER ROLE querygap_mcp_ro SET statement_timeout = '10s';
ALTER ROLE querygap_mcp_ro SET lock_timeout = '1s';
ALTER ROLE querygap_mcp_ro SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE querygap_mcp_ro SET search_path = pg_catalog, public;

-- Limit direct grants. PostgreSQL ACLs are additive: run the companion audit
-- to detect privileges inherited through PUBLIC, ownership, or memberships.
REVOKE ALL PRIVILEGES ON SCHEMA public FROM querygap_mcp_ro;
GRANT USAGE ON SCHEMA public TO querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON SCHEMA aou FROM querygap_mcp_ro;
GRANT USAGE ON SCHEMA aou TO querygap_mcp_ro;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA aou FROM querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA aou FROM querygap_mcp_ro;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA aou FROM querygap_mcp_ro;

-- Table-level revocation does not remove grants made on individual columns.
-- Revoke every direct column ACL before applying the relation allowlist.
DO $policy$
DECLARE
    relation_column record;
BEGIN
    FOR relation_column IN
        SELECT
            namespace.nspname,
            relation.relname,
            attribute.attname
        FROM pg_catalog.pg_class relation
        JOIN pg_catalog.pg_namespace namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_attribute attribute
          ON attribute.attrelid = relation.oid
        WHERE namespace.nspname IN ('public', 'aou')
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM querygap_mcp_ro',
            relation_column.attname,
            relation_column.nspname,
            relation_column.relname
        );
    END LOOP;
END
$policy$;

GRANT SELECT ON TABLE
    public.studies,
    public.datasets,
    public.variables,
    public.variable_reports,
    public.documents,
    public.ukb_search_docs_fields,
    public.ukb_field_page_data,
    public.ukb_field_instance_stats,
    aou.snapshots,
    aou.active_snapshots,
    aou.items,
    aou.identifiers,
    aou.edges,
    aou.links,
    aou.program_measurement_details,
    aou.survey_scales,
    aou.survey_scale_memberships,
    aou.survey_scale_response_weights,
    aou.search_docs,
    aou.search_doc_members,
    aou.aliases,
    aou.embedding_cache
TO querygap_mcp_ro;

-- TEMP is normally inherited from PUBLIC. This removes a direct role grant;
-- the audit reports whether PUBLIC still supplies it. Revoking TEMP from
-- PUBLIC is a database-wide policy decision and is intentionally not done.
DO $policy$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO querygap_mcp_ro',
        current_database()
    );
    EXECUTE format(
        'REVOKE CREATE ON DATABASE %I FROM querygap_mcp_ro',
        current_database()
    );
    EXECUTE format(
        'REVOKE TEMPORARY ON DATABASE %I FROM querygap_mcp_ro',
        current_database()
    );
END
$policy$;
