# Mendix on Snowpark Container Services

Deploy and manage Mendix applications on Snowpark Container Services from a single
admin UI. This app installs a deployment **controller** and an **admin UI**; from
there you register Mendix apps, upload their deployment archives (PAD), and the
controller brings each one up as its own container service.

## What you grant at install

- **CREATE COMPUTE POOL** - runs the controller, admin UI, and per-app services.
- **BIND SERVICE ENDPOINT** - exposes the admin UI and per-app web endpoints.
- **CREATE WAREHOUSE** - query warehouse for the services.

## What you bind at install

- **pg_secret** - a GENERIC_STRING secret whose value is JSON holding your
  Snowflake-managed Postgres `host:port` and application password:
  `{"host":"<host>:5432","password":"<application password>"}`.
- **pg_eai** - an external access integration permitting egress to that Postgres
  instance.

## One-time consumer setup (outside the app)

These are account-level objects the app cannot create for itself. The admin UI's
**Setup / Verify** page shows the exact SQL and checks each step:

1. Create the Snowflake-managed Postgres instance and its network policy.
2. Create the external access integration (+ network rule) for Postgres egress,
   then bind it as `pg_eai`.
3. Create the Postgres application user and grant `CREATEDB`.
4. Create the secret with the Postgres credentials, then bind it as `pg_secret`.

## Required Snowflake role

**ACCOUNTADMIN is not required.** Every privilege above, plus installing the app
itself, is an ordinary grantable Snowflake privilege. Have ACCOUNTADMIN (or
SECURITYADMIN) run this once to create a scoped installer role, then do the rest
of the install as that role:

```sql
-- Run once by ACCOUNTADMIN or SECURITYADMIN.
CREATE ROLE IF NOT EXISTS MENDIX_APP_INSTALLER
  COMMENT = 'Least-privilege role to install and configure the Mendix Native App - no ACCOUNTADMIN required';

-- Install the app from the listing
GRANT CREATE APPLICATION ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;
GRANT IMPORT SHARE       ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;   -- per Snowflake's listing-install docs; confirm on your first install

-- Passed on to the app via GRANT ... TO APPLICATION at install (WITH GRANT OPTION is
-- what lets this role hand a privilege to the application object)
GRANT CREATE COMPUTE POOL   ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER WITH GRANT OPTION;
GRANT CREATE WAREHOUSE      ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER WITH GRANT OPTION;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER WITH GRANT OPTION;  -- granted to PUBLIC by default since BCR-2321 (May 2026); harmless if redundant

-- One-time Postgres backing-store prerequisites (Setup / Verify page, steps 1-4)
GRANT CREATE POSTGRES INSTANCE           ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;  -- AWS/Azure only, not GCP
GRANT CREATE NETWORK POLICY              ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;
GRANT CREATE EXTERNAL ACCESS INTEGRATION ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;
GRANT CREATE COMPUTE POOL                ON ACCOUNT TO ROLE MENDIX_APP_INSTALLER;  -- to run the one-off PG_SETUP_JOB before the app exists

-- Schema-level, in whatever schema holds the prerequisite objects (secret, network
-- rules, the throwaway PG_SETUP_JOB service)
GRANT USAGE  ON DATABASE <db>          TO ROLE MENDIX_APP_INSTALLER;
GRANT USAGE  ON SCHEMA   <db>.<schema> TO ROLE MENDIX_APP_INSTALLER;
GRANT CREATE SECRET       ON SCHEMA <db>.<schema> TO ROLE MENDIX_APP_INSTALLER;
GRANT CREATE SERVICE      ON SCHEMA <db>.<schema> TO ROLE MENDIX_APP_INSTALLER;
GRANT CREATE NETWORK RULE ON SCHEMA <db>.<schema> TO ROLE MENDIX_APP_INSTALLER;

GRANT ROLE MENDIX_APP_INSTALLER TO ROLE <your operator/admin role>;
```

No `MANAGE APPLICATION SPECIFICATIONS` grant is needed: the role that runs
`CREATE APPLICATION` owns the app and can approve the `caller_token_spec` request
(see "After install" below) without an extra grant.

This role list is built from Snowflake's documented privilege model, not yet
validated end to end against a live install with this exact role - if a step
fails with an authorization error, that's the one to double-check first.

## After install

Approve the app's request for extended caller-token validity (required - without
it, `executeAsCaller` sessions fall back to the 120s default and operator-role
resolution fails with `OAUTH_ACCESS_TOKEN_EXPIRED`). Do this in Snowsight (app
security details, permissions tab), in the admin UI's **Setup / Verify** page, or:

```sql
SHOW SPECIFICATIONS IN APPLICATION <app_name>;
ALTER APPLICATION <app_name> APPROVE SPECIFICATION caller_token_spec SEQUENCE_NUMBER = <n>;
```

Then grant the admin role to your operators:

```sql
GRANT APPLICATION ROLE <app_name>.app_admin TO ROLE <operators>;
```

Then open the admin UI (the app's default web endpoint) to register and deploy
Mendix apps. Everything the admin UI does is also available on the controller's
REST API, so deploys can be scripted from CI/CD — find the endpoint with
`SHOW ENDPOINTS IN SERVICE <app_name>.app_public.MENDIX_DEPLOY_CONTROLLER;` and
authenticate with a Programmatic Access Token
(`Authorization: Snowflake Token="<PAT>"`).

## Granting the app access to your Snowflake data

The controller and per-app services run with **restricted caller's rights**
(`executeAsCaller`): a query against your Snowflake objects succeeds only when
**both** the operator running it **and** this application object hold the
privilege. So for each Mendix app, grant the application read access to the
databases, schemas, and objects that app queries, plus `USAGE` on its query
warehouse:

```sql
GRANT USAGE  ON DATABASE <data_db>                           TO APPLICATION <app_name>;
GRANT USAGE  ON SCHEMA   <data_db>.<data_schema>             TO APPLICATION <app_name>;
GRANT SELECT ON ALL TABLES IN SCHEMA <data_db>.<data_schema> TO APPLICATION <app_name>;
GRANT SELECT ON ALL VIEWS  IN SCHEMA <data_db>.<data_schema> TO APPLICATION <app_name>;
GRANT USAGE  ON WAREHOUSE <query_warehouse>                  TO APPLICATION <app_name>;
```

Without these grants the app reports: *the owning application `<app_name>` must
have at least one CALLER privilege granted on TABLE ...*. Snowflake does not
allow `FUTURE` grants to an application, so re-run the `ALL TABLES` / `ALL VIEWS`
grants after you add new objects the app must read.

**Grant before the app first connects.** A privilege granted after the app has
already opened its Snowflake session may not be picked up until the app refreshes
its connection, so run these grants as part of setup, before the app's first query.
