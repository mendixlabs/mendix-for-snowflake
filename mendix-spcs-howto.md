# How-To: Deploy Mendix on Snowpark Container Services (SPCS)

This guide covers the controller-based deployment model. The controller is a FastAPI service that manages Mendix app lifecycle on SPCS: it provisions services, stores app constants as Snowflake secrets, and handles new-version deploys without Docker rebuilds per app.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  SPCS: MENDIX_DEPLOY_CONTROLLER                                     │
│  FastAPI service managing all Mendix app deployments                │
│  Endpoint: public (PAT-authenticated)                               │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ creates / alters / suspends / resumes
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │ app-a service│  │ app-b service│  │ ...          │
   │ mendix-base  │  │ mendix-base  │  │              │
   │ image        │  │ image        │  │              │
   └──────────────┘  └──────────────┘  └──────────────┘
              │               │
              └───────────────┘
                      │
              Shared: compute pool, Postgres instance, EAI,
                      MENDIX_DEPLOY_STAGE (PAD zips)
```

**Key concepts:**
- One `mendix-base` Docker image handles all apps. Each app container reads its PAD zip from the Snowflake stage at startup.
- App constants are stored as Snowflake GENERIC_STRING secrets, mounted into the container via `directoryPath`. No PAD baked into an image.
- New version deploy = upload new PAD zip to stage + call the controller API. No Docker build.
- The controller itself (`mendix-deploy-controller`) is a separate image built once and updated only when controller code changes.

---

## Prerequisites

### Software

| Requirement | Notes |
|-------------|-------|
| Mendix Studio Pro 10.24.19+ or 11.6.5+ | PAD export required |
| Docker (Rancher Desktop or Docker Desktop) | Needed for one-time base image build |
| Rancher Desktop: use `dockerd (moby)` engine, not `containerd` | |
| Snowflake CLI (`snow`) 3.x+ | `pip install snowflake-cli-labs` |
| PowerShell 5.1+ | Ships with Windows 10/11 |

### Snowflake Account

- ACCOUNTADMIN access for initial setup
- Snowflake-managed Postgres instance (or bring your own)
- A **Programmatic Access Token (PAT)** for non-interactive CLI and Docker registry auth

### Snowflake CLI Connection

Edit `~/.snowflake/connections.toml`:

```toml
[mendix]
account = "<ACCOUNT_LOCATOR>.<REGION>"
user = "<SNOWFLAKE_USER>"
password = "<PAT-or-password>"
database = "<DATABASE>"
schema = "<SCHEMA>"
warehouse = "<WAREHOUSE>"
role = "ACCOUNTADMIN"
authenticator = "snowflake"
```

Notes:
- `account` must include the region: `xy12345.eu-west-2.aws` (convert from SQL format `AWS_EU_WEST_2`)
- Do not include `host` or `port`
- A PAT is recommended as the password for non-interactive use. Generate in Snowsight: User Menu > Preferences > Authentication > Programmatic access tokens. Leave role restriction empty for the connection PAT so it can authenticate for Docker registry pushes (which require ACCOUNTADMIN).
- Verify with: `snow sql -q "SELECT CURRENT_USER(), CURRENT_ROLE();" --connection mendix`

---

## One-Time Setup

### Step 1: Snowflake Infrastructure

Run as ACCOUNTADMIN:

```sql
-- Database and schema
CREATE DATABASE IF NOT EXISTS <DATABASE>;
CREATE SCHEMA IF NOT EXISTS <DATABASE>.<SCHEMA>;

-- Image repository
CREATE IMAGE REPOSITORY IF NOT EXISTS <DATABASE>.<SCHEMA>.<IMAGE_REPO>;

-- Compute pool
CREATE COMPUTE POOL IF NOT EXISTS MENDIX_POC_POOL
  MIN_NODES = 1
  MAX_NODES = 3
  INSTANCE_FAMILY = CPU_X64_S
  AUTO_RESUME = TRUE
  AUTO_SUSPEND_SECS = 3600;
```

### Step 2: Snowflake-Managed Postgres

The SPCS egress network rule keeps Postgres network-isolated (only reachable from container services).

```sql
-- 1. Get SPCS egress IP ranges
SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES();

