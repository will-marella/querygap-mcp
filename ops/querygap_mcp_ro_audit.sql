-- Non-secret audit for the QueryGaP MCP database identity.
-- Run while connected to the catalog database as querygap_mcp_ro. Output is
-- limited to check names, PASS/WARN/FAIL, and non-sensitive remediation
-- summaries.

WITH required_relations(relation_name) AS (
    VALUES
        ('studies'),
        ('datasets'),
        ('variables'),
        ('variable_reports'),
        ('documents'),
        ('ukb_search_docs_fields'),
        ('ukb_field_page_data'),
        ('ukb_field_instance_stats')
),
target_role AS (
    SELECT r.oid AS role_oid
    FROM pg_catalog.pg_roles r
    WHERE r.rolname = 'querygap_mcp_ro'
),
session_identity AS (
    SELECT
        session_user::text AS session_role,
        current_user::text AS current_role
),
role_state AS (
    SELECT
        r.rolsuper,
        r.rolcreatedb,
        r.rolcreaterole,
        r.rolreplication,
        r.rolbypassrls,
        r.rolinherit,
        r.rolcanlogin,
        r.rolconnlimit
    FROM pg_catalog.pg_roles r
    WHERE r.rolname = 'querygap_mcp_ro'
),
relation_state AS (
    SELECT
        rr.relation_name,
        to_regclass(format('public.%I', rr.relation_name)) AS relation_oid
    FROM required_relations rr
),
unexpected_access AS (
    SELECT c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN target_role target
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND NOT (
          n.nspname = 'public'
          AND c.oid = ANY (
              ARRAY(
                  SELECT rs.relation_oid
                  FROM relation_state rs
                  WHERE rs.relation_oid IS NOT NULL
              )
          )
      )
      AND has_table_privilege(target.role_oid, c.oid, 'SELECT')
),
unexpected_column_access AS (
    SELECT attribute.attrelid, attribute.attnum
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute attribute ON attribute.attrelid = c.oid
    CROSS JOIN target_role target
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND NOT (
          n.nspname = 'public'
          AND c.oid = ANY (
              ARRAY(
                  SELECT rs.relation_oid
                  FROM relation_state rs
                  WHERE rs.relation_oid IS NOT NULL
              )
          )
      )
      AND has_column_privilege(
          target.role_oid, c.oid, attribute.attnum, 'SELECT'
      )
),
unexpected_write AS (
    SELECT c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN target_role target
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_table_privilege(
          target.role_oid, c.oid,
          'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
      )
),
unexpected_column_write AS (
    SELECT attribute.attrelid, attribute.attnum
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute attribute ON attribute.attrelid = c.oid
    CROSS JOIN target_role target
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND has_column_privilege(
          target.role_oid,
          c.oid,
          attribute.attnum,
          'INSERT, UPDATE, REFERENCES'
      )
),
unexpected_sequence_access AS (
    SELECT c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN target_role target
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.relkind = 'S'
      AND has_sequence_privilege(
          target.role_oid, c.oid, 'SELECT, USAGE, UPDATE'
      )
),
executable_routines AS (
    SELECT
        COALESCE(extension.extname, '') AS extension_name,
        language.lanpltrusted,
        routine.prosecdef
    FROM pg_catalog.pg_proc routine
    JOIN pg_catalog.pg_namespace namespace ON namespace.oid = routine.pronamespace
    JOIN pg_catalog.pg_language language ON language.oid = routine.prolang
    CROSS JOIN target_role target
    LEFT JOIN pg_catalog.pg_depend dependency
      ON dependency.classid = 'pg_catalog.pg_proc'::regclass
     AND dependency.objid = routine.oid
     AND dependency.deptype = 'e'
    LEFT JOIN pg_catalog.pg_extension extension ON extension.oid = dependency.refobjid
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND has_schema_privilege(target.role_oid, namespace.oid, 'USAGE')
      AND has_function_privilege(target.role_oid, routine.oid, 'EXECUTE')
),
role_settings AS (
    SELECT COALESCE(array_agg(setting), ARRAY[]::text[]) AS settings
    FROM (
        SELECT unnest(COALESCE(r.rolconfig, ARRAY[]::text[])) AS setting
        FROM pg_catalog.pg_roles r
        WHERE r.rolname = 'querygap_mcp_ro'
    ) s
),
checks(check_name, passed, detail) AS (
    SELECT
        'role exists',
        EXISTS (SELECT 1 FROM role_state),
        'Create querygap_mcp_ro with a parameterized generated password.'
    UNION ALL
    SELECT
        'connected identity is target role',
        COALESCE((SELECT
            session_role = 'querygap_mcp_ro'
            AND current_role = 'querygap_mcp_ro'
        FROM session_identity), false),
        'Connect directly as querygap_mcp_ro; do not rely on SET ROLE.'
    UNION ALL
    SELECT
        'dangerous role attributes absent',
        COALESCE((SELECT NOT (
            rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
            OR rolbypassrls OR rolinherit
        ) FROM role_state), false),
        'Require NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS.'
    UNION ALL
    SELECT
        'login enabled with bounded connections',
        COALESCE((SELECT rolcanlogin AND rolconnlimit BETWEEN 1 AND 4 FROM role_state), false),
        'Enable LOGIN and set CONNECTION LIMIT no higher than 4.'
    UNION ALL
    SELECT
        'no role memberships',
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members m
            JOIN pg_catalog.pg_roles member ON member.oid = m.member
            WHERE member.rolname = 'querygap_mcp_ro'
        ),
        'Remove memberships; privileges inherited or reachable with SET ROLE are out of scope.'
    UNION ALL
    SELECT
        'required relations exist',
        NOT EXISTS (SELECT 1 FROM relation_state WHERE relation_oid IS NULL),
        'Apply the expected QueryGaP catalog schema before granting access.'
    UNION ALL
    SELECT
        'required relations selectable',
        NOT EXISTS (
            SELECT 1 FROM relation_state
            WHERE relation_oid IS NULL
               OR NOT COALESCE(has_table_privilege(
                   (SELECT role_oid FROM target_role), relation_oid, 'SELECT'
               ), false)
        ),
        'Grant SELECT on exactly the eight required catalog relations.'
    UNION ALL
    SELECT
        'required relations are read only',
        NOT EXISTS (
            SELECT 1 FROM relation_state
            WHERE relation_oid IS NOT NULL
              AND COALESCE(has_table_privilege(
                  (SELECT role_oid FROM target_role), relation_oid,
                  'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
              ), false)
        ),
        'Remove all effective write privileges, including those from PUBLIC, ownership, or membership.'
    UNION ALL
    SELECT
        'no other non-system relations selectable',
        NOT EXISTS (SELECT 1 FROM unexpected_access),
        'Remove effective SELECT outside the eight-relation allowlist.'
    UNION ALL
    SELECT
        'no other non-system columns selectable',
        NOT EXISTS (SELECT 1 FROM unexpected_column_access),
        'Remove effective column-level SELECT outside the eight-relation allowlist.'
    UNION ALL
    SELECT
        'no effective writes on non-system relations',
        NOT EXISTS (SELECT 1 FROM unexpected_write),
        'Remove all effective relation writes, including PUBLIC, ownership, and memberships.'
    UNION ALL
    SELECT
        'no effective column writes on non-system relations',
        NOT EXISTS (SELECT 1 FROM unexpected_column_write),
        'Remove effective column-level INSERT, UPDATE, and REFERENCES privileges.'
    UNION ALL
    SELECT
        'no non-system sequence privileges',
        NOT EXISTS (SELECT 1 FROM unexpected_sequence_access),
        'Remove effective SELECT, USAGE, and UPDATE privileges from every sequence.'
    UNION ALL
    SELECT
        'no unsafe executable routines',
        NOT EXISTS (
            SELECT 1
            FROM executable_routines
            WHERE prosecdef
               OR (extension_name <> '' AND extension_name <> 'vector')
               OR (extension_name = '' AND NOT lanpltrusted)
        ),
        'Revoke unsafe routine execution or isolate the MCP catalog database.'
    UNION ALL
    SELECT
        'public schema usable but not creatable',
        COALESCE(has_schema_privilege(
            (SELECT role_oid FROM target_role), 'public', 'USAGE'
        ), false)
        AND NOT COALESCE(has_schema_privilege(
            (SELECT role_oid FROM target_role), 'public', 'CREATE'
        ), false),
        'Grant USAGE only; revoke CREATE directly and from PUBLIC if PUBLIC supplies it.'
    UNION ALL
    SELECT
        'database connect allowed',
        COALESCE(has_database_privilege(
            (SELECT role_oid FROM target_role), current_database(), 'CONNECT'
        ), false),
        'Grant CONNECT on the catalog database.'
    UNION ALL
    SELECT
        'database CREATE privilege denied',
        COALESCE(NOT has_database_privilege(
            (SELECT role_oid FROM target_role), current_database(), 'CREATE'
        ), false),
        'Revoke CREATE on the catalog database directly and through inherited grants.'
    UNION ALL
    SELECT
        'read-only defaults and timeouts set',
        settings @> ARRAY[
            'default_transaction_read_only=on',
            'statement_timeout=10s',
            'lock_timeout=1s',
            'idle_in_transaction_session_timeout=15s',
            'search_path=pg_catalog, public'
        ],
        'Apply the role defaults from ops/querygap_mcp_ro.sql.'
    FROM role_settings
),
results(check_name, status, remediation) AS (
    SELECT
        check_name,
        CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END,
        CASE WHEN passed THEN 'Configured as required.' ELSE detail END
    FROM checks
    UNION ALL
    SELECT
        'temporary objects denied',
        CASE
            WHEN COALESCE(has_database_privilege(
                (SELECT role_oid FROM target_role), current_database(), 'TEMP'
            ), false) THEN 'WARN'
            ELSE 'PASS'
        END,
        CASE
            WHEN COALESCE(has_database_privilege(
                (SELECT role_oid FROM target_role), current_database(), 'TEMP'
            ), false)
            THEN 'TEMP is inherited from PUBLIC; acceptable only for bounded local testing.'
            ELSE 'Configured as required.'
        END
    UNION ALL
    SELECT
        'trusted security-invoker routines inherited',
        CASE
            WHEN EXISTS (
                SELECT 1 FROM executable_routines
                WHERE extension_name = '' AND lanpltrusted AND NOT prosecdef
            ) THEN 'WARN'
            ELSE 'PASS'
        END,
        CASE
            WHEN EXISTS (
                SELECT 1 FROM executable_routines
                WHERE extension_name = '' AND lanpltrusted AND NOT prosecdef
            )
            THEN 'Invoker permissions remain enforced; acceptable only without arbitrary SQL.'
            ELSE 'Configured as required.'
        END
)
SELECT check_name, status, remediation
FROM results
ORDER BY check_name;
