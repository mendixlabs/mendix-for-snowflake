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

- **pg_secret** - a secret holding your Snowflake-managed Postgres `host:port` and
  application password.
- **pg_eai** - an external access integration permitting egress to that Postgres
  instance.

## One-time consumer setup (outside the app)

These require ACCOUNTADMIN and cannot be performed by the app. The admin UI's
**Setup / Verify** page shows the exact SQL and checks each step:

1. Create the Snowflake-managed Postgres instance and its network policy.
2. Create the external access integration (+ network rule) for Postgres egress,
   then bind it as `pg_eai`.
3. Create the Postgres application user and grant `CREATEDB`.
4. Create the secret with the Postgres credentials, then bind it as `pg_secret`.

## After install

Grant the admin role to your operators:

```sql
GRANT APPLICATION ROLE <app_name>.app_admin TO ROLE <operators>;
```

Then open the admin UI (the app's default web endpoint) to register and deploy
Mendix apps.
