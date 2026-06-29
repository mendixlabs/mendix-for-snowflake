-- =============================================================================
-- Mendix-on-SPCS Native App - setup script
--
-- Runs as the application (app role) on install and upgrade. Replaces:
--   Controller/sql/setup.sql, Controller/setup.ps1, Admin UI/setup.ps1.
--
-- The app role owns everything created here, so the object-level GRANTs the old
-- setup.sql issued to MENDIX_DEPLOY_CONTROLLER_ROLE are gone - ownership covers them.
--
-- Provisioning order is driven by Snowflake's callbacks, not by this script's
-- top-to-bottom flow:
--   1. install            -> this script runs: roles, schema, tables, stage, procs
--   2. consumer grants     -> grant_callback(privileges) creates pool + warehouse
--   3. consumer binds refs -> register_reference(...) binds pg_secret / pg_eai
--   4. when (2) and (3) are both satisfied -> services start (idempotent)
--
-- Image paths are the fixed provider-side FQN, identical to manifest.yml's
-- allow-list. The <PROVIDER_*> tokens are substituted by scripts/build-and-push.ps1.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Application role + public (non-versioned) schema
--    Services and runtime-created objects MUST live in a non-versioned schema.
-- -----------------------------------------------------------------------------
CREATE APPLICATION ROLE IF NOT EXISTS app_admin;
CREATE SCHEMA IF NOT EXISTS app_public;
GRANT USAGE ON SCHEMA app_public TO APPLICATION ROLE app_admin;

-- -----------------------------------------------------------------------------
-- 2. App registry + activity audit log (was YOUR_DB.PUBLIC, now the app namespace)
--    DDL unchanged from Controller/sql/setup.sql except the schema prefix. The app
--    role owns these, so no per-table GRANTs to a controller role are needed.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_public.MENDIX_APPS (
    name               VARCHAR    NOT NULL PRIMARY KEY,
    service_name       VARCHAR    NOT NULL,
    pg_database        VARCHAR    NOT NULL,
    resource_tier      VARCHAR    DEFAULT 'medium',
    use_caller_rights  BOOLEAN    DEFAULT FALSE,
    constants          VARIANT,                      -- { "Module.Name": "value" }
    pad_stage_path     VARCHAR,                      -- relative: apps/{name}/current.zip
    endpoint_url       VARCHAR,
    last_deploy_status VARCHAR,                      -- DEPLOYING | READY | FAILED
    created_at         TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    last_deployed_at   TIMESTAMP,
    owner_role         VARCHAR    DEFAULT 'MENDIX_ADMIN_OPERATOR_ROLE'  -- management-plane owner
);

-- The controller used to create this at startup (activity.py::init_table); pre-create
-- it here so the schema is complete on install.
CREATE TABLE IF NOT EXISTS app_public.MENDIX_ACTIVITY (
    id        NUMBER AUTOINCREMENT PRIMARY KEY,
    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    operator  VARCHAR,
    action    VARCHAR,
    app_name  VARCHAR,
    detail    VARIANT,
    result    VARCHAR
);

-- -----------------------------------------------------------------------------
-- 3. Deploy stage (PAD zips land here, mounted into the controller + app services)
-- -----------------------------------------------------------------------------
CREATE STAGE IF NOT EXISTS app_public.MENDIX_DEPLOY_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Single-row readiness state. Each callback records its own precondition here and
-- maybe_start_services gates on all three, deterministically (SYSTEM$GET_ALL_REFERENCES
-- does not return a clean empty sentinel for declared-but-unbound references, so we
-- track bind state ourselves rather than probe it).
CREATE TABLE IF NOT EXISTS app_public.install_state (
    pool_ready   BOOLEAN DEFAULT FALSE,   -- grant_callback created pool + warehouse
    secret_bound BOOLEAN DEFAULT FALSE,   -- pg_secret reference bound
    eai_bound    BOOLEAN DEFAULT FALSE    -- pg_eai reference bound
);
INSERT INTO app_public.install_state (pool_ready, secret_bound, eai_bound)
    SELECT FALSE, FALSE, FALSE WHERE NOT EXISTS (SELECT 1 FROM app_public.install_state);

