# Mendix on Snowpark Container Services

Run Mendix applications natively on Snowflake using Snowpark Container Services (SPCS). No Mendix Cloud, no Kubernetes operator, no external infrastructure. The Mendix runtime runs as a container inside Snowflake, connected to a Snowflake-managed Postgres database, with file storage on Snowflake stages. Users authenticate via Snowflake identity and can query Snowflake data as themselves.

## Screenshots

The optional [Streamlit admin UI](#admin-ui-optional) manages apps from a browser, themed to Siemens iX:

**Apps overview** — service and deploy status for every app you own, with refresh and bulk actions.

![Admin UI — Apps overview](Screenshots/apps-overview.png)

**Register a new app** — provisions the SPCS service, filestorage stage, and secrets; sets the owner role and resource tier.

![Admin UI — Register a new app](Screenshots/register-new-app.png)

**Service logs** — tail any app's logs, plus the controller's and admin UI's own logs for privileged operators.

![Admin UI — Service logs](Screenshots/logs.png)

**Activity** — an audit log of every mutating operation (deploy, suspend, resume, constants/spec edit, delete), recording the operator, action, target app, and outcome.

![Admin UI — Activity audit log](Screenshots/activity-screen.png)

## What This Is

A controller-based deployment toolkit for running Mendix apps on SPCS:

- **Controller** (`Controller/`) - A FastAPI service that runs inside SPCS and manages the full app lifecycle: provisioning services, storing constants as Snowflake secrets, and deploying new PAD versions without Docker rebuilds per app.
- **`upload-pad.ps1`** - The operator-facing deploy script. Uploads a Mendix PAD zip to the Snowflake stage and calls the controller API to trigger a deploy. No Docker build required per deploy.
- **`setup.ps1`** - One-time infrastructure setup: creates the controller role, secrets, stage, app registry table, and controller service.
- **Admin UI** (`Admin UI/`) - Streamlit-based admin frontend that runs as a sibling SPCS service. Calls the controller over internal SPCS DNS and lets operators manage apps from a browser. Pages: app status and lifecycle (deploy, suspend, resume, delete), PAD upload, constants editor, logs, activity audit log, and a privileged Infrastructure page for compute pool resize. Multi-tenant: each app carries an `owner_role` and operators see only apps owned by roles they hold.
- **Mendix Base Image** (`Mendix Base Image/`) - A generic Mendix runner image. Built once and shared across all apps. No app code baked in — the app is loaded from the stage at container startup.
- **SnowflakeSSO module** - Mendix module that reads the `Sf-Context-Current-User` header injected by SPCS, auto-logs users in using their Snowflake identity, and captures the caller token for querying Snowflake data as the end user.
- **[mendix-spcs-howto.md](mendix-spcs-howto.md)** - Full setup and deployment guide.
- **[mendix-spcs-caveats-and-ideas.md](mendix-spcs-caveats-and-ideas.md)** - Known limitations and future work.

## Architecture

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
   │  app service │  │  app service │  │  ...         │
   │ mendix-base  │  │ mendix-base  │  │              │
   └──────────────┘  └──────────────┘  └──────────────┘
              │               │
              └───────────────┘
                      │
              Shared: compute pool, Postgres instance, EAI,
                      MENDIX_DEPLOY_STAGE (PAD zips)
```

- **Compute**: SPCS compute pool (CPU_X64_S or larger), shared across all app services
- **Database**: Snowflake Postgres instance; one database per app, auto-created at startup
- **File storage**: Snowflake internal stage mounted as a volume (files are queryable from SQL)
- **Auth**: Snowflake OAuth on the endpoint; SnowflakeSSO module maps Snowflake users to Mendix users
- **End-user access**: each app's public endpoint is gated by a per-app account role (`APP_<NAME>_USER`); only members of that role, the app's `owner_role`, or a privileged role reach the app. Membership is managed in the IdP via SCIM. See the howto's [Access model](mendix-spcs-howto.md#access-model-multi-tenant-isolation).
- **Data access**: Caller's rights with compound token; queries execute as the logged-in user
- **Deploy**: PAD zip uploaded to stage via Snow CLI; controller alters or restarts the service in-place, preserving the endpoint URL

## Prerequisites

- Mendix Studio Pro 10.24.19+ or 11.6.5+ (Portable App Distribution export)
- Snowflake account with ACCOUNTADMIN access
- Snowflake CLI (`snow`) 3.x+
- PowerShell 5.1+

## Quick Start

Full one-time setup is in [mendix-spcs-howto.md](mendix-spcs-howto.md). The short version:

1. Build and push the `mendix-base` image once (shared across all apps)
2. Run `.\Controller\setup.ps1` to provision the controller infrastructure
3. Build and push the `mendix-deploy-controller` image, then wait for the controller service to reach RUNNING (see the howto, Step 4)
4. For each app, export a Portable App Distribution from Studio Pro and run:

```powershell
.\Controller\upload-pad.ps1 `
  -AppName "my-app" `
  -PadPath "C:\path\to\MyApp_portable_20261201.zip" `
  -ControllerUrl "https://<controller-ingress>.snowflakecomputing.app" `
  -Token "<controller-pat>" `
  -Config ".\Controller\controller-config.json" `
  -AppConfig "C:\path\to\my-app-config.json"   # first deploy only
```

Subsequent deploys omit `-AppConfig`. The script uploads the PAD to the Snowflake stage, triggers the controller, and polls until the app is READY.

## Deploying a New Version

No Docker build. Export a new PAD from Studio Pro and run `upload-pad.ps1` without `-AppConfig`:

```powershell
.\Controller\upload-pad.ps1 `
  -AppName "my-app" `
  -PadPath "C:\path\to\MyApp_portable_20261215.zip" `
  -ControllerUrl "https://<controller-ingress>.snowflakecomputing.app" `
  -Token "<controller-pat>" `
  -Config ".\Controller\controller-config.json"
```

## Querying Snowflake Data

Mendix microflows can query Snowflake tables using the logged-in user's identity (caller's rights). The SnowflakeSSO module captures a compound token (service + user), which authenticates JDBC connections over the internal Snowflake network. No EAI or external egress needed for this path.

See [mendix-spcs-howto.md](mendix-spcs-howto.md) for setup details.

## File Storage

Files uploaded through Mendix land on a Snowflake stage and are immediately queryable:

```sql
LIST @YOUR_DB.PUBLIC.MYAPP_FILESTORAGE_STAGE;
SELECT $1 FROM @YOUR_DB.PUBLIC.MYAPP_FILESTORAGE_STAGE/export.csv (FILE_FORMAT => 'csv_format');
```

## Cost

SPCS compute pools charge per hour of runtime. A CPU_X64_S pool costs 0.11 credits/hour. The compute pool auto-suspends when all services are suspended (`AUTO_SUSPEND_SECS = 3600`). See the howto for scheduled suspend/resume task examples.

## Known Limitations

- SPCS endpoints get a fixed `<hash>-<account>.snowflakecomputing.app` URL — no custom domains
- Snowflake Postgres network policy must be updated when SPCS egress IP ranges rotate (current expiry: 2026-09-07)
- Trial Mendix license terminates the runtime after ~2 hours; a production license requires egress to `licensing.mendix.com`
- Caller's rights tokens expire after 30 minutes; the SnowflakeSSO refresh snippet must be present in the Main Layout

See [mendix-spcs-caveats-and-ideas.md](mendix-spcs-caveats-and-ideas.md) for the full list.