-- 2. Ingress rule for Postgres (use IPs from above)
CREATE NETWORK RULE SPCS_TO_PG_INGRESS
  TYPE = IPV4
  VALUE_LIST = ('<cidr1>', '<cidr2>', ...)
  MODE = POSTGRES_INGRESS;

CREATE NETWORK POLICY MENDIX_PG_POLICY
  ALLOWED_NETWORK_RULE_LIST = (SPCS_TO_PG_INGRESS);

-- 3. Create the instance (save the output — credentials shown only once!)
CREATE POSTGRES INSTANCE MENDIX_PG
  COMPUTE_FAMILY = 'STANDARD_M'
  STORAGE_SIZE_GB = 10
  AUTHENTICATION_AUTHORITY = POSTGRES
  POSTGRES_VERSION = 17
  NETWORK_POLICY = 'MENDIX_PG_POLICY';

-- 4. Egress rule so SPCS containers can reach Postgres
CREATE NETWORK RULE MENDIX_PG_EGRESS
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('<pg-host>:5432');

CREATE EXTERNAL ACCESS INTEGRATION MENDIX_PG_EAI
  ALLOWED_NETWORK_RULES = (MENDIX_PG_EGRESS)
  ENABLED = TRUE;
```

**One-time: grant CREATEDB to the `application` Postgres user** (required for multi-app support — each app gets its own database, auto-created at startup):

After the mendix-base image is built and pushed (Step 3), run this job:

```sql
CREATE SERVICE <DATABASE>.<SCHEMA>.PG_SETUP_JOB
  IN COMPUTE POOL MENDIX_POC_POOL
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  EXTERNAL_ACCESS_INTEGRATIONS = (MENDIX_PG_EAI)
  FROM SPECIFICATION $$
spec:
  containers:
  - name: psql
    image: /<database>/<schema>/<image_repo>/mendix-base:latest
    command: ["bash", "-c", "PGPASSWORD='<application-password>' PGSSLMODE=require psql -h <pg-host> -p 5432 -U application -d postgres -c 'ALTER USER application CREATEDB;'"]
$$;

-- Wait ~30s, check the log:
CALL SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.PG_SETUP_JOB', '0', 'psql', 5);
-- Should show: ALTER ROLE

DROP SERVICE <DATABASE>.<SCHEMA>.PG_SETUP_JOB;
```

Notes:
- Postgres credentials (host, application password) are in the `CREATE POSTGRES INSTANCE` output. Save them.
- SPCS egress IPs have an expiry date. Monitor and update `SPCS_TO_PG_INGRESS` before they expire.
- The Postgres instance is NOT reachable from your local machine. Use an SPCS job for any psql admin work.

### Step 3: Build and Push the Mendix Base Image

This is a generic Mendix runner. Build it once; all apps share it.

```powershell
cd "Mendix Base Image"

# Login to Snowflake image registry
snow spcs image-registry login --connection mendix

$registry = "$(snow spcs image-registry url --connection mendix)"
$repo = "$registry/<database>/<schema>/<image_repo>"

docker build -t mendix-base .
docker tag mendix-base "$repo/mendix-base:latest"
docker push "$repo/mendix-base:latest"
```

Notes:
- The Docker connection uses `mendix` CLI credentials. If login fails with 401, ensure the `mendix` connection has a valid password/PAT without role restrictions in connections.toml. A PAT with `ROLE_RESTRICTION` set to a limited role will be rejected.
- Build happens locally. The image is ~500 MB (Eclipse Temurin JDK 21 base).

### Step 4: Set Up the Controller

**Configure `Controller/controller-config.json`** (gitignored — never commit this):

```json
{
  "snowConnection": "<snow-cli-connection-name>",
  "controllerPat": "<PAT-restricted-to-MENDIX_DEPLOY_CONTROLLER_ROLE>",
  "snowflake": {
    "database": "<SNOWFLAKE_DATABASE>",
    "schema": "<SNOWFLAKE_SCHEMA>",
    "computePool": "<COMPUTE_POOL_NAME>",
    "warehouse": "<WAREHOUSE_NAME>",
    "imageRepo": "<database>/<schema>/<image_repo>",
    "pgEai": "<EXTERNAL_ACCESS_INTEGRATION_NAME>"
  },
  "postgres": {
    "host": "<snowflake-managed-postgres-hostname>",
    "port": 5432,
    "username": "application",
    "password": "<postgres-application-password>"
  }
}
```

The `controllerPat` is used by `upload-pad.ps1` to authenticate against the controller's public endpoint. Generate it:

```sql
ALTER USER <you> ADD PROGRAMMATIC ACCESS TOKEN mendix_deploy_controller_pat
  ROLE_RESTRICTION = 'MENDIX_DEPLOY_CONTROLLER_ROLE'
  DAYS_TO_EXPIRY = 90;
