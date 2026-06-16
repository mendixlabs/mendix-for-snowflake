-- =============================================================
-- Mendix SPCS Deployment Controller — infrastructure reference
--
-- DO NOT run this file directly. Use Controller/setup.ps1, which
-- reads controller-config.json and executes the SQL with the
-- correct values substituted in.
--
-- This file documents the full schema and serves as a reference
-- for manual inspection or recreation from scratch.
--
-- Placeholders used below:
--   <DB_SCHEMA>   e.g. YOUR_DB.PUBLIC
--   <POOL>        e.g. MENDIX_POC_POOL
--   <WAREHOUSE>   e.g. COMPUTE_WH
--   <IMAGE_REPO>  e.g. your_db/public/poc_repo
--   <PG_EAI>      e.g. MENDIX_PG_EAI
-- =============================================================

-- -------------------------------------------------------------
-- 1. Controller role
-- -------------------------------------------------------------
CREATE ROLE IF NOT EXISTS MENDIX_DEPLOY_CONTROLLER_ROLE;
-- Hardening: replace ACCOUNTADMIN with a dedicated CI/CD service account role before production use.
GRANT ROLE MENDIX_DEPLOY_CONTROLLER_ROLE TO ROLE ACCOUNTADMIN;

-- Object-level access (required for the controller service's Snowflake session to function)
GRANT USAGE ON DATABASE  <DB>                                   TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON SCHEMA    <DB_SCHEMA>                            TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON WAREHOUSE <WAREHOUSE>                            TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON INTEGRATION <PG_EAI>                             TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

-- Service management
GRANT USAGE            ON COMPUTE POOL <POOL>                   TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT READ             ON IMAGE REPOSITORY <DB_SCHEMA>.POC_REPO TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE SERVICE   ON SCHEMA <DB_SCHEMA>                    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE SECRET    ON SCHEMA <DB_SCHEMA>                    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE STAGE     ON SCHEMA <DB_SCHEMA>                    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE TABLE     ON SCHEMA <DB_SCHEMA>                    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT                          TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

-- -------------------------------------------------------------
-- 2. Deploy stage (PAD zips land here, mounted into all app services)
-- -------------------------------------------------------------
CREATE STAGE IF NOT EXISTS <DB_SCHEMA>.MENDIX_DEPLOY_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

GRANT READ  ON STAGE <DB_SCHEMA>.MENDIX_DEPLOY_STAGE TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT WRITE ON STAGE <DB_SCHEMA>.MENDIX_DEPLOY_STAGE TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

-- -------------------------------------------------------------
-- 3. App registry
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS <DB_SCHEMA>.MENDIX_APPS (
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
    last_deployed_at   TIMESTAMP
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE <DB_SCHEMA>.MENDIX_APPS
    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

-- -------------------------------------------------------------
-- 4. Controller secrets (values supplied by setup.ps1 at runtime)
-- -------------------------------------------------------------
CREATE OR REPLACE SECRET <DB_SCHEMA>.CTRL_PG_HOST
    TYPE = GENERIC_STRING SECRET_STRING = '<pg-host>:<pg-port>';

CREATE OR REPLACE SECRET <DB_SCHEMA>.CTRL_PG_PASS
    TYPE = GENERIC_STRING SECRET_STRING = '<pg-application-password>';

GRANT READ ON SECRET <DB_SCHEMA>.CTRL_PG_HOST TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT READ ON SECRET <DB_SCHEMA>.CTRL_PG_PASS TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

-- -------------------------------------------------------------
-- 5. Controller service
-- -------------------------------------------------------------
CREATE SERVICE IF NOT EXISTS <DB_SCHEMA>.MENDIX_DEPLOY_CONTROLLER
    IN COMPUTE POOL <POOL>
    FROM SPECIFICATION $$
spec:
  containers:
  - name: controller
    image: /<IMAGE_REPO>/mendix-deploy-controller:latest
    env:
      COMPUTE_POOL: <POOL>
      IMAGE_REPO: <IMAGE_REPO>/mendix-base
      DB_SCHEMA: <DB_SCHEMA>
      PG_EAI: <PG_EAI>
      QUERY_WAREHOUSE: <WAREHOUSE>
    secrets:
    - snowflakeSecret: <DB_SCHEMA>.CTRL_PG_HOST
      directoryPath: /secrets/pg_host
    - snowflakeSecret: <DB_SCHEMA>.CTRL_PG_PASS
      directoryPath: /secrets/pg_pass
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
      name: "@<DB_SCHEMA>.MENDIX_DEPLOY_STAGE"
  endpoints:
  - name: controller-api
    port: 8080
    public: true
$$
    MIN_INSTANCES = 1
    MAX_INSTANCES = 1
    QUERY_WAREHOUSE = <WAREHOUSE>;

-- -------------------------------------------------------------
-- 6. (Optional) Restrict controller endpoint to specific roles
-- -------------------------------------------------------------
-- GRANT SERVICE ROLE <DB_SCHEMA>.MENDIX_DEPLOY_CONTROLLER!all_endpoints_usage
--     TO ROLE <ci_or_admin_role>;
