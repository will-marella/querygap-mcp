"""Secure one-command launcher and startup preflight for local MCP testing.

The repository-root ``.env.mcp.local`` is the default, or callers may provide
one explicit path.  The application ``.env`` is never read, and values from
the dedicated file are never printed.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
from openai import APIConnectionError, APIStatusError, RateLimitError

from querygap_mcp.database import DatabaseConfigurationError, settings_from_environment
from querygap_mcp.embedding import EMBEDDING_DIMENSIONS, OpenAIEmbeddingProvider
from querygap_mcp.quota import EmbeddingBudgetExceeded


MCP_ENV_FILENAME = ".env.mcp.local"
LOCAL_HOST = "127.0.0.1"
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_EXACT_KEYS = {"OPENAI_API_KEY"}
_REQUIRED_RELATION_COLUMNS: Mapping[str, frozenset[str]] = {
    "studies": frozenset({"study_id", "name", "metadata"}),
    "datasets": frozenset(
        {"study_id", "dataset_id", "name", "description", "search_tsv", "embedding"}
    ),
    "variables": frozenset(
        {
            "study_id",
            "dataset_id",
            "variable_id",
            "name",
            "description",
            "search_tsv",
            "embedding",
        }
    ),
    "documents": frozenset(
        {"study_id", "document_id", "name", "url", "document_type", "embedding"}
    ),
    "variable_reports": frozenset(
        {
            "study_id",
            "variable_id",
            "stat_n",
            "stat_mean",
            "stat_median",
            "stat_sd",
            "stat_min",
            "stat_max",
        }
    ),
    "ukb_search_docs_fields": frozenset(
        {
            "field_id",
            "title",
            "notes",
            "units",
            "value_type",
            "item_type",
            "category_path",
            "encoding_id",
            "encoding_title",
            "source_url",
            "category_url",
            "encoding_url",
            "aliases",
            "search_tsv",
            "embedding",
            "instance_id",
            "instance_min",
            "instance_max",
            "instance_descriptions",
        }
    ),
    "ukb_field_page_data": frozenset(
        {
            "field_id",
            "participants",
            "item_count",
            "defined_instance_min",
            "defined_instance_max",
            "not_summarised_reason",
        }
    ),
    "ukb_field_instance_stats": frozenset(
        {
            "field_id",
            "instance",
            "instance_description",
            "participants",
            "item_count",
            "min",
            "median",
            "mean",
            "stddev",
            "max",
        }
    ),
}
_REQUIRED_INDEXED_COLUMNS: Mapping[str, frozenset[str]] = {
    "studies": frozenset({"study_id"}),
    "datasets": frozenset({"study_id", "search_tsv"}),
    "variables": frozenset({"study_id", "dataset_id", "search_tsv"}),
    "documents": frozenset({"study_id"}),
    "variable_reports": frozenset({"study_id", "variable_id"}),
    "ukb_search_docs_fields": frozenset({"field_id", "search_tsv"}),
    "ukb_field_page_data": frozenset({"field_id"}),
    "ukb_field_instance_stats": frozenset({"field_id"}),
}
_AOU_REQUIRED_RELATION_COLUMNS: Mapping[str, frozenset[str]] = {
    "snapshots": frozenset(
        {
            "snapshot_key",
            "release_id",
            "release_label",
            "extracted_at_utc",
            "source_commit_sha",
            "coverage_status",
            "selection_method",
        }
    ),
    "active_snapshots": frozenset(
        {"source_namespace", "snapshot_key", "activated_at_utc"}
    ),
    "items": frozenset(
        {
            "item_key",
            "canonical_key",
            "item_kind",
            "name",
            "description",
            "domain_id",
            "vocabulary_id",
            "concept_code",
            "standard_concept",
            "survey_concept_id",
            "survey_path",
            "help_text",
            "attributes",
            "source_locator",
            "source_record_sha256",
        }
    ),
    "identifiers": frozenset(
        {
            "snapshot_key",
            "item_key",
            "identifier_system",
            "identifier_value",
            "identifier_role",
            "is_primary",
        }
    ),
    "edges": frozenset(
        {
            "edge_key",
            "from_item_key",
            "predicate",
            "to_item_key",
            "qualifier_item_key",
            "ordinal",
            "assertion_status",
            "match_method",
            "source_locator",
            "evidence_text",
        }
    ),
    "links": frozenset(
        {
            "link_key",
            "item_key",
            "link_type",
            "target_specificity",
            "url",
            "label",
            "is_preferred",
        }
    ),
    "program_measurement_details": frozenset({"item_key"}),
    "survey_scales": frozenset(
        {"scale_key", "snapshot_key", "sheet_name", "scale_name"}
    ),
    "survey_scale_memberships": frozenset(
        {
            "membership_key",
            "snapshot_key",
            "scale_key",
            "variable_item_key",
            "question_code",
            "question_text",
            "ordinal",
            "duplicate_ordinal",
            "reverse_scored",
            "variable_match_method",
            "qa_flags",
        }
    ),
    "survey_scale_response_weights": frozenset(
        {
            "response_weight_key",
            "snapshot_key",
            "membership_key",
            "answer_item_key",
            "response_code",
            "response_label",
            "weight_text",
            "weight_value",
            "derived_weight_value",
            "derived_weight_method",
            "ordinal",
            "answer_match_method",
        }
    ),
    "search_docs": frozenset(
        {
            "doc_key",
            "item_key",
            "snapshot_key",
            "search_group_key",
            "search_role",
            "item_kind",
            "is_lexically_searchable",
            "is_embeddable",
            "title",
            "subtitle",
            "native_id",
            "concept_code",
            "domain_id",
            "vocabulary_id",
            "preferred_link_key",
            "content_sha256",
            "search_tsv",
        }
    ),
    "search_doc_members": frozenset({"doc_key", "item_key"}),
    "aliases": frozenset(
        {"snapshot_key", "item_key", "normalized_alias", "alias_type"}
    ),
    "embedding_cache": frozenset(
        {"content_sha256", "embedding_model", "dimensions", "embedding"}
    ),
}
_AOU_REQUIRED_INDEXED_COLUMNS: Mapping[str, frozenset[str]] = {
    "snapshots": frozenset({"snapshot_key"}),
    "active_snapshots": frozenset({"source_namespace"}),
    "items": frozenset({"item_key", "snapshot_key", "item_kind"}),
    "identifiers": frozenset(
        {"snapshot_key", "item_key", "identifier_system", "identifier_value"}
    ),
    "edges": frozenset({"from_item_key", "to_item_key", "predicate"}),
    "links": frozenset({"item_key"}),
    "program_measurement_details": frozenset({"item_key"}),
    "survey_scales": frozenset({"scale_key", "snapshot_key"}),
    "survey_scale_memberships": frozenset(
        {"scale_key", "variable_item_key"}
    ),
    "survey_scale_response_weights": frozenset({"membership_key"}),
    "search_docs": frozenset(
        {"doc_key", "item_key", "snapshot_key", "search_tsv"}
    ),
    "search_doc_members": frozenset({"doc_key", "item_key"}),
    "aliases": frozenset({"snapshot_key", "normalized_alias", "item_key"}),
    "embedding_cache": frozenset(
        {"content_sha256", "embedding_model", "embedding"}
    ),
}
_AOU_REQUIRED_INDEX_NAMES = frozenset(
    {
        "idx_aou_embedding_cache_ivfflat",
        "idx_aou_search_docs_title_trgm",
    }
)


class LocalConfigurationError(RuntimeError):
    """Raised when the local secret file or launcher options are unsafe."""


class PreflightError(RuntimeError):
    """Raised with a sanitized startup-check failure."""


@dataclass(frozen=True)
class PreflightReport:
    database: str = "connected, schema compatible, exact catalog allowlist"
    transactions: str = "read-only"
    embeddings: str = "enabled and reachable"
    warnings: tuple[str, ...] = ()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _allowed_environment_key(key: str) -> bool:
    return key.startswith("QG_MCP_") or key in _ALLOWED_EXACT_KEYS


def _parse_environment_file(contents: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line_number, original in enumerate(contents.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} line {line_number} must use KEY=VALUE syntax."
            )
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not _KEY_RE.fullmatch(key):
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} line {line_number} has an invalid key."
            )
        if not _allowed_environment_key(key):
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} may contain only QG_MCP_* settings and OPENAI_API_KEY."
            )
        if key in parsed:
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} defines {key} more than once."
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\x00" in value:
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} line {line_number} contains an invalid value."
            )
        parsed[key] = value
    return parsed


def _validate_secret_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise LocalConfigurationError(
            f"Missing {MCP_ENV_FILENAME}; copy .env.mcp.local.example and set mode 600."
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LocalConfigurationError(f"{MCP_ENV_FILENAME} must be a regular file, not a symlink.")
    if os.name == "posix":
        if metadata.st_uid != os.getuid():
            raise LocalConfigurationError(f"{MCP_ENV_FILENAME} must be owned by the current user.")
        # Owner read/write is permitted; executable, group, and other bits are not.
        if stat.S_IMODE(metadata.st_mode) & 0o177:
            raise LocalConfigurationError(
                f"{MCP_ENV_FILENAME} permissions are too broad; run chmod 600 {MCP_ENV_FILENAME}."
            )


def load_local_environment(path: Path | None = None) -> Path:
    """Load one explicit MCP file without overriding shell settings."""
    candidate = (path or (repository_root() / MCP_ENV_FILENAME)).expanduser()
    path = Path(os.path.abspath(candidate))
    if path == repository_root() / ".env":
        raise LocalConfigurationError("The application .env cannot be used by the MCP launcher.")
    _validate_secret_file(path)
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise LocalConfigurationError(f"Could not read {MCP_ENV_FILENAME} safely.") from None
    parsed = _parse_environment_file(contents)
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return path


def _connect_for_preflight() -> Any:
    settings = settings_from_environment()
    try:
        return psycopg2.connect(
            dsn=settings.database_url,
            connect_timeout=settings.connect_timeout_seconds,
            options="-c default_transaction_read_only=on",
        )
    except Exception:
        raise PreflightError("Database connection failed.") from None


def _check_database(connection: Any, expected_role: str) -> tuple[str, ...]:
    warnings: list[str] = []
    aou_enabled = os.environ.get("QG_MCP_AOU_ENABLED", "").strip() == "1"
    required_columns = {
        **{("public", name): columns for name, columns in _REQUIRED_RELATION_COLUMNS.items()},
        **(
            {("aou", name): columns for name, columns in _AOU_REQUIRED_RELATION_COLUMNS.items()}
            if aou_enabled
            else {}
        ),
    }
    required_indexes = {
        **{("public", name): columns for name, columns in _REQUIRED_INDEXED_COLUMNS.items()},
        **(
            {("aou", name): columns for name, columns in _AOU_REQUIRED_INDEXED_COLUMNS.items()}
            if aou_enabled
            else {}
        ),
    }
    public_relations = list(_REQUIRED_RELATION_COLUMNS)
    aou_relations = list(_AOU_REQUIRED_RELATION_COLUMNS) if aou_enabled else []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            row = cursor.fetchone()
            if not row or str(row[0]).lower() != "on":
                raise PreflightError("Database session is not read-only.")

            cursor.execute(
                """
                SELECT
                    session_user,
                    current_user,
                    rolsuper,
                    rolcreaterole,
                    rolcreatedb,
                    rolbypassrls,
                    rolreplication,
                    rolinherit,
                    rolcanlogin,
                    rolconnlimit,
                    COALESCE(rolconfig, ARRAY[]::text[])
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
            role = cursor.fetchone()
            if not role or role[0] != expected_role or role[1] != expected_role:
                raise PreflightError(
                    "Database session and current roles must both match "
                    "QG_MCP_EXPECTED_DB_ROLE."
                )
            if any(bool(value) for value in role[2:7]):
                raise PreflightError("Database role has administrative privileges.")
            if bool(role[7]):
                raise PreflightError("Database role must be configured NOINHERIT.")
            if not bool(role[8]) or not 1 <= int(role[9]) <= 4:
                raise PreflightError("Database role login or connection limit is unsafe.")
            required_settings = {
                "default_transaction_read_only=on",
                "statement_timeout=10s",
                "lock_timeout=1s",
                "idle_in_transaction_session_timeout=15s",
                "search_path=pg_catalog, public",
            }
            if not required_settings.issubset(set(role[10])):
                raise PreflightError("Database role defaults are incomplete.")

            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members membership
                    JOIN pg_roles member_role ON member_role.oid = membership.member
                    WHERE member_role.rolname = current_user
                )
                """
            )
            inherited_roles = cursor.fetchone()
            if not inherited_roles or bool(inherited_roles[0]):
                raise PreflightError("Database role must not inherit privileges from other roles.")

            cursor.execute(
                """
                SELECT
                    has_database_privilege(current_user, current_database(), 'CREATE'),
                    has_database_privilege(current_user, current_database(), 'TEMP'),
                    has_schema_privilege(current_user, 'public', 'CREATE')
                """
            )
            broad_privileges = cursor.fetchone()
            if not broad_privileges or bool(broad_privileges[0]) or bool(broad_privileges[2]):
                raise PreflightError("Database role has database or schema creation privileges.")
            if bool(broad_privileges[1]):
                warnings.append(
                    "Database role inherits TEMP privilege from PUBLIC; the MCP exposes no "
                    "arbitrary SQL and keeps connection and statement limits enforced."
                )

            if aou_enabled:
                cursor.execute(
                    """
                    SELECT
                        has_schema_privilege(current_user, 'aou', 'USAGE'),
                        has_schema_privilege(current_user, 'aou', 'CREATE')
                    """
                )
                aou_schema_privileges = cursor.fetchone()
                if (
                    not aou_schema_privileges
                    or not bool(aou_schema_privileges[0])
                    or bool(aou_schema_privileges[1])
                ):
                    raise PreflightError(
                        "Database role must have USAGE but not CREATE on the AoU schema."
                    )

            cursor.execute(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE (table_schema = 'public' AND table_name = ANY(%s))
                   OR (table_schema = 'aou' AND table_name = ANY(%s))
                """,
                (public_relations, aou_relations),
            )
            actual_columns: dict[tuple[str, str], set[str]] = {}
            for schema_name, table_name, column_name in cursor.fetchall():
                actual_columns.setdefault((schema_name, table_name), set()).add(
                    column_name
                )
            missing = {
                relation: columns - actual_columns.get(relation, set())
                for relation, columns in required_columns.items()
                if columns - actual_columns.get(relation, set())
            }
            if missing:
                raise PreflightError(
                    "Database schema is missing required MCP relations or columns."
                )

            cursor.execute(
                """
                SELECT n.nspname, c.relname, c.relkind
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE (n.nspname = 'public' AND c.relname = ANY(%s))
                   OR (n.nspname = 'aou' AND c.relname = ANY(%s))
                """,
                (public_relations, aou_relations),
            )
            relation_kinds = {
                (schema_name, relation): kind
                for schema_name, relation, kind in cursor.fetchall()
            }
            if set(relation_kinds) != set(required_columns) or any(
                kind not in {"r", "p", "m"} for kind in relation_kinds.values()
            ):
                raise PreflightError("Required MCP relations have unsupported relation kinds.")

            cursor.execute(
                """
                SELECT table_schema, table_name, column_name
                FROM (
                    SELECT DISTINCT
                        namespace.nspname AS table_schema,
                        table_class.relname AS table_name,
                        attribute.attname AS column_name
                    FROM pg_index index_record
                    JOIN pg_class table_class ON table_class.oid = index_record.indrelid
                    JOIN pg_namespace namespace ON namespace.oid = table_class.relnamespace
                    CROSS JOIN LATERAL unnest(index_record.indkey)
                        WITH ORDINALITY AS key_column(attribute_number, position)
                    JOIN pg_attribute attribute
                      ON attribute.attrelid = table_class.oid
                     AND attribute.attnum = key_column.attribute_number
                    WHERE (
                           (
                                namespace.nspname = 'public'
                            AND table_class.relname = ANY(%s)
                           )
                        OR (
                                namespace.nspname = 'aou'
                            AND table_class.relname = ANY(%s)
                           )
                    )
                      AND index_record.indisvalid
                      AND index_record.indisready
                ) indexed_columns
                """,
                (
                    list(_REQUIRED_INDEXED_COLUMNS),
                    list(_AOU_REQUIRED_INDEXED_COLUMNS) if aou_enabled else [],
                ),
            )
            actual_indexed: dict[tuple[str, str], set[str]] = {}
            for schema_name, table_name, column_name in cursor.fetchall():
                actual_indexed.setdefault((schema_name, table_name), set()).add(
                    column_name
                )
            missing_indexes = {
                relation: columns - actual_indexed.get(relation, set())
                for relation, columns in required_indexes.items()
                if columns - actual_indexed.get(relation, set())
            }
            if missing_indexes:
                raise PreflightError("Database schema is missing required MCP indexes.")

            if aou_enabled:
                cursor.execute(
                    """
                    SELECT index_class.relname
                    FROM pg_index index_record
                    JOIN pg_class index_class
                      ON index_class.oid = index_record.indexrelid
                    JOIN pg_namespace namespace
                      ON namespace.oid = index_class.relnamespace
                    WHERE namespace.nspname = 'aou'
                      AND index_class.relname = ANY(%s)
                      AND index_record.indisvalid
                      AND index_record.indisready
                    """,
                    (list(_AOU_REQUIRED_INDEX_NAMES),),
                )
                if {row[0] for row in cursor.fetchall()} != set(
                    _AOU_REQUIRED_INDEX_NAMES
                ):
                    raise PreflightError(
                        "Database schema is missing required AoU search indexes."
                    )
                cursor.execute(
                    """
                    SELECT
                        EXISTS (
                            SELECT 1
                            FROM aou.active_snapshots AS active
                            JOIN aou.search_docs AS document
                              ON document.snapshot_key = active.snapshot_key
                        ),
                        EXISTS (
                            SELECT 1
                            FROM aou.embedding_cache
                            WHERE dimensions = 1536
                        ),
                        to_regprocedure('similarity(text,text)') IS NOT NULL
                    """
                )
                aou_data = cursor.fetchone()
                if not aou_data or not all(bool(value) for value in aou_data):
                    raise PreflightError(
                        "The AoU schema lacks an active searchable snapshot, "
                        "embeddings, or pg_trgm."
                    )

            cursor.execute(
                """
                SELECT n.nspname, relname,
                       has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                       has_table_privilege(
                           current_user,
                           c.oid,
                           'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                       ) AS can_write
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE (n.nspname = 'public' AND relname = ANY(%s))
                   OR (n.nspname = 'aou' AND relname = ANY(%s))
                """,
                (public_relations, aou_relations),
            )
            privileges = {
                (schema_name, name): (can_select, can_write)
                for schema_name, name, can_select, can_write in cursor.fetchall()
            }
            if set(privileges) != set(required_columns):
                raise PreflightError("Database schema is missing required MCP relations.")
            if any(not can_select or can_write for can_select, can_write in privileges.values()):
                raise PreflightError("Database role is not SELECT-only on required MCP relations.")

            cursor.execute(
                """
                SELECT n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND NOT (
                         (n.nspname = 'public' AND c.relname = ANY(%s))
                      OR (n.nspname = 'aou' AND c.relname = ANY(%s))
                  )
                  AND has_table_privilege(current_user, c.oid, 'SELECT')
                LIMIT 1
                """,
                (public_relations, aou_relations),
            )
            if cursor.fetchone() is not None:
                raise PreflightError("Database role can read relations outside the MCP allowlist.")

            cursor.execute(
                """
                SELECT n.nspname, c.relname, attribute.attname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute attribute ON attribute.attrelid = c.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND NOT (
                         (n.nspname = 'public' AND c.relname = ANY(%s))
                      OR (n.nspname = 'aou' AND c.relname = ANY(%s))
                  )
                  AND has_column_privilege(
                      current_user, c.oid, attribute.attnum, 'SELECT'
                  )
                LIMIT 1
                """,
                (public_relations, aou_relations),
            )
            if cursor.fetchone() is not None:
                raise PreflightError(
                    "Database role can read columns outside the MCP allowlist."
                )

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND has_table_privilege(
                      current_user,
                      c.oid,
                      'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                  )
                """
            )
            writable = cursor.fetchone()
            if not writable or int(writable[0]) != 0:
                raise PreflightError("Database role can write to non-system relations.")

            cursor.execute(
                """
                SELECT n.nspname, c.relname, attribute.attname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute attribute ON attribute.attrelid = c.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND has_column_privilege(
                      current_user,
                      c.oid,
                      attribute.attnum,
                      'INSERT,UPDATE,REFERENCES'
                  )
                LIMIT 1
                """
            )
            if cursor.fetchone() is not None:
                raise PreflightError(
                    "Database role has column-level write privileges."
                )

            cursor.execute(
                """
                SELECT n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind = 'S'
                  AND has_sequence_privilege(
                      current_user, c.oid, 'SELECT,USAGE,UPDATE'
                  )
                LIMIT 1
                """
            )
            if cursor.fetchone() is not None:
                raise PreflightError("Database role has non-system sequence privileges.")

            cursor.execute(
                """
                SELECT
                    COALESCE(extension.extname, ''),
                    language.lanpltrusted,
                    routine.prosecdef
                FROM pg_proc routine
                JOIN pg_namespace namespace ON namespace.oid = routine.pronamespace
                JOIN pg_language language ON language.oid = routine.prolang
                LEFT JOIN pg_depend dependency
                  ON dependency.classid = 'pg_proc'::regclass
                 AND dependency.objid = routine.oid
                 AND dependency.deptype = 'e'
                LEFT JOIN pg_extension extension ON extension.oid = dependency.refobjid
                WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND has_schema_privilege(current_user, namespace.oid, 'USAGE')
                  AND has_function_privilege(current_user, routine.oid, 'EXECUTE')
                """
            )
            inherited_safe_routine = False
            for extension_name, trusted_language, security_definer in cursor.fetchall():
                if extension_name in {"vector", "pg_trgm"} and not security_definer:
                    continue
                if extension_name or security_definer or not trusted_language:
                    raise PreflightError("Database role can execute an unsafe routine.")
                inherited_safe_routine = True
            if inherited_safe_routine:
                warnings.append(
                    "Database role inherits EXECUTE on trusted security-invoker routines; "
                    "those routines retain invoker permissions and the MCP exposes no "
                    "arbitrary SQL."
                )

            cursor.execute("SELECT to_regtype('vector') IS NOT NULL")
            vector = cursor.fetchone()
            if not vector or vector[0] is not True:
                raise PreflightError("Database schema is missing the vector type.")
            return tuple(warnings)
    except PreflightError:
        raise
    except Exception:
        raise PreflightError("Database schema and role checks failed.") from None


def _embeddings_enabled() -> bool:
    value = os.getenv("QG_MCP_EMBEDDINGS_ENABLED")
    return (value or "1").strip().lower() not in {"0", "false", "no", "off"}


def _is_transient_embedding_error(error: Exception) -> bool:
    """Return whether an OpenAI probe failure is safe to serve through.

    Hosted startup may continue in keyword-only mode for failures that can
    reasonably clear without a configuration change: transport failures,
    provider timeouts, rate limits, and provider 5xx responses. Authentication,
    permission, model, and bad-request failures remain fatal API status errors.
    """
    if isinstance(error, (APIConnectionError, RateLimitError)):
        return True
    return isinstance(error, APIStatusError) and 500 <= error.status_code <= 599


def _check_embeddings(
    *,
    consume_budget: bool = True,
    allow_transient_failure: bool = False,
) -> str:
    if not _embeddings_enabled():
        return "disabled explicitly; keyword retrieval only"
    try:
        provider = OpenAIEmbeddingProvider.from_environment()
    except Exception:
        raise PreflightError("Embedding configuration is invalid.") from None
    if provider is None:
        raise PreflightError(
            "Embeddings are enabled but OPENAI_API_KEY is absent; set the key or "
            "disable embeddings explicitly."
        )
    try:
        if consume_budget:
            vector = provider("QueryGaP MCP startup preflight")
        else:
            vector = provider.startup_probe("QueryGaP MCP startup preflight")
    except EmbeddingBudgetExceeded:
        if not consume_budget:
            return "daily budget unavailable or exhausted; keyword retrieval only"
        raise PreflightError("Embedding provider check failed.") from None
    except Exception as error:
        if allow_transient_failure and _is_transient_embedding_error(error):
            return "provider temporarily unavailable; keyword retrieval only"
        raise PreflightError("Embedding provider check failed.") from None
    if len(vector) != EMBEDDING_DIMENSIONS or not all(
        math.isfinite(value) for value in vector
    ):
        raise PreflightError("Embedding provider returned an invalid vector.")
    return "enabled and reachable"


def run_preflight(
    *,
    consume_embedding_budget: bool = True,
    allow_transient_embedding_failure: bool = False,
) -> PreflightReport:
    """Verify database and embeddings with strict failure handling by default."""
    expected_role = os.getenv("QG_MCP_EXPECTED_DB_ROLE", "").strip()
    if not expected_role:
        raise PreflightError("QG_MCP_EXPECTED_DB_ROLE is required.")
    try:
        connection = _connect_for_preflight()
        try:
            warnings = _check_database(connection, expected_role)
        finally:
            connection.rollback()
            connection.close()
    except (DatabaseConfigurationError, PreflightError):
        raise
    except Exception:
        raise PreflightError("Database preflight failed.") from None
    return PreflightReport(
        embeddings=_check_embeddings(
            consume_budget=consume_embedding_budget,
            allow_transient_failure=allow_transient_embedding_failure,
        ),
        warnings=warnings,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preflight and run QueryGaP MCP locally.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument(
        "--env-file",
        type=Path,
        help=f"MCP-only environment file (default: repository {MCP_ENV_FILENAME}).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate configuration and dependencies without starting the server.",
    )
    action.add_argument(
        "--live-tests",
        action="store_true",
        help="Run the opt-in live MCP protocol suite after preflight, then exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise LocalConfigurationError("port must be between 1 and 65535")
    if not args.path.startswith("/") or "?" in args.path or "#" in args.path:
        raise LocalConfigurationError("path must be an absolute URL path")

    try:
        load_local_environment(args.env_file)
        report = run_preflight()
    except (DatabaseConfigurationError, LocalConfigurationError, PreflightError) as error:
        print(f"QueryGaP MCP preflight failed: {error}", file=sys.stderr)
        return 2

    print(f"Database preflight: {report.database}", file=sys.stderr)
    print(f"Transaction preflight: {report.transactions}", file=sys.stderr)
    print(f"Embedding preflight: {report.embeddings}", file=sys.stderr)
    for warning in report.warnings:
        print(f"Preflight warning: {warning}", file=sys.stderr)
    if args.preflight_only:
        return 0
    if args.live_tests:
        environment = os.environ.copy()
        environment["QG_MCP_LIVE_TESTS"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/mcp_live"],
            cwd=repository_root(),
            env=environment,
            check=False,
        )
        return completed.returncode

    print(f"Starting QueryGaP MCP at http://{LOCAL_HOST}:{args.port}{args.path}", file=sys.stderr)
    from querygap_mcp.database import close_pool
    from querygap_mcp.server import mcp

    try:
        try:
            mcp.run(
                transport="streamable-http",
                host=LOCAL_HOST,
                port=args.port,
                streamable_http_path=args.path,
                stateless_http=True,
                json_response=True,
                max_request_body_size=65_536,
            )
        except KeyboardInterrupt:
            pass
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
