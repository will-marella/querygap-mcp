from __future__ import annotations

from pathlib import Path

from pglast import parse_sql


ROOT = Path(__file__).resolve().parents[2]
POLICY = (ROOT / "ops/querygap_mcp_ro.sql").read_text(encoding="utf-8")
AUDIT = (ROOT / "ops/querygap_mcp_ro_audit.sql").read_text(encoding="utf-8")

REQUIRED_RELATIONS = {
    "studies",
    "datasets",
    "variables",
    "variable_reports",
    "documents",
    "ukb_search_docs_fields",
    "ukb_field_page_data",
    "ukb_field_instance_stats",
}


def test_role_policy_and_audit_are_valid_postgres() -> None:
    parse_sql(POLICY)
    parse_sql(AUDIT)


def test_policy_has_exact_catalog_allowlist_and_no_credentials() -> None:
    for relation in REQUIRED_RELATIONS:
        assert f"public.{relation}" in POLICY

    lower = POLICY.lower()
    assert "password '" not in lower
    assert "create role" not in lower
    assert "alter role querygap_mcp_ro login" not in lower
    assert "grant all" not in lower
    assert "alter default privileges" not in lower


def test_policy_sets_defense_in_depth_and_revokes_broad_direct_access() -> None:
    lower = POLICY.lower()
    assert "default_transaction_read_only = 'on'" in lower
    assert "statement_timeout = '10s'" in lower
    assert "lock_timeout = '1s'" in lower
    assert "idle_in_transaction_session_timeout = '15s'" in lower
    assert "search_path = pg_catalog, public" in lower
    assert "connection limit 4" in lower
    assert "revoke all privileges on all tables in schema public" in lower
    assert "revoke all privileges on all sequences in schema public" in lower
    assert "revoke all privileges (%i) on table %i.%i" in lower
    assert "revoke create on database %i from querygap_mcp_ro" in lower
    assert "revoke temporary" in lower


def test_audit_checks_additive_acl_escape_hatches_without_secret_output() -> None:
    lower = AUDIT.lower()
    for phrase in (
        "connected identity is target role",
        "no role memberships",
        "login enabled with bounded connections",
        "required relations are read only",
        "no other non-system relations selectable",
        "no other non-system columns selectable",
        "no effective writes on non-system relations",
        "no effective column writes on non-system relations",
        "no non-system sequence privileges",
        "no unsafe executable routines",
        "public schema usable but not creatable",
        "database connect allowed",
        "database create privilege denied",
        "temporary objects denied",
        "trusted security-invoker routines inherited",
    ):
        assert phrase in lower

    assert "rolpassword" not in lower
    assert "pg_shadow" not in lower
    assert "connection string" not in lower
    assert "session_user::text" in lower
    assert "current_user::text" in lower
    assert lower.count("has_column_privilege(") == 2
    assert lower.count("has_sequence_privilege(") == 1
    assert lower.count("c.relkind in ('r', 'p', 'v', 'm', 'f')") == 4
    assert lower.count("c.relkind = 's'") == 1