```

Copy the token value from the output.

**Run the setup script:**

```powershell
.\Controller\setup.ps1 -Config .\Controller\controller-config.json
```

This creates:
- `MENDIX_DEPLOY_CONTROLLER_ROLE` (with the minimum grants needed)
- `MENDIX_DEPLOY_STAGE` (Snowflake stage for PAD zip uploads)
- `MENDIX_APPS` table (app registry, including each app's `owner_role`)
- `CTRL_PG_HOST` and `CTRL_PG_PASS` secrets (controller's Postgres credentials)
- `MENDIX_DEPLOY_CONTROLLER` service (with `executeAsCaller: true` and a caller-token validity, used to resolve PAT callers' roles for authorization)

**Build and push the controller image:**

```powershell
cd Controller

snow spcs image-registry login --connection mendix
$registry = "$(snow spcs image-registry url --connection mendix)"
$repo = "$registry/<database>/<schema>/<image_repo>"

docker build -t mendix-deploy-controller .
docker tag mendix-deploy-controller "$repo/mendix-deploy-controller:latest"
docker push "$repo/mendix-deploy-controller:latest"
```

Wait for the controller to reach RUNNING:

```powershell
& snow sql -q "SHOW SERVICES LIKE 'MENDIX_DEPLOY_CONTROLLER' IN SCHEMA <DATABASE>.<SCHEMA>;" `
  --connection mendix --format json | ConvertFrom-Json | Select-Object name, status
```

The controller endpoint URL is your deployment API base URL. Retrieve it:

```powershell
& snow sql -q "SHOW ENDPOINTS IN SERVICE <DATABASE>.<SCHEMA>.MENDIX_DEPLOY_CONTROLLER;" `
  --connection mendix --format json | ConvertFrom-Json | Select-Object name, ingress_url
```

---

## Deploying a New App

### Step 1: Prepare an App Config

Create a JSON file **outside the repo** (it contains credentials):

```json
{
  "pg_database":       "<app-database-name>",
  "admin_password":    "<mendix-admin-password>",
  "resource_tier":     "medium",
  "use_caller_rights": true,
  "owner_role":        "MENDIX_ADMIN_OPERATOR_ROLE",
  "constants": {
    "Module.ConstantName": "value",
    "ExternalDatabaseConnector.LogNode": "ExternalDatabaseConnector"
  }
}
```

`resource_tier` options: `"small"`, `"medium"`, `"large"` — maps to different CPU/memory limits.

`use_caller_rights: true` enables SPCS caller's rights (the container can query Snowflake as the logged-in user). See the Caller's Rights section below.

`owner_role` is optional. It sets which operator role sees and manages the app in the admin UI (see [Admin UI](#admin-ui-optional) and [Access model](#access-model-multi-tenant-isolation)). Omit it to default to `MENDIX_ADMIN_OPERATOR_ROLE`. The deploy PAT's role (`MENDIX_DEPLOY_CONTROLLER_ROLE`) is privileged, so `upload-pad.ps1` can register and deploy apps under any `owner_role`.

For constants that reference the Snowflake internal hostname (JDBC URLs), use the `{SNOWFLAKE_HOST}` placeholder — the base image entrypoint resolves it at container startup:

```json
"MyFirstModule.SnowflakeDB_DBSource": "jdbc:snowflake://{SNOWFLAKE_HOST}/?db=MY_DB&schema=MY_SCHEMA&warehouse=COMPUTE_WH&authenticator=oauth&JDBC_QUERY_RESULT_FORMAT=JSON"
```

### Step 2: Export a PAD from Studio Pro

App menu > Create Portable App Distribution > select the target.

The exported `.zip` file is the PAD.

### Step 3: Run `upload-pad.ps1`

```powershell
.\Controller\upload-pad.ps1 `
  -AppName "my-app" `
  -PadPath "C:\path\to\MyApp_portable_20261201_1200.zip" `
  -ControllerUrl "https://<controller-ingress>.snowflakecomputing.app" `
  -Token "<controllerPat from controller-config.json>" `
  -Config ".\Controller\controller-config.json" `
  -AppConfig "C:\path\to\my-app-controller-config.json"
