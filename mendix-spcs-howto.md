# Mendix on SPCS: Developer & Automation Reference

This document covers what the install-path docs do not: how to **build a Mendix app** for the
platform (SnowflakeSSO, JDBC, constants) and how to **automate the controller** through its
REST API. For everything else:

- **Publishing the app** (provider): [native-app/HOW-TO-PUBLISH.md](native-app/HOW-TO-PUBLISH.md)
- **Installing the app** (consumer): the listing page and [native-app/app/readme.md](native-app/app/readme.md)
- **Post-install setup** (Postgres, EAI, secrets, grants): the admin UI's **Setup / Verify** page
  shows the exact SQL and verifies each step

---

## Building a Mendix App for the Platform

### SnowflakeSSO module setup (Studio Pro)

1. Import `App Components/SnowflakeSSO.mpk` into your Studio Pro project
2. Copy `App Components/login.html` to `theme/web/` (replaces default login page with Snowflake SSO redirect)
3. Add `Snippet_TriggerSFTokenRefresh` to your Main Layout (keeps the caller token fresh)
4. Set `SnowflakeSSO.ASu_RegisterSnowflakeSSO` as the project's After Startup microflow
5. Map the `SnowflakeSSO.User` module role to all user roles that can log in, including every
   userrole named as a target in an [end-user role mapping](#end-user-role-mapping) - the module
   syncs a user's Mendix userroles from `MX_ROLE_MAPPING` at each login

The module reads the `Sf-Context-Current-User` header injected by SPCS, auto-logs users in
under their Snowflake identity, and captures the caller token for querying Snowflake data as
the end user.

### Querying Snowflake in a microflow

1. Retrieve `SnowflakeSSO.SnowflakeUser` for the current user
2. Call `GetCompoundToken` Java action → `$Token`
3. In `ExecuteQuery`: username override = `$SnowflakeUser/Name`, password override = `$Token`

The compound token (`<service-token>.<caller-token>`) authenticates via OAuth over the internal
Snowflake network. No EAI needed for this path. A query succeeds only when **both** the end user
and the application object hold the privilege (restricted caller's rights) — the data grants are
covered by [native-app/app/readme.md](native-app/app/readme.md) and Setup / Verify step 6.
Caller tokens expire after `SERVICE_CALLER_TOKEN_VALIDITY_SECS` (set account-wide at install,
typically 1800), so grants added to a live app can take up to ~30 minutes to be picked up.

### JDBC connection URL

The External Database Connector uses a JDBC URL. The Snowflake internal hostname is available as
`$SNOWFLAKE_HOST` (SPCS-injected env var). Use the `{SNOWFLAKE_HOST}` placeholder in the
constant — the base image entrypoint resolves it before the Mendix runtime starts:

```
jdbc:snowflake://{SNOWFLAKE_HOST}/?db=MY_DB&schema=MY_SCHEMA&warehouse=COMPUTE_WH&authenticator=oauth&JDBC_QUERY_RESULT_FORMAT=JSON
```

Set `DBUserName` and `DBPassword` constants to `PLACEHOLDER` — they are overridden per-query in
the microflow with the compound token.

---

## App Constants

Constants from Studio Pro are stored as Snowflake `GENERIC_STRING` secrets, one per constant,
in the app's own schema (`MXAPP_<APPNAME>`, which also holds the `PG_PASS`/`ADMIN_PASS`
secrets and the filestorage stage). The controller manages them; deleting the app drops the
schema and everything in it.

**Naming convention:** secret name = `MX_CONST_<MODULE>_<CONSTANTNAME>` (uppercase, dots
replaced by underscores). Example: `ExternalDatabaseConnector.LogNode` →
`MXAPP_MYAPP.MX_CONST_EXTERNALDATABASECONNECTOR_LOGNODE`.

**How they reach the container:** the base image `entrypoint.sh` reads
`etc/constants/variables.conf` from the extracted PAD to find the env var name for each
constant, then reads the corresponding secret from `/secrets/<secret_dir>/secret_string` and
exports it as an env var.

**Where values live:** only in the secrets. The app registry (`MENDIX_APPS.CONSTANTS`) stores
the constant names with every value masked as the reserved sentinel `<HIDDEN>`, and API
responses return the same masked form. Submitting `<HIDDEN>` back means "keep the existing
secret"; any other value replaces it.

**Updating a constant without a new PAD:** use the **Constants** editor on the Apps page (or
`PUT /apps/{name}/constants`, below). Saving updates the changed secrets, restarts the service,
and polls for RUNNING; a save where every value is still `<HIDDEN>` is a no-op.

If a newly deployed PAD introduces constants that have no default and no stored value, the
deploy returns 422 with the list of missing names; the admin UI prefills them into the
Constants editor.

---

## Licensing an app

Every app deploys trial-licensed: 6 concurrent users, unlimited named users, and the runtime
stops after a randomly chosen 2-4 hours (SPCS restarts it, losing sessions and in-memory
state). To run production-licensed, request a License ID + License Key from Mendix Support
(Developer Portal → app → "Request New App Node", hosting type Docker/other - no server ID
needed) and set them on the app.

**Where to set it:** the **License** section on the Apps page detail panel, the two optional
fields on the Register page, or `PUT`/`DELETE /apps/{name}/license` directly (below).

**What happens:** the License ID is not a credential - it is stored on the registry row and
passed to the container as a plain `RUNTIME_LICENSE_ID` env var, visible in the Service spec
expander like any other setting. The License Key *is* a credential and follows the constants
pattern: it is written straight to a per-app Snowflake secret
(`MXAPP_<APPNAME>.MX_LICENSE_KEY`) and mounted at `/secrets/mx_license_key`; it is write-only
from the UI/API side and never appears in a response, the registry row, or the activity log.
Saving or removing a license restarts the service, since the runtime only checks the license
once at startup.

**Validation is local and offline.** No egress is added or required - this is the documented
model for Docker, Kubernetes DIY, Cloud Foundry, and VM deployments, and container licenses
are not bound to a server ID, so restarts and redeploys are unproblematic.

**Removing a license** (`DELETE /apps/{name}/license`) reverts the app to trial after the next
restart.

---

## End-user role mapping

Maps Snowflake account roles to Mendix userroles, so end-users of a deployed app can be given
more than the static default userrole (`User`). Requires `use_caller_rights=true`: the SnowflakeSSO
module resolves an end-user's account roles via a compound-token session, which needs the caller
token that only an `executeAsCaller` service receives. The mapping stays configurable regardless;
the UI and API warn when it is inert.

**Where to set it:** the **End-user role mapping** section on the Apps page detail panel, or
`PUT`/`DELETE /apps/{name}/role-mapping` directly (below).

**Keys and values:** keys are Snowflake account role names, stored uppercase (Snowflake role
names are case-insensitive identifiers - quoted mixed-case roles are unsupported). Values are
Mendix userrole names, validated at deploy time against the PAD's `model/metadata.json`. Only
**account** roles are detectable (`CURRENT_AVAILABLE_ROLES()`); Snowflake **application** roles
cannot be mapped, since `IS_APPLICATION_ROLE_IN_SESSION` only works inside app-owned SQL context.

**What happens:** the mapping is not a secret. It is stored unmasked on the registry row and
delivered to the container as a plain `MX_ROLE_MAPPING` env var (compact JSON), visible in the
Service spec expander like any other setting. A user holding several mapped roles gets all of
the corresponding userroles; a user holding none of the mapped roles falls back to the default
userrole, with a log line - login is never denied by the mapping. Role sync happens once per
login, before the Mendix session is initialized, so a role change takes effect at the user's
*next* login, not mid-session. Saving or removing the mapping restarts the service.

**Removing the mapping** (`DELETE /apps/{name}/role-mapping`) reverts every login to the static
default userrole.

---

## Access Model

**Management plane** (who may deploy and manage an app): each app row has an `owner_role`. An
operator sees and manages an app only if its `owner_role` is one of their Snowflake roles —
reads of other apps return 404, mutations 403. Roles in `PRIVILEGED_ROLES` (set to
`ACCOUNTADMIN` in the app's service spec) bypass the check. A request with no resolvable roles
is denied (fail closed).

**Data plane** (which end users may open an app in a browser): app endpoints are gated by a
per-app **application role**. At registration the controller creates `app_<name>_user`, grants
the service's `ALL_ENDPOINTS_USAGE` service role to it, and also to `app_admin` (so operators
can always reach the app). Grant end users access per user or via an IdP-managed account role:

```sql
GRANT APPLICATION ROLE <app_name>.app_<name>_user TO USER <username>;
GRANT APPLICATION ROLE <app_name>.app_<name>_user TO ROLE <idp_group_role>;
```

This gate is independent of the app's caller's-rights setting (which governs what SQL the app
runs once inside, not ingress). On app deletion the controller drops the service and the
application role.

---

## Automating the Controller (REST API / CI-CD)

Everything the admin UI does goes through the controller's REST API, so deploys can be driven
from CI/CD directly.

**Endpoint discovery:**

```sql
SHOW ENDPOINTS IN SERVICE <app_name>.app_public.MENDIX_DEPLOY_CONTROLLER;
```

**Authentication:** the endpoint sits behind SPCS ingress, so the caller authenticates as a
Snowflake user — non-interactively with a Programmatic Access Token:

```
Authorization: Snowflake Token="<PAT>"
```

The PAT's user/role must hold the `app_admin` application role (endpoint access), and the
role determines what the API allows: apps whose `owner_role` the caller holds, or everything
for `PRIVILEGED_ROLES` members.

**API:**

| Method + path | Purpose |
|---|---|
| `GET /apps` | List apps visible to the caller (with service status) |
| `POST /apps` | Register an app (name, pg_database, admin_password, resource_tier, use_caller_rights, constants, owner_role, optional license_id + license_key) |
| `GET /apps/{name}` | App record + live service status; poll this after a deploy |
| `POST /apps/{name}/trigger-deploy` | Deploy the PAD staged under `apps/{name}/` (202, async) |
| `PUT /apps/{name}/constants` | Update constants (`<HIDDEN>` = keep existing; 202, restarts) |
| `PUT /apps/{name}/spec` | Change resource tier / caller's rights (202, restarts) |
| `PUT /apps/{name}/license` | Set the Mendix license (license_id + license_key; 202, restarts) |
| `DELETE /apps/{name}/license` | Remove the license, revert to trial (202, restarts) |
| `PUT /apps/{name}/role-mapping` | Set/replace the Snowflake role -> Mendix userrole mapping (202, restarts) |
| `DELETE /apps/{name}/role-mapping` | Clear the role mapping, revert to the default userrole (202, restarts) |
| `POST /apps/{name}/suspend` / `resume` | Suspend or resume the service (202) |
| `DELETE /apps/{name}` | Drop the service, access role, per-app secrets, and registry row |
| `GET /apps/{name}/logs` | App container logs |
| `GET /activity` | Audit log (scoped to the caller's apps) |
| `GET /system/logs/{target}`, `GET`/`PATCH /system/compute-pool` | Infrastructure (privileged roles only) |

**CI/CD deploy recipe** — mirror of what the admin UI does:

```powershell
# 1. Stage the PAD (directory destination with trailing slash - a destination ending
#    in a filename nests the file as <name>.zip/<name>.zip):
snow stage copy "MyApp_portable.zip" "@MENDIX_SPCS_APP.app_public.MENDIX_DEPLOY_STAGE/apps/my-app/"

# 2. Trigger the deploy (202; the controller prefers current.zip, else the newest .zip):
$headers = @{ Authorization = 'Snowflake Token="<PAT>"' }
Invoke-RestMethod -Method Post -Headers $headers `
  -Uri "https://<controller-ingress>/apps/my-app/trigger-deploy"

# 3. Poll until READY or FAILED:
do {
  Start-Sleep 10
  $app = Invoke-RestMethod -Headers $headers -Uri "https://<controller-ingress>/apps/my-app"
} while ($app.app.last_deploy_status -notin @("READY", "FAILED"))
```

Mutating calls return `202` and run in the background (SPCS ingress enforces a hard request
timeout, so nothing long-running is synchronous). Mendix apps take 3-5 minutes to fully
initialize after a deploy (PAD extraction, DB schema sync, model init) — a readiness probe
failing during that window is normal.

---

## Troubleshooting

| Problem | Cause / Fix |
|---------|-------------|
| `404 Not Found` on CLI login | Add region to account: `<LOCATOR>.<REGION>` (e.g., `xy12345.eu-west-2.aws`) |
| `snow spcs image-registry login` returns 401 | Connection PAT has a role restriction that limits access. Use a PAT without restriction for Docker operations, or check that `connections.toml` has valid credentials. |
| `push access denied` after successful login | Stale Docker credentials override the fresh token. Run `docker logout <registry>` first, then `snow spcs image-registry login`, then push — all in one session. |
| Service cycling PENDING → FAILED immediately | Check the app's logs on the admin UI Logs page. The most common cause is a missing or wrong secret value. |
| Deploy stuck in DEPLOYING / no READY or FAILED | Background task failed and could not update the registry. Check the controller's own logs (Logs page, privileged operators). |
| "PAD not found at /mnt/deploy-stage/..." | The stage path was given a filename destination and nested the zip. `LIST @<app>.app_public.MENDIX_DEPLOY_STAGE/apps/<name>/;` — if `current.zip` shows as a directory containing a file, `REMOVE` both and re-copy with a directory destination. |
| Service fails after "Extracting PAD..." | psql auth failure in the entrypoint: the Postgres password in the bound `pg_secret` is wrong. Fix the consumer secret — the controller caches it, so rebinding requires a reinstall of the app. |
| Service RUNNING but app won't load (readiness timeout) | Mendix PAD apps take 3-5 minutes to initialize. Wait before investigating. Check logs for Java stack traces if it's been >10 minutes. |
| MxAdmin login fails ("incorrect password") | `RUNTIME_ADMINUSER_PASSWORD` was not set. Supplied via the MxAdmin password field at registration. |
| "Object does not exist or not authorized" on Snowflake query | Data grant missing, or granted after the app connected (up to ~30 min token lag). See Setup / Verify step 6. |
| Readiness probe failing permanently on port 8090 | Admin port binds to `localhost` and is unreachable by SPCS. Use port 8080 with path `/` — that's what the controller-generated spec does. |
| New constant missing (422 on trigger-deploy) | PAD introduced a constant without a default and without a stored value. The error body lists the missing constant names. Set them via the Constants editor (or `PUT /apps/{name}/constants`) then retry. |

---

## Appendix: What `mendix-base` Contains

The base image (`Mendix Base Image/Dockerfile`) is Eclipse Temurin JRE 21 (a JDK is a Studio Pro dev-time concern, not needed to run a deployed app) plus `unzip` and `postgresql-client`, running as a non-root user. There is no Mendix app baked in. The `entrypoint.sh` at startup:

1. Reads `$PAD_STAGE_PATH` and extracts the PAD zip to `/mendix/pad/` (under the non-root user's writable WORKDIR, not filesystem root)
2. Reads file-based secrets from `/secrets/` and maps them to env vars:
   - `pg_pass` → `RUNTIME_PARAMS_DATABASEPASSWORD`
   - `admin_pass` → `M2EE_ADMIN_PASS` and `RUNTIME_ADMINUSER_PASSWORD`
   - `mx_const_<module>_<name>` → the env var from the PAD's `variables.conf`
3. Resolves `{SNOWFLAKE_HOST}` placeholder in any env var (for JDBC URLs)
4. Auto-creates the Postgres database if it does not exist (using `psql`)
5. Executes `/mendix/pad/bin/start /mendix/pad/etc/Default`

App constants follow the HOCON naming in `etc/constants/variables.conf`. The variable name is case-sensitive and has no extra underscores between words (e.g., `RUNTIME_PARAMS_DATABASETYPE`, not `RUNTIME_PARAMS_DATABASE_TYPE`).