-- -----------------------------------------------------------------------------
-- 4. grant_callback - REQUIRED for apps with containers.
--    Invoked by Snowflake after the consumer grants the manifest privileges.
--    Creates the compute pool + warehouse (named off the app database so two
--    installs in one account do not collide), then attempts to start services.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.grant_callback(privileges ARRAY)
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
DECLARE
    app_db   STRING;
    pool     STRING;
    wh       STRING;
BEGIN
    app_db := CURRENT_DATABASE();
    pool   := app_db || '_COMPUTE_POOL';
    wh     := app_db || '_WH';

    IF (ARRAY_CONTAINS('CREATE COMPUTE POOL'::VARIANT, :privileges)) THEN
        EXECUTE IMMEDIATE
            'CREATE COMPUTE POOL IF NOT EXISTS ' || pool ||
            ' MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS' ||
            ' AUTO_RESUME = TRUE AUTO_SUSPEND_SECS = 3600';
    END IF;

    IF (ARRAY_CONTAINS('CREATE WAREHOUSE'::VARIANT, :privileges)) THEN
        EXECUTE IMMEDIATE
            'CREATE WAREHOUSE IF NOT EXISTS ' || wh ||
            ' WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE' ||
            ' INITIALLY_SUSPENDED = TRUE';
    END IF;

    UPDATE app_public.install_state SET pool_ready = TRUE;
    CALL app_public.maybe_start_services();
    RETURN 'grant_callback: pool=' || pool || ' wh=' || wh;
END;
$$;

-- -----------------------------------------------------------------------------
-- 5. configuration_callback - REQUIRED for SECRET / EXTERNAL_ACCESS_INTEGRATION
--    references. Returns the JSON config Snowsight shows in the consumer bind
--    dialog. Envelope: { "type": "CONFIGURATION", "payload": { ... } }.
--    (Payload shapes per docs.snowflake.com/.../container-eai-example.)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.get_reference_config(ref_name STRING)
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
BEGIN
    CASE (ref_name)
        WHEN 'pg_secret' THEN
            -- A Snowflake-managed-Postgres credential is an opaque string secret.
            -- VERIFY: docs give a full payload only for OAUTH2; GENERIC_STRING shows
            -- only payload.type. Confirm no extra keys are required at bind time.
            RETURN '{"type": "CONFIGURATION", "payload": {"type": "GENERIC_STRING"}}';
        WHEN 'pg_eai' THEN
            -- The egress host:port is consumer-specific (their PG instance), so the
            -- consumer supplies it when binding; allowed_secrets = LIST ties the EAI
            -- to the pg_secret they also bind.
            RETURN '{"type": "CONFIGURATION", "payload": {"host_ports": [], "allowed_secrets": "LIST", "secret_references": ["pg_secret"]}}';
        ELSE
            RETURN '{"type": "ERROR", "payload": {"message": "unknown reference: ' || ref_name || '"}}';
    END;
END;
$$;

-- -----------------------------------------------------------------------------
-- 6. register_reference - the manifest reference callback (pg_secret, pg_eai).
--    ADD / REMOVE / CLEAR dispatch per docs. After binding, attempt to start.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.register_reference(ref_name STRING, operation STRING, ref_or_alias STRING)
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
DECLARE
    is_add BOOLEAN;
BEGIN
    CASE (operation)
        WHEN 'ADD' THEN
            SELECT SYSTEM$SET_REFERENCE(:ref_name, :ref_or_alias);
        WHEN 'REMOVE' THEN
            SELECT SYSTEM$REMOVE_REFERENCE(:ref_name, :ref_or_alias);
        WHEN 'CLEAR' THEN
            SELECT SYSTEM$REMOVE_ALL_REFERENCES(:ref_name);
        ELSE
            RETURN 'unknown operation: ' || operation;
    END;

    -- Track bind state deterministically (used by maybe_start_services).
    is_add := (operation = 'ADD');
    IF (ref_name = 'pg_secret') THEN
        UPDATE app_public.install_state SET secret_bound = :is_add;
    ELSEIF (ref_name = 'pg_eai') THEN
        UPDATE app_public.install_state SET eai_bound = :is_add;
    END IF;

    CALL app_public.maybe_start_services();
    RETURN 'registered ' || ref_name || ' (' || operation || ')';
END;
$$;