```

`-AppConfig` is required on first deploy (registers the app). Omit it on subsequent deploys.

The script does four things:

1. **Registers the app** (`POST /apps`) — creates the SPCS service, filestorage stage, and secrets, and records the app's `owner_role`. Returns 409 if already registered (safe to retry).
2. **Uploads the PAD** to Snowflake stage via `snow stage copy` — bypasses the SPCS ingress timeout entirely.
3. **Triggers deploy** (`POST /apps/{name}/trigger-deploy`) — returns 202 immediately; the controller runs the deploy in a background thread.
4. **Polls** `GET /apps/{name}` every 10 seconds until `last_deploy_status` is `READY` or `FAILED`.

The deploy process (server-side, asynchronous):
- Parses the PAD zip for constant definitions
- Validates all constants have values (fails fast with 422 if not)
- Syncs secret values if constants changed
- Calls `ALTER SERVICE FROM SPECIFICATION` (if constants changed) or `SUSPEND` + `RESUME` (if not)
- Polls SPCS until the service reaches RUNNING
- Updates the registry with READY status and the endpoint URL

**Startup time:** Mendix apps take 3-5 minutes to fully initialize (PAD extraction, DB schema sync, model initialization). The readiness probe failing during this window is normal.

---

## Deploying a New Version

The SPCS service endpoint URL is preserved across all updates.

```powershell
.\Controller\upload-pad.ps1 `
  -AppName "my-app" `
  -PadPath "C:\path\to\MyApp_portable_20261215_0900.zip" `
  -ControllerUrl "https://<controller-ingress>.snowflakecomputing.app" `
  -Token "<controllerPat>" `
  -Config ".\Controller\controller-config.json"
```

No `-AppConfig` needed — the app is already registered.

If the new PAD introduces constants that weren't in the registry, the deploy returns 422 with a list of missing constants. Update the app's constants first:

```powershell
$headers = @{ Authorization = "Snowflake Token=`"<controllerPat>`"" }
$body = @{ constants = @{ "Module.NewConstant" = "value" } } | ConvertTo-Json
Invoke-RestMethod -Uri "https://<controller-url>/apps/my-app/constants" `
  -Method Put -Headers $headers -ContentType "application/json" -Body $body
```

Then re-run `upload-pad.ps1`.

---

## App Constants

Constants from Studio Pro are stored as Snowflake `GENERIC_STRING` secrets, one per constant. The controller manages them.

**Naming convention:** secret name = `MX_CONST_<MODULE>_<CONSTANTNAME>` (uppercase, dots replaced by underscores). Example: `ExternalDatabaseConnector.LogNode` → `MX_CONST_EXTERNALDATABASECONNECTOR_LOGNODE`.

**How they reach the container:** The base image `entrypoint.sh` reads `etc/constants/variables.conf` from the extracted PAD to find the env var name for each constant, then reads the corresponding secret from `/secrets/<secret_dir>/secret_string` and exports it as an env var.

**Updating a constant without a new PAD:**

```powershell
$headers = @{ Authorization = "Snowflake Token=`"<controllerPat>`"" }
$body = @{ constants = @{ "Module.ConstantName" = "new-value" } } | ConvertTo-Json
Invoke-RestMethod -Uri "https://<controller-url>/apps/my-app/constants" `
  -Method Put -Headers $headers -ContentType "application/json" -Body $body
```

