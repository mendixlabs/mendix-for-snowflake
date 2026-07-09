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
3. Create the Postgres application user and grant `CREATEDB` and `CREATEROLE`
   (the controller uses this account to provision a per-app role and database
   for each Mendix app you register).
4. Create the secret with the Postgres credentials, then bind it as `pg_secret`.

## Per-app Postgres isolation

Each Mendix app gets its own Postgres role and password, scoped to only that
app's own database. The controller creates both when you register the app.
Every app container connects as its own per-app role - never as the shared
`application` bootstrap credential above, which the controller holds and
never mounts into an app container.

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
privilege **directly**. So for each Mendix app, grant read access to the
databases, schemas, and objects that app queries to both the application and
the operator's active role, plus `USAGE` on its query warehouse:

```sql
GRANT USAGE  ON DATABASE <data_db>                           TO APPLICATION <app_name>;
GRANT USAGE  ON SCHEMA   <data_db>.<data_schema>             TO APPLICATION <app_name>;
GRANT SELECT ON ALL TABLES IN SCHEMA <data_db>.<data_schema> TO APPLICATION <app_name>;
GRANT SELECT ON ALL VIEWS  IN SCHEMA <data_db>.<data_schema> TO APPLICATION <app_name>;
GRANT USAGE  ON WAREHOUSE <query_warehouse>                  TO APPLICATION <app_name>;

-- The operator's active role needs the same grants directly. Role-hierarchy
-- inheritance does not satisfy the restricted caller's-rights check: an
-- ACCOUNTADMIN session that only inherits access through SYSADMIN (a common
-- setup) still fails here, even though the same session sees the data fine
-- outside the app.
GRANT USAGE  ON DATABASE <data_db>                           TO ROLE <operator_role>;
GRANT USAGE  ON SCHEMA   <data_db>.<data_schema>             TO ROLE <operator_role>;
GRANT SELECT ON ALL TABLES IN SCHEMA <data_db>.<data_schema> TO ROLE <operator_role>;
GRANT SELECT ON ALL VIEWS  IN SCHEMA <data_db>.<data_schema> TO ROLE <operator_role>;
GRANT USAGE  ON WAREHOUSE <query_warehouse>                  TO ROLE <operator_role>;
```

Without these grants the app reports: *the owning application `<app_name>` must
have at least one CALLER privilege granted on TABLE ...*. If the application
grants above are already in place and the error persists, check the operator's
role next: run `SHOW GRANTS TO ROLE <operator_role>` and confirm the privilege
appears directly, not only through an inherited role. Snowflake does not
allow `FUTURE` grants to an application, so re-run the `ALL TABLES` / `ALL VIEWS`
grants after you add new objects the app must read.

### The CALLER grant: a third grant type

That exact error text names a real, separate grant type. `GRANT CALLER
<privilege> ... TO APPLICATION` is distinct from both the regular `GRANT ...
TO APPLICATION` grant and the operator's direct role grant above; restricted
caller's rights requires all three together before a query succeeds. Apply
this grant in addition to the two above, not instead of them:

```sql
GRANT CALLER USAGE  ON DATABASE <data_db>                       TO APPLICATION <app_name>;
GRANT CALLER USAGE  ON SCHEMA   <data_db>.<data_schema>         TO APPLICATION <app_name>;
GRANT CALLER SELECT ON TABLE    <data_db>.<data_schema>.<table> TO APPLICATION <app_name>;
```

This form is per table; repeat the last line for every table and view the app
queries. Snowflake's `GRANT CALLER` reference also documents `GRANT INHERITED
CALLER SELECT ON ALL TABLES IN SCHEMA ... TO APPLICATION`, which would mirror
the `ALL TABLES IN SCHEMA` grants above. This project has only verified the
per-table form live; confirm the `INHERITED` form on your own account before
relying on it.

Verify the CALLER grant with `SHOW CALLER GRANTS ON TABLE
<data_db>.<data_schema>.<table>` or `SHOW CALLER GRANTS TO APPLICATION
<app_name>`. A plain `SHOW GRANTS ON TABLE ...` shows only the regular SELECT
grant; it says nothing about the CALLER grant, and the two are checked
separately.

**Neither grant type survives a reinstall.** Dropping and recreating the
application object, even under the identical app name, creates a distinct
application instance. Every `GRANT ... TO APPLICATION` and `GRANT CALLER ...
TO APPLICATION` statement is scoped to that instance and must run again after
every `DROP APPLICATION` + `CREATE APPLICATION`, which is easy to forget. The
operator's direct role grants are unaffected, since those are role-scoped
rather than application-instance-scoped.

**Grant before the app first connects.** A privilege granted after the app has
already opened its Snowflake session may not be picked up until the app refreshes
its connection, so run these grants as part of setup, before the app's first query.
