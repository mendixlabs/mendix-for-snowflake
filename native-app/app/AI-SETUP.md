# AI-assisted consumer setup

This runbook configures an installed Mendix on SPCS Native App. It is written
for Snowflake Cortex Code and other SQL-capable assistants.

The installed application exposes the executable version of this runbook as a
view. Replace `<app_name>` with the installed application name, then load it:

```sql
SELECT step_number, title, instructions, requires_approval, verify_sql
FROM <app_name>.app_public.ai_setup_steps
ORDER BY step_number;
```

## Agent rules

1. Show the full plan before changing the account. Ask for the application name,
   operator role, object database and schema, and preferred object-name prefix.
2. Run one row at a time. Replace every placeholder and show the resolved SQL
   before execution. Never execute SQL that still contains `<...>` placeholders.
3. Ask for confirmation before every row where `requires_approval` is true.
   Stop if an existing object has a different definition or owner.
4. Never request, print, store, or repeat a Postgres password in chat. For steps
   containing credentials, show the SQL with password placeholders and ask the
   user to run it in a private worksheet. Continue from their success report.
5. Run each row's `verify_sql` before continuing. Finish with a table showing
   PASS, MANUAL ACTION, SKIPPED, or FAILED for every step.

## Required workflow

1. Collect the application name, operator role, consumer-owned object database
   and schema, and an object-name prefix. Confirm Snowflake-managed Postgres is
   supported in the account's cloud and region.
2. Create the Postgres ingress network rule, network policy, and Postgres
   instance using the current SPCS egress CIDRs.
3. Create the host-port egress network rule and external access integration for
   the new Postgres instance.
4. Run the temporary `PG_SETUP_JOB` as `snowflake_admin` to grant `CREATEROLE`
   to the Postgres `application` user, verify `ALTER ROLE`, and remove the
   temporary service and secret.
5. Have the user privately create the permanent `pg_secret`. Do not accept its
   password through chat or an agent command.
6. Grant the application privileges, bind `pg_secret` with `READ`, bind `pg_eai`
   with `USAGE`, grant `app_admin` to the operator, and approve the pending
   `caller_token_spec`. Verify that both platform services reach `RUNNING`.

The installed view holds the version-specific commands and verification SQL.
This file holds the execution contract and required workflow. Update both in
the same change whenever setup behavior changes.

## Scope

Rows 1 through 6 are the minimum installation path. Data-access grants are a
separate per-Mendix-app task because their database, schema, warehouse, and
operator role vary. Optional outbound API access and email alerts remain on the
admin UI's **Setup / Verify** page.

Do not publish listings, alter provider objects, change unrelated network
policies, or remove existing account objects while following this runbook.

## Human fallback

The consumer README explains the privilege and security model. After the two
required references bind and the services start, the admin UI's **Setup /
Verify** page provides the same commands with editable names and manual checks.