This updates the secret in Snowflake, alters the service spec to restart the container, and polls for RUNNING.

---

## Caller's Rights (Querying Snowflake as the End User)

When `use_caller_rights: true`, SPCS injects a user identity token into every ingress request via the `Sf-Context-Current-User-Token` header. The SnowflakeSSO module captures this and enables querying Snowflake as the logged-in user.

### One-Time SQL Setup

Run as ACCOUNTADMIN after the service is running:

```sql
-- Extend caller token validity (default is 2 min; max 7 days)
ALTER SERVICE <DATABASE>.<SCHEMA>.<APP>_SERVICE
  SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800;
```

The controller runs this automatically when `use_caller_rights: true` and the service is first created.

**Grant the service permission to act on behalf of callers:**

```sql
-- For each Snowflake database/schema/warehouse the app needs to query:
GRANT CALLER USAGE ON DATABASE <target_db> TO ROLE <service_owner_role>;
GRANT INHERITED CALLER USAGE ON ALL SCHEMAS IN DATABASE <target_db> TO ROLE <service_owner_role>;
GRANT INHERITED CALLER SELECT ON ALL TABLES IN DATABASE <target_db> TO ROLE <service_owner_role>;
GRANT CALLER USAGE ON WAREHOUSE <warehouse> TO ROLE <service_owner_role>;

-- End users must have secondary roles active for cross-role access:
ALTER USER <username> SET DEFAULT_SECONDARY_ROLES = ('ALL');
```

Caller's rights uses two-layer permission checks: both the user AND the service owner role must have the privilege. If a user gets "Object does not exist or not authorized" for a table they can clearly see in Snowsight, the `GRANT INHERITED CALLER SELECT` is missing for that object.

### SnowflakeSSO Module Setup

1. Import `App Components/SnowflakeSSO.mpk` into your Studio Pro project
2. Copy `App Components/login.html` to `theme/web/` (replaces default login page with Snowflake SSO redirect)
3. Add `Snippet_TriggerSFTokenRefresh` to your Main Layout (keeps the caller token fresh)
4. Set `SnowflakeSSO.ASu_RegisterSnowflakeSSO` as the project's After Startup microflow
5. Map the `SnowflakeSSO.User` module role to all user roles that can log in

**Querying Snowflake in a microflow:**
1. Retrieve `SnowflakeSSO.SnowflakeUser` for the current user
2. Call `GetCompoundToken` Java action → `$Token`
3. In `ExecuteQuery`: username override = `$SnowflakeUser/Name`, password override = `$Token`

The compound token (`<service-token>.<caller-token>`) authenticates via OAuth over the internal Snowflake network. No EAI needed for this path.

### JDBC Connection URL

The External Database Connector uses a JDBC URL. The Snowflake internal hostname is available as `$SNOWFLAKE_HOST` (SPCS-injected env var). Use the `{SNOWFLAKE_HOST}` placeholder in the constant:

```
jdbc:snowflake://{SNOWFLAKE_HOST}/?db=MY_DB&schema=MY_SCHEMA&warehouse=COMPUTE_WH&authenticator=oauth&JDBC_QUERY_RESULT_FORMAT=JSON
```

The `entrypoint.sh` replaces `{SNOWFLAKE_HOST}` before the Mendix runtime starts. Set `DBUserName` and `DBPassword` constants to `PLACEHOLDER` — they are overridden per-query in the microflow with the compound token.

---

## Updating the Controller

When controller code changes (changes to `Controller/app/`), rebuild and push the controller image, then refresh the running service:

```powershell
cd Controller

snow spcs image-registry login --connection mendix
$registry = "$(snow spcs image-registry url --connection mendix)"
$repo = "$registry/<database>/<schema>/<image_repo>"

docker build -t mendix-deploy-controller .
docker tag mendix-deploy-controller "$repo/mendix-deploy-controller:latest"
docker push "$repo/mendix-deploy-controller:latest"

# Refresh the running service to pick up the new :latest:
.\update.ps1
```

