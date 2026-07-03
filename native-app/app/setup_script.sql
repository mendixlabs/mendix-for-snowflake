-- =============================================================================
-- Mendix-on-SPCS Native App - setup script
--
-- Runs as the application (app role) on install and upgrade.
--
-- The app role owns everything created here, so no object-level GRANTs to a
-- separate controller role are needed - ownership covers them.
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
-- 2. App registry + activity audit log
--    The app role owns these, so no per-table GRANTs are needed.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_public.MENDIX_APPS (
    name               VARCHAR    NOT NULL PRIMARY KEY,
    service_name       VARCHAR    NOT NULL,
    app_schema         VARCHAR,                      -- per-app schema (MXAPP_<NAME>) holding secrets + filestorage stage
    pg_database        VARCHAR    NOT NULL,
    resource_tier      VARCHAR    DEFAULT 'medium',
    use_caller_rights  BOOLEAN    DEFAULT FALSE,
    constants          VARIANT,                      -- { "Module.Name": "value" }
    pad_stage_path     VARCHAR,                      -- relative: apps/{name}/current.zip
    endpoint_url       VARCHAR,
    last_deploy_status VARCHAR,                      -- DEPLOYING | READY | FAILED
    created_at         TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    last_deployed_at   TIMESTAMP,
    owner_role         VARCHAR    DEFAULT 'MENDIX_ADMIN_OPERATOR_ROLE',  -- management-plane owner
    license_id         VARCHAR,                      -- Mendix License ID (identifier, not a credential; the key lives only in the MX_LICENSE_KEY secret)
    user_roles         VARIANT,   -- Mendix userrole names detected from the PAD's model/metadata.json at deploy
    role_mapping       VARIANT    -- operator-set map: Snowflake account role (UPPER) -> Mendix userrole; not a secret
);
-- CREATE IF NOT EXISTS never adds columns to an existing table; upgrades from
-- versions without app_schema need the explicit ALTER. Rows written before the
-- column existed stay NULL and are invalid (clean break, no install base).
ALTER TABLE app_public.MENDIX_APPS ADD COLUMN IF NOT EXISTS app_schema VARCHAR;
ALTER TABLE app_public.MENDIX_APPS ADD COLUMN IF NOT EXISTS license_id VARCHAR;
ALTER TABLE app_public.MENDIX_APPS ADD COLUMN IF NOT EXISTS user_roles VARIANT;
ALTER TABLE app_public.MENDIX_APPS ADD COLUMN IF NOT EXISTS role_mapping VARIANT;

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

-- Power-user CLI deploy path (PLAN section 5.5): large PADs exceed the SPCS ingress
-- upload limit on the browser->Streamlit hop, so consumers cannot push them through
-- the admin UI. Granting READ+WRITE on the deploy stage to app_admin lets a consumer
-- holding app_admin `snow stage copy` the PAD straight to apps/<name>/current.zip,
-- then trigger the deploy (Apps page "Redeploy" / POST /apps/<name>/trigger-deploy).
GRANT READ, WRITE ON STAGE app_public.MENDIX_DEPLOY_STAGE TO APPLICATION ROLE app_admin;

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

