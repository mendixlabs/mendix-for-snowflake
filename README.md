# Mendix on Snowpark Container Services

Run Mendix applications natively on Snowflake using Snowpark Container Services (SPCS). No Mendix Cloud, no Kubernetes operator, no external infrastructure. The Mendix runtime runs as a container inside Snowflake, connected to a managed Snowflake Postgres database, with file storage on Snowflake stages.

## What This Is

A deployment toolkit for running Mendix apps on SPCS:

- **deploy.ps1** - One-command deploy script. Takes a Mendix PAD package (zip), builds a Docker image, pushes to the Snowflake registry, and updates the running service.
- **deploy-config.json** - Configuration file with database, service, and resource settings. Each developer maintains their own copy.
- **SnowflakeSSO module** - Mendix module that reads the `Sf-Context-Current-User` header injected by SPCS and auto-logs users into Mendix using their Snowflake identity. No separate Mendix login required. Requires the included `login.html` to replace the default Mendix login page.
- **mendix-spcs-howto.md** - Step-by-step setup guide covering Snowflake infrastructure, Postgres instance, image registry, service deployment, and troubleshooting.

## Architecture

```
User (Snowflake Auth) --> SPCS Public Endpoint --> Mendix Runtime (JDK 21)
                                                      |
                                                      |--> Snowflake Postgres (managed, persistent)
                                                      |--> Snowflake Stage Volume (file storage)
```

- **Compute**: SPCS compute pool (CPU_X64_S or larger)
- **Database**: Snowflake Postgres instance, connected via EAI with SPCS egress IP whitelisting
- **File storage**: Snowflake internal stage mounted as a volume (files are queryable from SQL)
- **Auth**: Snowflake OAuth on the endpoint; SSO module maps Snowflake users to Mendix users
- **Deploy**: `ALTER SERVICE FROM SPECIFICATION` preserves the endpoint URL across deploys

## Prerequisites

- Mendix Studio Pro 10.24.19+ or 11.6.5+ (Portable App Distribution support)
- Snowflake account with ACCOUNTADMIN access
- Rancher Desktop or Docker Desktop (dockerd engine, linux/amd64)
- Snowflake CLI (`snow`) installed and configured

## Quick Start

The script handles extraction, Docker build, registry push, and service update. First-time setup requires running the infrastructure SQL from the howto.

1. Create a Portable App Distribution package in Studio Pro (App > Create Deployment Package > Portable package)
2. Copy `deploy-config.json`, fill in your environment values
3. Run:

```powershell
.\deploy.ps1 -PadPath "path\to\MyApp_portable_20260609.zip"
```

## File Storage

Files uploaded through Mendix land on a Snowflake stage and are immediately queryable:

```sql
LIST @MENDIX_FILESTORAGE_STAGE;
SELECT $1 FROM @MENDIX_FILESTORAGE_STAGE/export.csv (FILE_FORMAT => 'csv_format');
```

Mendix apps become a data ingestion interface: users upload files through the app, data is available for Snowflake analytics without ETL.

## SSO

The SnowflakeSSO module eliminates the Mendix login page. SPCS authenticates users via Snowflake OAuth before requests reach the container, then injects a trusted `Sf-Context-Current-User` header. The module reads this header, creates/finds the corresponding Mendix user, and establishes a session automatically.

## Cost

SPCS compute pools charge per hour of runtime. A CPU_X64_S pool costs 0.11 credits/hour ($0.32/hour at $3/credit). The deploy-config includes scheduled tasks to suspend the service outside office hours.

## Docs

- [mendix-spcs-howto.md](mendix-spcs-howto.md) - Full setup and deployment guide
- [mendix-spcs-caveats-and-ideas.md](mendix-spcs-caveats-and-ideas.md) - Known limitations and future work

## Requirements / Limitations

- Snowflake Postgres requires a network policy with SPCS egress IPs whitelisted (IP ranges rotate; monitor expiry)
- SPCS endpoints require Snowflake authentication (no anonymous/public-facing apps)
- No custom domain support on SPCS endpoints
- Trial Mendix license terminates after ~2 hours (production license recommended)
- Stage volumes do not support random writes or file appends (fine for Mendix's write-once file pattern)