`update.ps1` runs `ALTER SERVICE ... FROM SPECIFICATION`, which re-resolves the `:latest` tag and pins the new sha256 digest. The script polls until the service is RUNNING with the refreshed digest. The controller spec includes `executeAsCaller: true`, and `update.ps1` re-applies `SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800` each run (idempotent), so existing deployments pick up the caller-rights capability needed for role resolution on the next update. For a first-time upgrade to multi-tenant isolation, run the migration first (see [Access model: Upgrading an existing deployment](#upgrading-an-existing-deployment)).

> **Important:** `ALTER SERVICE SUSPEND` + `RESUME` does NOT re-pull `:latest`. SPCS pins the image to a sha256 digest in the resolved spec at CREATE / ALTER time and keeps that digest across SUSPEND/RESUME. Only `ALTER SERVICE FROM SPECIFICATION` re-resolves the tag.

Wait for `MENDIX_DEPLOY_CONTROLLER` to return to RUNNING before deploying any apps.

---

## Admin UI (Optional)

A Streamlit-based admin UI is available under `Admin UI/`. It runs as a sibling SPCS service in the same compute pool, calls the controller over the internal SPCS network, and lets operators manage apps from a browser instead of from PowerShell. PAD uploads still go through `upload-pad.ps1`; the admin UI handles status, redeploys from an existing stage path, constants edits, suspend/resume, logs, and delete.

The admin UI is multi-tenant: each app has an `owner_role` and an operator sees and manages only the apps owned by roles they hold. See [Access model](#access-model-multi-tenant-isolation) below.

### One-Time Setup

```powershell
cd "Admin UI"

# Copy the example config, fill in connection / db / schema / pool / warehouse / imageRepo:
Copy-Item admin-ui-config.example.json admin-ui-config.json
notepad admin-ui-config.json

# Build and push the image:
.\build-and-push.ps1

# Provision the service + operator role:
.\setup.ps1
```

`setup.ps1` prints the endpoint URL when it's ready and creates `MENDIX_ADMIN_OPERATOR_ROLE`. It also provisions the service with `executeAsCaller: true` and sets `SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800`, which the UI needs to resolve each operator's roles. Grant the operator role to humans who should access the UI:

```sql
GRANT ROLE MENDIX_ADMIN_OPERATOR_ROLE TO USER <username>;
```

Grant any additional team roles that own apps the same way; an operator's visible apps are determined by the roles they hold (see [Access model](#access-model-multi-tenant-isolation)).

Open the printed URL in a browser, authenticate with Snowflake credentials, and the apps owned by your roles appear in the Apps page. The Register page includes an **Owner role** selector populated from your roles.

### Internals

- The admin UI calls the controller at `http://mendix-deploy-controller:8080` over the internal SPCS DNS. No PAT is involved on this internal path; Snowflake enforces service-to-service access via the owner role of both services.
- The admin UI forwards the operator's Snowflake username as an `X-Operator` header (logged for audit) and the operator's resolved roles as an `X-Operator-Roles` header on every request. The controller uses the role header for authorization on this internal path.
- Roles are resolved once per browser session: the admin UI opens a caller's-rights session (compound token) and runs `SELECT CURRENT_AVAILABLE_ROLES()`. This is why the service runs with `executeAsCaller: true` and a caller-token validity. Mid-session role-grant changes are picked up on the next page refresh.
- `MIN_INSTANCES = MAX_INSTANCES = 1` because SPCS does not provide session affinity and Streamlit holds per-session state in process memory.
- The admin UI's image must be rebuilt and re-pinned when its code changes:

```powershell
cd "Admin UI"
.\build-and-push.ps1
.\update.ps1
```

  `update.ps1` runs `ALTER SERVICE ... FROM SPECIFICATION` so SPCS re-resolves `:latest`. See the warning under "Updating the Controller" for why SUSPEND/RESUME isn't enough.

### Access model (multi-tenant isolation)

Each app row in `MENDIX_APPS` has an `owner_role`. The controller authorizes every request against the caller's Snowflake roles:

- An operator may see and manage an app only if its `owner_role` is one of the caller's roles. Reads of other apps return 404 (existence is not leaked); mutations return 403.
- `MENDIX_DEPLOY_CONTROLLER_ROLE` is **privileged**: it bypasses the `owner_role` check and can act on any app. This is the role behind the deploy PAT, so `upload-pad.ps1` keeps working across all apps. Override the privileged set with the `PRIVILEGED_ROLES` env var on the controller (comma-separated) if needed.

The controller learns the caller's roles two ways, depending on the path:

- **Internal hop (admin UI):** trusts the `X-Operator-Roles` header, which the admin UI derived from Snowflake under the operator's identity.
- **Public endpoint (PAT clients like `upload-pad.ps1`):** SPCS injects a caller token, so the controller resolves the roles itself via `CURRENT_AVAILABLE_ROLES()` and ignores any `X-Operator-Roles` header. This is why the controller also runs with `executeAsCaller: true` and a caller-token validity.

A request with no resolvable roles is denied (fail closed).

#### Upgrading an existing deployment

Existing installs need the `owner_role` column added and backfilled before the new controller image enforces it:

1. Run the migration once: `snow sql -f Controller/sql/migration-owner-role.sql --connection <conn>` (adjust the schema in the file if not `YOUR_DB.PUBLIC`). Existing rows backfill to `MENDIX_ADMIN_OPERATOR_ROLE`, so they stay visible to that role.
2. Build/push the **admin UI** image, then `Admin UI\update.ps1`, then port-flip the admin UI (8501 → 8502 → 8501) to re-provision the WebSocket route after the `executeAsCaller` change.
3. Build/push the **controller** image, then `Controller\update.ps1`.

Deploy the admin UI before the controller: a new admin UI sending `X-Operator-Roles` to the old controller is harmless, whereas enforcing in the controller before the admin UI sends the header would lock operators out.

### Theming (Siemens iX)

The admin UI applies Siemens iX-aligned styling via `Admin UI/app/branding.py::apply_branding()`, called from every page after `st.set_page_config`. It injects the Titillium Web font (Google Fonts, an open stand-in for licensed Siemens Sans), the iX v5 Classic color tokens with light/dark handled through `@media (prefers-color-scheme)`, and a persistent Siemens logo via `st.logo()` (vendored at `Admin UI/app/assets/siemens-logo-white.svg`). The logo is white-only, so its container is given a dark backdrop in both light and dark mode. The theme's primary color is set with the `STREAMLIT_THEME_PRIMARY_COLOR` env var in the service spec; everything else is CSS-only (no `.streamlit/config.toml`). Details and the exact token values are in [`PLAN-2-siemens-ix-styling.md`](PLAN-2-siemens-ix-styling.md).

---

## Scheduled Suspend / Resume (Optional)

SPCS services with public endpoints do not auto-suspend. For POC environments:

```sql
-- Suspend at 6 PM UK weekdays
CREATE TASK <DATABASE>.<SCHEMA>.SUSPEND_CONTROLLER_EVENING
  SCHEDULE = 'USING CRON 0 18 * * 1-5 Europe/London'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS ALTER SERVICE <DATABASE>.<SCHEMA>.MENDIX_DEPLOY_CONTROLLER SUSPEND;

-- Resume at 8 AM UK weekdays
CREATE TASK <DATABASE>.<SCHEMA>.RESUME_CONTROLLER_MORNING
  SCHEDULE = 'USING CRON 0 8 * * 1-5 Europe/London'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS ALTER SERVICE <DATABASE>.<SCHEMA>.MENDIX_DEPLOY_CONTROLLER RESUME;

ALTER TASK <DATABASE>.<SCHEMA>.SUSPEND_CONTROLLER_EVENING RESUME;
ALTER TASK <DATABASE>.<SCHEMA>.RESUME_CONTROLLER_MORNING RESUME;
```

Repeat for each app service. The compute pool will idle-suspend when all services are suspended (`AUTO_SUSPEND_SECS = 3600`).

---

## Troubleshooting

| Problem | Cause / Fix |
|---------|-------------|
| `404 Not Found` on CLI login | Add region to account: `<LOCATOR>.<REGION>` (e.g., `xy12345.eu-west-2.aws`) |
| `snow spcs image-registry login` returns 401 | Connection PAT has a role restriction that limits access. Use a PAT without restriction for Docker operations, or check that `connections.toml` has valid credentials. |
| `push access denied` after successful login | Stale Docker credentials override the fresh token. Run `docker logout <registry>` first, then `snow spcs image-registry login`, then push — all in one session. |
| Service cycling PENDING → FAILED immediately | Check container logs: `snow sql -q "CALL SYSTEM\$GET_SERVICE_LOGS('<DB>.<SCHEMA>.<APP>_SERVICE', 0, 'mendix-app', 50);"` The most common cause is a missing or wrong secret value. |
| Deploy stuck in DEPLOYING / no READY or FAILED | Background task failed and could not update the registry. Check controller logs: `snow sql -q "CALL SYSTEM\$GET_SERVICE_LOGS('<DB>.<SCHEMA>.MENDIX_DEPLOY_CONTROLLER', 0, 'controller', 100);"` |
| "PAD not found at /mnt/deploy-stage/..." | Only happens if stage was manually manipulated. Check `LIST @MENDIX_DEPLOY_STAGE/apps/<name>/;` — if `current.zip` shows as a directory containing a file, remove both: `REMOVE @MENDIX_DEPLOY_STAGE/apps/<name>/current.zip/<filename>;`. Then run trigger-deploy again. |
| Service fails after "Extracting PAD..." | psql auth failure in entrypoint (wrong PG password in `<APP>_PG_PASS` secret). Fix: `ALTER SECRET <DB>.<SCHEMA>.<APP>_PG_PASS SET SECRET_STRING = '<actual-pg-password>';` then trigger-deploy again. |
| Service RUNNING but app won't load (readiness timeout) | Mendix PAD apps take 3-5 minutes to initialize. Wait before investigating. Check logs for Java stack traces if it's been >10 minutes. |
| MxAdmin login fails ("incorrect password") | `RUNTIME_ADMINUSER_PASSWORD` was not set. Supplied via the `admin_password` field in the app config JSON. |
| "Object does not exist or not authorized" on Snowflake query | Caller grant missing. Run `GRANT INHERITED CALLER SELECT ON ALL TABLES IN ...` for the target database. |
| Readiness probe failing permanently on port 8090 | Admin port binds to `localhost` and is unreachable by SPCS. Use port 8080 with path `/` — that's what the controller-generated spec does. |
| Controller API returns 401 | PAT expired or wrong. The `controllerPat` in `controller-config.json` has a 90-day default expiry. Generate a new one and update the config. |
| New constant missing (422 on trigger-deploy) | PAD introduced a constant without a default and without a stored value. The error body lists the missing constant names. Register the values via `PUT /apps/{name}/constants` then retry. |

---

## Appendix: What `mendix-base` Contains

The base image (`Mendix Base Image/Dockerfile`) is Eclipse Temurin JDK 21 plus `unzip` and `postgresql-client`. There is no Mendix app baked in. The `entrypoint.sh` at startup:

1. Reads `$PAD_STAGE_PATH` and extracts the PAD zip to `/mendix-pad/`
2. Reads file-based secrets from `/secrets/` and maps them to env vars:
   - `pg_pass` → `RUNTIME_PARAMS_DATABASEPASSWORD`
   - `admin_pass` → `M2EE_ADMIN_PASS` and `RUNTIME_ADMINUSER_PASSWORD`
   - `mx_const_<module>_<name>` → the env var from the PAD's `variables.conf`
3. Resolves `{SNOWFLAKE_HOST}` placeholder in any env var (for JDBC URLs)
4. Auto-creates the Postgres database if it does not exist (using `psql`)
5. Executes `/mendix-pad/bin/start /mendix-pad/etc/Default`

App constants follow the HOCON naming in `etc/constants/variables.conf`. The variable name is case-sensitive and has no extra underscores between words (e.g., `RUNTIME_PARAMS_DATABASETYPE`, not `RUNTIME_PARAMS_DATABASE_TYPE`).
