# How-To: Deploy Mendix on Snowpark Container Services (SPCS)

## Prerequisites

### Software

| Requirement | Version / Notes | Install |
|-------------|----------------|---------|
| Mendix Studio Pro | 10.24.19+ or 11.6.5+ (PAD support required) | [mendix.com](https://www.mendix.com/studio-pro/) |
| WSL 2 | Required on Windows for Docker to build `linux/amd64` images | `wsl --install` in an elevated terminal, then reboot |
| Docker engine | Either **Rancher Desktop** (set engine to `dockerd`, not `containerd`) or **Docker Desktop** | [Rancher Desktop](https://rancherdesktop.io/) / [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| Snowflake CLI (`snow`) | 3.x+ (provides `snow sql`, registry auth) | `pip install snowflake-cli-labs` or see [docs](https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation) |
| PowerShell | 5.1+ (ships with Windows 10/11; used by `deploy.ps1`) | Built-in |

### Snowflake Account

- A Snowflake account with **ACCOUNTADMIN** role access (compute pools, image repos, services, secrets, and network rules all require elevated privileges).
- A **Programmatic Access Token (PAT)** for non-interactive CLI and Docker registry authentication. Generate one in Snowsight: User Menu > Preferences > Authentication > Programmatic access tokens.

### Network Access

Your workstation must be able to reach:

- `<org>-<account>.registry.snowflakecomputing.com` (Docker image push/pull)
- `<account_locator>.<region>.snowflakecomputing.com` (Snowflake CLI)

If you're behind a corporate proxy or firewall, ensure these hostnames are allowed on ports 443 (HTTPS) and the Docker registry port.

### Rancher Desktop Configuration (if using Rancher)

1. Open Rancher Desktop > Preferences > Container Engine.
2. Select **dockerd (moby)** as the engine. The `containerd` engine does not expose the Docker CLI socket that `docker build`/`docker push` require.
3. Restart Rancher Desktop after changing the engine.

## Account Details Reference

Before starting, gather your account details. You'll need them throughout this guide:

| Field | How to Find | Format |
|-------|-------------|--------|
| Organization | `SELECT CURRENT_ORGANIZATION_NAME()` | e.g., `MYORG123` |
| Account Name | `SELECT CURRENT_ACCOUNT_NAME()` | e.g., `MY_ACCOUNT` |
| Account Locator | `SELECT CURRENT_ACCOUNT()` | e.g., `xy12345` |
| Region | `SELECT CURRENT_REGION()` | e.g., `AWS_EU_WEST_2` |
| Registry URL | `SHOW IMAGE REPOSITORIES` → `repository_url` column | `<org>-<account>.registry.snowflakecomputing.com` |

> **Important:** The registry URL uses **hyphens** between org and account name (e.g., `myorg123-my-account`), even if your account name uses underscores (`MY_ACCOUNT`). Docker treats these as different hosts — always use the exact URL from `SHOW IMAGE REPOSITORIES`.

**Placeholders used in this guide:**

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `<ACCOUNT_LOCATOR>` | Your account locator | `xy12345` |
| `<REGION>` | Cloud region in CLI format (dots, not underscores) | `eu-west-2.aws` |
| `<REGISTRY_HOST>` | Full registry hostname from `SHOW IMAGE REPOSITORIES` | `myorg-myaccount.registry.snowflakecomputing.com` |
| `<DATABASE>` | Your target database name | `MY_DATABASE` |
| `<SCHEMA>` | Your target schema | `PUBLIC` |
| `<IMAGE_REPO>` | Image repository name | `POC_REPO` |
| `<SNOWFLAKE_USER>` | Your Snowflake username | `JANE_DOE` |
| `<WAREHOUSE>` | Your warehouse name | `COMPUTE_WH` |

---

## Step 1: Configure Snowflake CLI Connection

Edit `~/.snowflake/connections.toml` (note: this is `connections.toml`, NOT `config.toml`):

```toml
[mendix]
account = "<ACCOUNT_LOCATOR>.<REGION>"
user = "<SNOWFLAKE_USER>"
password = "<your-PAT-token>"
database = "<DATABASE>"
schema = "<SCHEMA>"
warehouse = "<WAREHOUSE>"
role = "ACCOUNTADMIN"
authenticator = "snowflake"
```

**Key lessons:**
- In `connections.toml`, connection names are top-level sections (no `connections.` prefix). The `connections.` prefix is only used in `config.toml`.
- The `account` field must include the region: `<ACCOUNT_LOCATOR>.<REGION>` (e.g., `xy12345.eu-west-2.aws`). Without it you get `404 Not Found` on login. Convert the region from SQL format (`AWS_EU_WEST_2`) to CLI format (`eu-west-2.aws`).
- Do NOT include `host` or `port` fields — the CLI resolves them from the account identifier.
- Use a **Programmatic Access Token (PAT)** as the password to bypass MFA/Duo for non-interactive use. Generate one in Snowsight: User Menu → Preferences → Authentication → Programmatic access tokens.
- Use `ACCOUNTADMIN` role for POC work (creating compute pools, services, secrets, image repos all require elevated privileges).

---

## Step 2: Build the Mendix PAD Container Image

1. In Studio Pro, create a Portable App Distribution package: App → Create Deployment Package → Portable package
2. Extract the ZIP to a working directory (e.g., `C:\Projects\mendix-spcs-poc\`)
3. Create a `Dockerfile`:

```dockerfile
FROM eclipse-temurin:21-jdk
WORKDIR /mendix
COPY ./app ./app
COPY ./bin ./bin
COPY ./etc ./etc
COPY ./lib ./lib
ENV MX_LOG_LEVEL=info
EXPOSE 8080 8090
CMD ["./bin/start", "etc/Default"]
```

4. Build for linux/amd64:

```powershell
cd C:\Projects\mendix-spcs-poc
docker build --platform linux/amd64 -t mendix-app:poc .
```

---

## Step 3: Create Snowflake Infrastructure

Run in Snowsight or via CLI:

### Step 3a: Create Snowflake Postgres Instance (Recommended)

This provides a persistent, managed database. No sidecar container needed.

```sql
USE ROLE ACCOUNTADMIN;

-- Account-level network policy (required for Docker registry access)
-- Without this, docker push/pull to the Snowflake registry fails with "Network policy is required"
-- Using 0.0.0.0/0 to avoid lockouts from dynamic IPs. Do NOT use a single /32 IP.
CREATE NETWORK POLICY IF NOT EXISTS ALLOW_ALL_POLICY
  ALLOWED_IP_LIST = ('0.0.0.0/0');
ALTER ACCOUNT SET NETWORK_POLICY = ALLOW_ALL_POLICY;

-- 1. Get SPCS egress IPs (needed for whitelisting)
SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES();
-- Note the ipv4_prefix values

-- 2. Create ingress rule allowing SPCS to reach Postgres
CREATE NETWORK RULE SPCS_TO_PG_INGRESS
  TYPE = IPV4
  VALUE_LIST = ('<cidr1>', '<cidr2>')  -- from step 1
  MODE = POSTGRES_INGRESS;

CREATE NETWORK POLICY MENDIX_PG_POLICY
  ALLOWED_NETWORK_RULE_LIST = (SPCS_TO_PG_INGRESS);

-- 3. Create the Postgres instance
CREATE POSTGRES INSTANCE MENDIX_PG
  COMPUTE_FAMILY = 'STANDARD_M'  -- smallest available varies by region
  STORAGE_SIZE_GB = 10
  AUTHENTICATION_AUTHORITY = POSTGRES
  POSTGRES_VERSION = 17
  NETWORK_POLICY = 'MENDIX_PG_POLICY';

-- IMPORTANT: Save the credentials shown! They cannot be retrieved again.
-- Note the 'host' and 'access_roles' from the output.

-- 4. Wait for instance to become READY
DESCRIBE POSTGRES INSTANCE MENDIX_PG
  ->> SELECT "property", "value" FROM $1 WHERE "property" IN ('state', 'host');

-- 5. Create EAI so SPCS can reach the Postgres host
CREATE NETWORK RULE MENDIX_PG_EGRESS
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('<host_from_step_4>:5432');

CREATE EXTERNAL ACCESS INTEGRATION MENDIX_PG_EAI
  ALLOWED_NETWORK_RULES = (MENDIX_PG_EGRESS)
  ENABLED = TRUE;
```

**Key lessons:**
- SPCS egress IPs are found via `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()`. These have an expiry date; monitor and update the network rule before they expire.
- The Postgres instance credentials are shown only at creation time. If lost, reset with `ALTER POSTGRES INSTANCE ... RESET ACCESS FOR 'application'`.
- Mendix uses the `application` role (not `snowflake_admin`) for app-level access.
- The default database is `postgres`; Mendix auto-creates its schema there.
- `RUNTIME_PARAMS_DATABASEUSESSL: "true"` is required (Snowflake Postgres mandates TLS).
- No Business Critical edition needed; this uses IP whitelisting over the public endpoint.

### Step 3b: Create Base Infrastructure

```sql
USE ROLE ACCOUNTADMIN;

-- Database
CREATE DATABASE IF NOT EXISTS <DATABASE>;
CREATE SCHEMA IF NOT EXISTS <DATABASE>.<SCHEMA>;

-- Image repository, stages
CREATE IMAGE REPOSITORY IF NOT EXISTS <DATABASE>.<SCHEMA>.<IMAGE_REPO>;
CREATE STAGE IF NOT EXISTS <DATABASE>.<SCHEMA>.SPECS ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');
CREATE STAGE IF NOT EXISTS <DATABASE>.<SCHEMA>.MENDIX_FILESTORAGE_STAGE
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Secrets
CREATE SECRET IF NOT EXISTS <DATABASE>.<SCHEMA>.MENDIX_DB_CREDENTIALS
  TYPE = PASSWORD
  USERNAME = 'mendix'
  PASSWORD = '<your-db-password>';

CREATE SECRET IF NOT EXISTS <DATABASE>.<SCHEMA>.MENDIX_ADMIN_PASSWORD
  TYPE = GENERIC_STRING
  SECRET_STRING = '<your-mendix-admin-password>';

-- Optional: License secret (skip for trial mode)
-- CREATE SECRET <DATABASE>.<SCHEMA>.MENDIX_LICENSE
--   TYPE = PASSWORD
--   USERNAME = '<LICENSE_ID>'
--   PASSWORD = '<LICENSE_KEY>';

-- Compute pool
CREATE COMPUTE POOL IF NOT EXISTS MENDIX_POC_POOL
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_S
  AUTO_RESUME = TRUE
  AUTO_SUSPEND_SECS = 3600;
```

---

## Step 4: Push Container Images

**Login to Snowflake registry:**

```powershell
docker login <REGISTRY_HOST> -u <SNOWFLAKE_USER>
# Enter your Snowflake password or PAT when prompted
```

**Tag and push Mendix app:**

```powershell
$registry = "<REGISTRY_HOST>/<database>/<schema>/<image_repo>"
docker tag mendix-app:poc "$registry/mendix-app:poc"
docker push "$registry/mendix-app:poc"
```

> Note: The registry path uses **lowercase** database/schema/repo names (e.g., `my_database/public/poc_repo`).

**Pull, tag and push PostgreSQL:**

```powershell
docker pull --platform linux/amd64 postgres:16
docker tag postgres:16 "$registry/postgres:16"
docker push "$registry/postgres:16"
```

**Verify images landed:**
```sql
SELECT SYSTEM$REGISTRY_LIST_IMAGES('/<DATABASE>/<SCHEMA>/<IMAGE_REPO>');
```

**Key lessons:**
- If you get `Authorization Failure`, make sure you're logged in with the correct hostname (hyphens, not underscores) AND the correct role. The Docker token is tied to the role used at login.
- If using a non-owner role, grant access: `GRANT READ, WRITE ON IMAGE REPOSITORY <DATABASE>.<SCHEMA>.<IMAGE_REPO> TO ROLE <role>;`
- After changing roles, you must `docker logout` and `docker login` again to refresh the token.
- In PowerShell, the `@` symbol in stage paths must be quoted (e.g., `'@<DATABASE>.<SCHEMA>.SPECS/'`) to avoid splatting errors.

---

## Step 5: Deploy the Service

The service can be created with an inline spec. Below shows the **recommended** setup using Snowflake Postgres (no sidecar):

```sql
USE ROLE ACCOUNTADMIN;
USE DATABASE <DATABASE>;

CREATE SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE
  IN COMPUTE POOL MENDIX_POC_POOL
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  EXTERNAL_ACCESS_INTEGRATIONS = (MENDIX_PG_EAI)
  FROM SPECIFICATION $$
spec:
  containers:
    - name: mendix-app
      image: /<database>/<schema>/<image_repo>/mendix-app:latest
      env:
        RUNTIME_PARAMS_DATABASETYPE: "POSTGRESQL"
        RUNTIME_PARAMS_DATABASEHOST: "<pg_host>:5432"
        RUNTIME_PARAMS_DATABASENAME: "postgres"
        RUNTIME_PARAMS_DATABASEUSERNAME: "application"
        RUNTIME_PARAMS_DATABASEPASSWORD: "<application_password>"
        RUNTIME_PARAMS_DATABASEUSESSL: "true"
        RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE: "com.mendix.storage.localfilesystem"
        RUNTIME_PARAMS_UPLOADEDFILESPATH: "/mnt/filestorage"
        M2EE_ADMIN_PASS: "<your-admin-password>"
        RUNTIME_ADMINUSER_PASSWORD: "<your-admin-password>"
      readinessProbe:
        port: 8080
        path: /
      volumeMounts:
        - name: filestorage
          mountPath: /mnt/filestorage
      resources:
        requests:
          memory: 2G
          cpu: 1
        limits:
          memory: 4G
          cpu: 2
  endpoints:
    - name: mendix-web
      port: 8080
      public: true
  volumes:
    - name: filestorage
      source: stage
      stageConfig:
        name: "@<DATABASE>.<SCHEMA>.MENDIX_FILESTORAGE_STAGE"
  logExporters:
    eventTableConfig:
      logLevel: INFO
$$;
```

<details>
<summary>Alternative: Sidecar PostgreSQL (for quick throwaway testing without Snowflake Postgres)</summary>

This runs a PostgreSQL container alongside Mendix. Data is ephemeral (lost on DROP SERVICE).

```sql
USE ROLE ACCOUNTADMIN;
USE DATABASE <DATABASE>;

CREATE SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE
  IN COMPUTE POOL MENDIX_POC_POOL
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  FROM SPECIFICATION $$
spec:
  containers:
    - name: mendix-app
      image: /<database>/<schema>/<image_repo>/mendix-app:poc
      env:
        RUNTIME_PARAMS_DATABASETYPE: "POSTGRESQL"
        RUNTIME_PARAMS_DATABASEHOST: "localhost:5432"
        RUNTIME_PARAMS_DATABASENAME: "mendix"
        RUNTIME_PARAMS_DATABASEUSERNAME: "mendix"
        RUNTIME_PARAMS_DATABASEPASSWORD: "<your-db-password>"
        RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE: "com.mendix.storage.localfilesystem"
        RUNTIME_PARAMS_UPLOADEDFILESPATH: "/mnt/filestorage"
        M2EE_ADMIN_PASS: "<your-admin-password>"
        RUNTIME_ADMINUSER_PASSWORD: "<your-admin-password>"
      readinessProbe:
        port: 8080
        path: /
      volumeMounts:
        - name: filestorage
          mountPath: /mnt/filestorage
      resources:
        requests:
          memory: 2G
          cpu: 1
        limits:
          memory: 4G
          cpu: 2
    - name: postgres
      image: /<database>/<schema>/<image_repo>/postgres:16
      env:
        POSTGRES_DB: "mendix"
        POSTGRES_USER: "mendix"
        POSTGRES_PASSWORD: "<your-db-password>"
      volumeMounts:
        - name: pgdata
          mountPath: /var/lib/postgresql/data
      resources:
        requests:
          memory: 512M
          cpu: 0.5
        limits:
          memory: 1G
          cpu: 1
  endpoints:
    - name: mendix-web
      port: 8080
      public: true
  volumes:
    - name: filestorage
      source: stage
      stageConfig:
        name: "@<DATABASE>.<SCHEMA>.MENDIX_FILESTORAGE_STAGE"
    - name: pgdata
      source: local
  logExporters:
    eventTableConfig:
      logLevel: INFO
$$;
```

</details>

> Note: Image paths inside the spec use **lowercase** (e.g., `/<database>/<schema>/<image_repo>/mendix-app:poc`).

**Key lessons about the service spec:**
- Stage volume names must be **fully qualified** and start with `@`: use `"@DATABASE.SCHEMA.MENDIX_FILESTORAGE_STAGE"`, not just `"@mendix_filestorage_stage"`. Without full qualification, SPCS cannot resolve the stage.
- Mendix PAD readiness probe is at `/probes/ready` on port **8090** (admin port), NOT `/health/ready` on 8080. The app also exposes `/probes/alive` and `/probes/started` on the same admin port.
- **Readiness probe strategy:** The admin port (8090) binds to `localhost` only and is NOT configurable via environment variables in PAD. Instead, use the **app port (8080)** with path `/` for the readiness probe. The app port binds to `0.0.0.0` and is reachable by SPCS.
- **Startup time:** Mendix PAD apps take 2-3 minutes to fully initialize (model sync, license check, task queues, DB schema setup). The readiness probe will fail during this time; this is expected. Don't assume a startup error until you've waited at least 3-4 minutes and checked the logs.
- You must have an active database context (`USE DATABASE`) when creating a service, even if using fully qualified names.
- Inline spec (`FROM SPECIFICATION $$...$$`) avoids needing to upload YAML to a stage; useful when stage uploads are blocked by network policies.

**Critical: PAD environment variable naming**

The Mendix PAD config system uses HOCON (in `etc/variables.conf`). Environment variables map to runtime settings via specific, **case-sensitive** names with **no separating underscores** between words. Getting these wrong causes silent fallback to defaults (e.g., HSQLDB instead of PostgreSQL).

| Runtime Setting | Correct Env Var | WRONG (will be silently ignored) |
|-----------------|-----------------|----------------------------------|
| `DatabaseType` | `RUNTIME_PARAMS_DATABASETYPE` | `RUNTIME_PARAMS_DATABASE_TYPE` |
| `DatabaseHost` | `RUNTIME_PARAMS_DATABASEHOST` | `RUNTIME_PARAMS_DATABASE_HOST` |
| `DatabaseName` | `RUNTIME_PARAMS_DATABASENAME` | `RUNTIME_PARAMS_DATABASE_NAME` |
| `DatabaseUserName` | `RUNTIME_PARAMS_DATABASEUSERNAME` | `RUNTIME_PARAMS_DATABASE_USERNAME` |
| `DatabasePassword` | `RUNTIME_PARAMS_DATABASEPASSWORD` | `RUNTIME_PARAMS_DATABASE_PASSWORD` |
| `DatabaseUseSsl` | `RUNTIME_PARAMS_DATABASEUSESSL` | n/a (required for Snowflake Postgres, set to `"true"`) |
| `com.mendix.core.StorageService` | `RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE` | `MXRUNTIME_com_mendix_core_StorageService` |
| `UploadedFilesPath` | `RUNTIME_PARAMS_UPLOADEDFILESPATH` | `MXRUNTIME_com_mendix_storage_localfilesystem_Location` |

**Admin user password vs. admin API password (two separate things!):**

| Env Var | Purpose | What it does |
|---------|---------|--------------|
| `M2EE_ADMIN_PASS` | M2EE admin API password | Authenticates requests to the management interface on port 8090 (probes, commands). Required for the PAD startup script to communicate with the runtime. |
| `RUNTIME_ADMINUSER_PASSWORD` | MxAdmin application login | Creates/updates the MxAdmin user in the database on startup. This is what you type into the Mendix login page. Without it, the MxAdmin user does not exist and you cannot log in. |

Both must be set. They can be the same value for POC work, but they serve different purposes.

**Why this happens:** The PAD config file (`etc/variables.conf`) defines HOCON substitutions like `"DatabaseType" = ${?RUNTIME_PARAMS_DATABASETYPE}`. The env var name is derived by uppercasing the HOCON path: `runtime.params.DatabaseType` becomes `RUNTIME_PARAMS_DATABASETYPE`. Underscores in the original setting name (like in `DatabaseType`) are preserved as-is; no extra underscore is inserted. The old docker-buildpack used `MXRUNTIME_` prefixed vars with dot-separated names; PAD does not use that format.

**How to find the correct env var for any setting:** Open `etc/variables.conf` in your PAD package and search for the setting name. The `${?ENV_VAR_NAME}` syntax shows the exact env var expected.

---

## Step 6: Validate and Monitor

**Check service status:**
```sql
SELECT SYSTEM$GET_SERVICE_STATUS('<DATABASE>.<SCHEMA>.MENDIX_SERVICE');
```

**View container logs:**
```sql
CALL SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.MENDIX_SERVICE', '0', 'mendix-app', 200);
CALL SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.MENDIX_SERVICE', '0', 'postgres', 100);
```

**Get the public endpoint URL:**
```sql
SHOW ENDPOINTS IN SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE;
```

**Expected startup behavior:**
- Compute pool takes ~2-3 minutes to go from IDLE → ACTIVE on first use
- PostgreSQL container starts within seconds
- Mendix container takes 1-2 minutes to fully initialize (model sync, license check, etc.)
- Trial license warning is normal if no license secret is provided — app will still run

---

## Step 7: Updating the Service

**Use ALTER, not DROP/CREATE** to keep the same endpoint URL.

### Quick deploy with script

After making changes in Studio Pro, export a new PAD package and run:

```powershell
.\deploy.ps1 -PadPath "C:\path\to\MyApp_portable_20260608_1147.zip"
```

Or without the parameter (it will prompt you):

```powershell
.\deploy.ps1
```

The script handles: unzipping (if needed), Dockerfile creation, Docker build, push to Snowflake registry, and updating the service spec via `ALTER SERVICE ... FROM SPECIFICATION`. The endpoint URL is preserved.

> **Critical: Why the script uses ALTER SERVICE, not suspend/resume**
>
> SPCS pins the image digest (SHA256) when a service is created or its spec is applied. Even if you push a new image to the same `:latest` tag, suspend/resume will re-pull the **old pinned digest**. `ALTER SERVICE ... FROM SPECIFICATION` re-resolves the tag to the current digest. This is the only way to deploy a new image without changing the endpoint URL.

You can also pass an already-extracted folder:

```powershell
.\deploy.ps1 -PadPath "C:\path\to\extracted\folder"
```

**Prerequisites for the script:**
- Rancher Desktop running
- Already logged in to the registry (`docker login <REGISTRY_HOST>`)
- Snowflake CLI configured with the connection name specified in the script

**Configuration:**

All settings live in `deploy-config.json` alongside the script. Each developer needs their own copy:

```json
{
  "snowConnection": "mendix",
  "service": { "name": "DB.SCHEMA.MENDIX_SERVICE", "imageRepo": "db/schema/repo", "imageName": "mendix-app", "externalAccessIntegration": "MENDIX_PG_EAI" },
  "database": { "host": "<pg-host>", "port": 5432, "name": "postgres", "username": "application", "password": "<pg-password>", "useSsl": true },
  "mendix": { "adminPassword": "<admin-pass>", "fileStorageStage": "@DB.SCHEMA.MENDIX_FILESTORAGE_STAGE" },
  "resources": { "memory": { "request": "2G", "limit": "4G" }, "cpu": { "request": 1, "limit": 2 } }
}
```

The registry host is derived automatically from the snow CLI connection (no need to configure it manually).

Use `-Config "other-file.json"` to point at a different config (e.g., dev vs prod environments).

**Security note:** `deploy-config.json` contains database passwords. Add it to `.gitignore` and ship a `deploy-config.example.json` with placeholder values instead.

---

The public endpoint URL contains a hash that is assigned at service creation time. Dropping and recreating the service generates a new hash (new URL). Suspending, resuming, or altering the spec in-place preserves the URL.

| Operation | URL preserved? | Use when |
|-----------|---------------|----------|
| `ALTER SERVICE ... FROM SPECIFICATION $$...$$` | Yes | Changing env vars, images, resources, probes |
| `ALTER SERVICE ... SUSPEND` / `RESUME` | Yes | Pausing to save credits |
| `ALTER SERVICE ... SET EXTERNAL_ACCESS_INTEGRATIONS = (...)` | Yes | Adding/changing egress rules |
| `DROP SERVICE` + `CREATE SERVICE` | **No** (new URL) | Only when you need a completely fresh start |

**Update the spec (preserves endpoint):**

```sql
ALTER SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE
  FROM SPECIFICATION $$
  ... updated spec here ...
$$;
```

**Suspend/resume (preserves endpoint and local volume data):**

```sql
ALTER SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE SUSPEND;
ALTER SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE RESUME;
```

**Full drop and recreate (new endpoint URL, data in local volumes lost):**
```sql
DROP SERVICE <DATABASE>.<SCHEMA>.MENDIX_SERVICE;
CREATE SERVICE ... ;
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `404 Not Found` on CLI login | Add region to account: `<ACCOUNT_LOCATOR>.<REGION>` |
| `Connection X is not configured` | Check you're editing `connections.toml` (not `config.toml`) and section name matches |
| `Authorization Failure` on docker push | Logout/login with correct hostname (hyphens) and correct role |
| `Splatting operator @` PowerShell error | Wrap stage paths in quotes: `'@STAGE_NAME'` |
| `Network policy is required` on Docker push or CLI | The Snowflake registry requires an account-level network policy to exist. Create a permissive one: `CREATE NETWORK POLICY ALLOW_ALL_POLICY ALLOWED_IP_LIST = ('0.0.0.0/0'); ALTER ACCOUNT SET NETWORK_POLICY = ALLOW_ALL_POLICY;` Do NOT use a restrictive single-IP policy or you'll get locked out when your IP changes. |
| Readiness probe permanently failing on port 8090 | Admin port binds to `localhost` and is not configurable in PAD. Use port 8080 with path `/` instead |
| Readiness probe failing for 2-3 minutes then succeeding | Normal; Mendix PAD takes 2-3 minutes to initialize. Wait before investigating |
| `unknown option secretKeyRef` | Use flat format: `snowflakeSecret: name` not nested `objectName` |
| `stage name must start with @` | Prefix stage name with `@` in volume config |
| `invalid value ... for 'volumes[0].stageConfig.name'` | Stage name must be **fully qualified**: `"@DATABASE.SCHEMA.STAGE_NAME"` |
| App starts but uses HSQLDB instead of PostgreSQL | Env var name is wrong. Use `RUNTIME_PARAMS_DATABASETYPE` (no underscore between DATABASE and TYPE). Check `etc/variables.conf` for correct names |
| File storage path shows `/mendix/app/./data/files` | Wrong env var. Use `RUNTIME_PARAMS_UPLOADEDFILESPATH`, not `MXRUNTIME_com_mendix_storage_localfilesystem_Location` |
| Storage service setting ignored | Use `RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE`, not `MXRUNTIME_com_mendix_core_StorageService` (PAD does not use `MXRUNTIME_` prefix format) |
| MxAdmin login says "incorrect password" | You need `RUNTIME_ADMINUSER_PASSWORD` (creates the app user), not just `M2EE_ADMIN_PASS` (admin API only). Check logs for "MxAdmin user with username 'MxAdmin' does not exist!" |
| Docker pipe error: `open //./pipe/docker_engine: The system cannot find the file specified` | Rancher Desktop's WSL2 backend lost the pipe. Run `wsl --shutdown`, then relaunch Rancher Desktop. |
| Pushed new image but service still runs old code after suspend/resume | SPCS **pins the image SHA256 digest** at service creation time. Suspend/resume re-pulls the same pinned digest, NOT the current `:latest` tag. Fix: use `ALTER SERVICE ... FROM SPECIFICATION $$...$$` which re-resolves the tag. The deploy script does this automatically. If you need a manual fix: `DROP SERVICE` + `CREATE SERVICE` (changes URL). |
| `The given member name 'X' has no corresponding member` in Java action | Mendix associations require fully-qualified names: `"System.UserRoles"` not `"UserRoles"`. Attributes use simple names (`"Name"`, `"Password"`). Use `getMember("Module.AssociationName")` cast to `MendixObjectReferenceSet` and call `addValue()` for many-to-many. |

---

## Architecture (POC)

```
┌─────────────────────────────────────────────────────────┐
│  SPCS Service: MENDIX_SERVICE                           │
│  Compute Pool: MENDIX_POC_POOL (CPU_X64_S)             │
│                                                         │
│  ┌─────────────────────┐  ┌──────────────────────────┐ │
│  │  mendix-app          │  │  postgres                │ │
│  │  Port 8080 (app)     │──│  Port 5432               │ │
│  │  Port 8090 (admin)   │  │  Data: local volume      │ │
│  │  Storage: stage vol   │  └──────────────────────────┘ │
│  └─────────────────────┘                                │
│                                                         │
│  Endpoint: mendix-web (public, Snowflake auth)          │
└─────────────────────────────────────────────────────────┘
```

**Limitations of POC setup:**
- PostgreSQL data is ephemeral (local volume — survives suspend/resume but lost on DROP)
- No horizontal scaling (each instance has its own PG)
- Trial license has user and time limits
- For production: use Snowflake Postgres via Private Link (requires Business Critical edition)