-- -----------------------------------------------------------------------------
-- 7. maybe_start_services - idempotent readiness gate.
--    Starts the controller + admin UI only once BOTH the compute pool exists
--    (grant_callback ran) AND both references are bound (register_reference ran).
--    Safe to call from either callback; CREATE SERVICE IF NOT EXISTS makes it
--    a no-op once started.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.maybe_start_services()
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
DECLARE
    ready BOOLEAN DEFAULT FALSE;
BEGIN
    -- All preconditions met? pool + warehouse created (grant_callback) AND both
    -- references bound (register_reference). Tracked deterministically in install_state.
    SELECT pool_ready AND secret_bound AND eai_bound INTO :ready FROM app_public.install_state;
    IF (NOT ready) THEN
        RETURN 'waiting: need privileges granted (pool/warehouse) and pg_secret + pg_eai bound';
    END IF;

    CALL app_public.start_controller();
    CALL app_public.start_admin_ui();
    RETURN 'services starting';
END;
$$;

-- -----------------------------------------------------------------------------
-- 8. start_controller - ports Controller/setup.ps1 step 4 into SQL.
--    Differences vs. today:
--      - DB_SCHEMA env is the app's own <db>.APP_PUBLIC (resolved at runtime).
--      - IMAGE_REPO env is the fixed provider FQN of the mendix-base image.
--      - PG_EAI env is reference('pg_eai') so the controller's per-app CREATE
--        SERVICE emits EXTERNAL_ACCESS_INTEGRATIONS = (reference('pg_eai')).
--      - The bootstrap PG credential mounts via the bound pg_secret reference
--        (snowflakeSecret.objectReference, NOT reference() which is SQL-level only)
--        instead of the CTRL_PG_HOST / CTRL_PG_PASS secrets created by setup.ps1.
--
--    PHASE 2 CONTROLLER CODE CHANGE REQUIRED: today the controller reads two files
--    /secrets/pg_host and /secrets/pg_pass. With a single bound pg_secret it must
--    read one secret and split host:port from password. Tracked in PLAN section 5.4.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.start_controller()
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
DECLARE
    app_db    STRING;
    db_schema STRING;
    pool      STRING;
    wh        STRING;
    spec      STRING;
    ddl       STRING;
    dollar    STRING;
BEGIN
    app_db    := CURRENT_DATABASE();
    db_schema := app_db || '.APP_PUBLIC';
    pool      := app_db || '_COMPUTE_POOL';
    wh        := app_db || '_WH';
    -- Built at runtime so no literal dollar-quote delimiter appears in this proc
    -- body (which is itself dollar-quoted); used to quote the inline service spec.
    dollar    := '$' || '$';
    spec :=
'spec:
  containers:
  - name: controller
    image: /<PROVIDER_DB>/<PROVIDER_SCHEMA>/<REPO>/mendix-deploy-controller:latest
    env:
      COMPUTE_POOL: ' || pool || '
      IMAGE_REPO: <PROVIDER_DB>/<PROVIDER_SCHEMA>/<REPO>/mendix-base
      DB_SCHEMA: ' || db_schema || '
      PG_EAI: reference(''pg_eai'')
      QUERY_WAREHOUSE: ' || wh || '
      CONTROLLER_SERVICE_NAME: MENDIX_DEPLOY_CONTROLLER
      ADMIN_UI_SERVICE_NAME: MENDIX_DEPLOY_ADMIN_UI
    secrets:
    - snowflakeSecret:
        objectReference: ''pg_secret''
      directoryPath: /secrets/pg
    readinessProbe:
      port: 8080
      path: /health
    volumeMounts:
    - name: deploy-stage
      mountPath: /mnt/deploy-stage
  volumes:
  - name: deploy-stage
    source: stage
    stageConfig:
      name: "@' || db_schema || '.MENDIX_DEPLOY_STAGE"
  endpoints:
  - name: controller-api
    port: 8080
    public: true