-- Internal-hop auth token. The admin UI calls the controller over the internal
-- service-to-service hop, where SPCS injects no caller token, so it vouches for the
-- operator's roles via the X-Operator-Roles header. The controller endpoint is
-- public, so that header-trust is gated on this shared secret that only the in-app
-- services carry (as the INTERNAL_AUTH_TOKEN env): a caller reaching the public
-- endpoint without a Snowflake caller token AND without this token gets no roles.
-- Generated once and kept stable across upgrades (guarded INSERT) so a re-run does
-- not rotate it out from under the running services.
CREATE TABLE IF NOT EXISTS app_public.internal_config (
    config_key   VARCHAR NOT NULL PRIMARY KEY,
    config_value VARCHAR NOT NULL
);
INSERT INTO app_public.internal_config (config_key, config_value)
    SELECT 'internal_auth_token', REPLACE(UUID_STRING(), '-', '')
    WHERE NOT EXISTS (
        SELECT 1 FROM app_public.internal_config WHERE config_key = 'internal_auth_token'
    );

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
        -- MAX_NODES must scale past the infra services: the controller + admin UI
        -- occupy node capacity, and every deployed Mendix app adds another service.
        -- With MAX_NODES = 1 no app service can ever schedule. One CPU_X64_XS node
        -- (1 vCPU / 6 GB) fits a medium-tier app (0.5 vCPU / 1 GB request), so scale
        -- by node count rather than node size.
        EXECUTE IMMEDIATE
            'CREATE COMPUTE POOL IF NOT EXISTS ' || pool ||
            ' MIN_NODES = 1 MAX_NODES = 5 INSTANCE_FAMILY = CPU_X64_XS' ||
            ' AUTO_RESUME = TRUE AUTO_SUSPEND_SECS = 3600';
        -- CREATE IF NOT EXISTS will not change MAX_NODES on a pool that already
        -- exists, so resize explicitly; this makes re-calling grant_callback the
        -- supported way to apply a new ceiling.
        EXECUTE IMMEDIATE 'ALTER COMPUTE POOL ' || pool || ' SET MAX_NODES = 5';
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
            -- The GENERIC_STRING short form (payload.type only) is sufficient: the
            -- install dry-run bound pg_secret and the app served with no extra keys.
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
-- 8. start_controller - creates the controller service.
--      - DB_SCHEMA env is the app's own <db>.APP_PUBLIC (resolved at runtime).
--      - IMAGE_REPO env is the fixed provider FQN of the mendix-base image.
--      - PG_EAI env is reference('pg_eai') so the controller's per-app CREATE
--        SERVICE emits EXTERNAL_ACCESS_INTEGRATIONS = (reference('pg_eai')).
--      - The bootstrap PG credential mounts via the bound pg_secret reference
--        (snowflakeSecret.objectReference, NOT reference() which is SQL-level
--        only); the controller reads host:port and password from that single
--        JSON secret at /secrets/pg.
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
    auth_tok  STRING;
BEGIN
    app_db    := CURRENT_DATABASE();
    db_schema := app_db || '.APP_PUBLIC';
    pool      := app_db || '_COMPUTE_POOL';
    wh        := app_db || '_WH';
    -- Shared internal-hop token (see section 3); injected as the controller's
    -- INTERNAL_AUTH_TOKEN env so it can authenticate the admin UI's role headers.
    SELECT config_value INTO :auth_tok FROM app_public.internal_config
        WHERE config_key = 'internal_auth_token';
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
      MENDIX_BASE_IMAGE: /<PROVIDER_DB>/<PROVIDER_SCHEMA>/<REPO>/mendix-base:latest
      DB_SCHEMA: ' || db_schema || '
      PG_EAI: reference(''pg_eai'')
      QUERY_WAREHOUSE: ' || wh || '
      CONTROLLER_SERVICE_NAME: MENDIX_DEPLOY_CONTROLLER
      ADMIN_UI_SERVICE_NAME: MENDIX_DEPLOY_ADMIN_UI
      PRIVILEGED_ROLES: ACCOUNTADMIN
      INTERNAL_AUTH_TOKEN: ' || auth_tok || '
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
    uid: 999
    gid: 999
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
-- 9. start_admin_ui - creates the admin UI service.
--    Both services are owned by the single app role, so no cross-role MONITOR
--    grant is needed. Endpoint access goes to app_admin (operators are granted
--    app_admin post-install).
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
    auth_tok  STRING;
BEGIN
    app_db    := CURRENT_DATABASE();
    db_schema := app_db || '.APP_PUBLIC';
    pool      := app_db || '_COMPUTE_POOL';
    wh        := app_db || '_WH';
    -- Same shared internal-hop token as the controller (section 3); the admin UI
    -- sends it as X-Internal-Auth so the controller trusts its operator-role headers.
    SELECT config_value INTO :auth_tok FROM app_public.internal_config
        WHERE config_key = 'internal_auth_token';
    dollar    := '$' || '$';   -- avoid a literal dollar-quote delimiter in this body
    -- Internal DNS: same-schema service reachable by its lowercased dashed name.
    -- Validated in the install dry-run (admin UI reaches the controller this way).
    -- Plain HTTP is intentional here: this call never leaves the app''s own compute
    -- pool (SPCS''s private pod network, not internet-routable), and it is
    -- authenticated via INTERNAL_AUTH_TOKEN below. Accepted risk, not an oversight -
    -- see the security best-practices review in PLAN-5b-external-listing.md.
    spec :=
'spec:
  containers:
  - name: streamlit
    image: /<PROVIDER_DB>/<PROVIDER_SCHEMA>/<REPO>/mendix-admin-ui:latest
    env:
      CONTROLLER_URL: http://mendix-deploy-controller:8080
      INTERNAL_AUTH_TOKEN: ' || auth_tok || '
      STREAMLIT_SERVER_MAX_UPLOAD_SIZE: "1024"
      DEPLOY_STAGE: "@' || db_schema || '.MENDIX_DEPLOY_STAGE"
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