capabilities:
  securityContext:
    executeAsCaller: true';

    ddl := 'CREATE SERVICE IF NOT EXISTS ' || db_schema || '.MENDIX_DEPLOY_CONTROLLER' ||
           ' IN COMPUTE POOL ' || pool ||
           ' FROM SPECIFICATION ' || dollar || spec || dollar ||
           ' MIN_INSTANCES = 1 MAX_INSTANCES = 1 QUERY_WAREHOUSE = ' || wh;
    EXECUTE IMMEDIATE ddl;

    -- SERVICE_CALLER_TOKEN_VALIDITY_SECS is NOT set here: setting it on a service
    -- requires MANAGE SERVICE CALLER ACCESS on the account, which no application
    -- object can hold. The consumer's ACCOUNTADMIN sets it once at the account
    -- level (Setup/Verify page, step 5); the value cascades to this service.
    -- executeAsCaller caller sessions need that non-default validity to avoid
    -- OAUTH_ACCESS_TOKEN_EXPIRED.

    -- Power-user / CLI access to the controller API.
    EXECUTE IMMEDIATE 'GRANT SERVICE ROLE ' || db_schema ||
        '.MENDIX_DEPLOY_CONTROLLER!ALL_ENDPOINTS_USAGE TO APPLICATION ROLE app_admin';

    RETURN 'controller started';
END;
$$;

-- -----------------------------------------------------------------------------
-- 9. start_admin_ui - ports Admin UI/setup.ps1 step 2 into SQL.
--    Both services are owned by the single app role now, so the separate
--    MENDIX_ADMIN_UI_ROLE / cross-role MONITOR grant from setup.ps1 is dropped.
--    Endpoint access goes to app_admin (operators are granted app_admin post-install).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE app_public.start_admin_ui()
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
DECLARE
    app_db    STRING;
    db_schema STRING;
    pool      STRING;
    wh        STRING;
    spec      STRING;
    ddl       STRING;
    dollar    STRING;
BEGIN
    app_db    := CURRENT_DATABASE();
    db_schema := app_db || '.APP_PUBLIC';
    pool      := app_db || '_COMPUTE_POOL';
    wh        := app_db || '_WH';
    dollar    := '$' || '$';   -- avoid a literal dollar-quote delimiter in this body
    -- Internal DNS: same-schema service reachable by its lowercased dashed name.
    -- VERIFY this short form resolves inside a Native App namespace.
    spec :=
'spec:
  containers:
  - name: streamlit
    image: /<PROVIDER_DB>/<PROVIDER_SCHEMA>/<REPO>/mendix-admin-ui:latest
    env:
      CONTROLLER_URL: http://mendix-deploy-controller:8080
      STREAMLIT_SERVER_MAX_UPLOAD_SIZE: "1024"
      STREAMLIT_THEME_BASE: "dark"
      STREAMLIT_THEME_PRIMARY_COLOR: "#00bde3"
      STREAMLIT_THEME_BACKGROUND_COLOR: "#0f1619"
      STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR: "#283236"
      STREAMLIT_THEME_TEXT_COLOR: "#f5fcff"
    readinessProbe:
      port: 8501
      path: /_stcore/health
  endpoints:
  - name: streamlit
    port: 8501
    public: true
capabilities:
  securityContext:
    executeAsCaller: true';

    ddl := 'CREATE SERVICE IF NOT EXISTS ' || db_schema || '.MENDIX_DEPLOY_ADMIN_UI' ||
           ' IN COMPUTE POOL ' || pool ||
           ' FROM SPECIFICATION ' || dollar || spec || dollar ||
           ' MIN_INSTANCES = 1 MAX_INSTANCES = 1 QUERY_WAREHOUSE = ' || wh;
    EXECUTE IMMEDIATE ddl;

    -- SERVICE_CALLER_TOKEN_VALIDITY_SECS is set account-level by the consumer
    -- (see start_controller for why); it cascades to this service.

    EXECUTE IMMEDIATE 'GRANT SERVICE ROLE ' || db_schema ||
        '.MENDIX_DEPLOY_ADMIN_UI!ALL_ENDPOINTS_USAGE TO APPLICATION ROLE app_admin';

    RETURN 'admin_ui started';
END;
$$;

-- -----------------------------------------------------------------------------
-- 10. Grant the manifest callbacks to an application role so the framework can
--     invoke them, plus a manual re-run helper for operators.
-- -----------------------------------------------------------------------------
GRANT USAGE ON PROCEDURE app_public.grant_callback(ARRAY)                       TO APPLICATION ROLE app_admin;
GRANT USAGE ON PROCEDURE app_public.get_reference_config(STRING)                TO APPLICATION ROLE app_admin;
GRANT USAGE ON PROCEDURE app_public.register_reference(STRING, STRING, STRING)  TO APPLICATION ROLE app_admin;
GRANT USAGE ON PROCEDURE app_public.maybe_start_services()                      TO APPLICATION ROLE app_admin;
