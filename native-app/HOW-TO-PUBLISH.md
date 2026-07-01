# How to Publish: Mendix Native App on Snowflake Marketplace

This document covers the **provider workflow** only: building images, cutting a versioned release, validating it with a same-account install, and creating the private listing.

Consumer install instructions are in `app/readme.md` (shown in Snowsight at install time) and `../mendix-spcs-howto.md`.

> **Authority + verification.** The authoritative design is `../PLAN-native-app-packaging.md` §8 (build & release pipeline) and §9 (R1, the EXTERNAL/NAAAPS gate); this file is the operational runbook. The `snow app version create` and `snow app release-directive set` invocations below are verified against `snow app … --help`. Items still to confirm against live behavior are tagged **(verify at run)** — do not treat them as settled.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker (Rancher Desktop `dockerd (moby)`, or Docker Desktop) | Build and push the three images |
| Snowflake CLI (`snow`) 3.x+ | Version create, release directive |
| PowerShell 5.1+ | Build scripts |
| Provider Snowflake account | Must own `MENDIX_SPCS_PKG`; ACCOUNTADMIN for first-time setup |

**`native-app/scripts/native-app-config.json`** (gitignored; copy from `native-app-config.example.json`):

```json
{
  "snowConnection": "<snow-cli-connection-name>",
  "providerDb":     "<PROVIDER_DATABASE>",
  "providerSchema": "<PROVIDER_SCHEMA>",
  "repo":           "MENDIX_NATIVE_REPO",
  "version":        "v1",
  "distribution":   "INTERNAL",
  "listingName":    "MENDIX_SPCS_LISTING",
  "consumerTargets": ["<ORG.CONSUMER_ACCOUNT>"]
}
```

`distribution` starts as `INTERNAL`. Flipping it to `EXTERNAL` starts the NAAAPS review — do not do this until the business is ready to publish (see [Distribution gate](#distribution-gate-external-listing) below).

---

## One-Time Provider Setup

Run once when the provider account is first configured:

```sql
-- Image repository (sibling to the package, not inside it)
CREATE DATABASE IF NOT EXISTS <PROVIDER_DATABASE>;
CREATE SCHEMA IF NOT EXISTS <PROVIDER_DATABASE>.<PROVIDER_SCHEMA>;
CREATE IMAGE REPOSITORY IF NOT EXISTS <PROVIDER_DATABASE>.<PROVIDER_SCHEMA>.MENDIX_NATIVE_REPO;

-- Application package
CREATE APPLICATION PACKAGE IF NOT EXISTS MENDIX_SPCS_PKG;
```

Confirm the image registry URL: `snow spcs image-registry url --connection <conn>`

---

## Cutting a Release

### Step 1: Build and push images

Images must be pushed **before** version-create; the version freezes the image digests at creation time.

```powershell
cd native-app\scripts
.\build-and-push.ps1
```

This builds `mendix-deploy-controller`, `mendix-admin-ui`, and `mendix-base` for `linux/amd64`, pushes them to the provider registry, and stages a token-resolved deploy copy at `native-app/.build/`. It also captures the `@sha256:...` digest of each pushed image and rewrites every `:latest` tag to the immutable digest in the staged `manifest.yml` and `setup_script.sql`, so the frozen version pins the exact images that passed the security review.

### Step 2: Create the version

```powershell
cd ..\\.build
snow app version create v1 --skip-git-check --connection <conn>
```

`--skip-git-check` is normally required: `.build/` is gitignored and the working tree
carries untracked files, so the CLI's clean-tree check otherwise blocks. Run from `.build/`
so the version freezes the token-resolved manifest (real provider FQNs), not the `<PROVIDER_*>`
tokens in the committed `app/`.

Confirm it was created:

```sql
SHOW VERSIONS IN APPLICATION PACKAGE MENDIX_SPCS_PKG;
-- Observed for a fresh INTERNAL v1: state = READY, review_status = NOT_REVIEWED.
-- review_status tracks the EXTERNAL NAAAPS security review; INTERNAL distribution
-- has no such review and no publish gate, so NOT_REVIEWED never blocks
-- version-create, install, or an INTERNAL release directive.
```

---

## Same-Account Install Dry-Run

This is the validation step: install the app **from the frozen version** (not from the live dev stage). Mechanically identical to a consumer install from the listing.

### Install

```sql
CREATE APPLICATION MENDIX_SPCS_APP
  FROM APPLICATION PACKAGE MENDIX_SPCS_PKG
  USING VERSION v1 PATCH 0;
```

### Grant and bind

```sql
GRANT CREATE COMPUTE POOL   ON ACCOUNT TO APPLICATION MENDIX_SPCS_APP;
GRANT CREATE WAREHOUSE      ON ACCOUNT TO APPLICATION MENDIX_SPCS_APP;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO APPLICATION MENDIX_SPCS_APP;
GRANT APPLICATION ROLE MENDIX_SPCS_APP.app_admin TO ROLE ACCOUNTADMIN;

-- Required for executeAsCaller services (cannot be set by the app itself)
ALTER ACCOUNT SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800;

CALL MENDIX_SPCS_APP.app_public.grant_callback(
  ARRAY_CONSTRUCT('CREATE COMPUTE POOL','CREATE WAREHOUSE','BIND SERVICE ENDPOINT'));

CALL MENDIX_SPCS_APP.app_public.register_reference(
  'pg_secret', 'ADD',
  SYSTEM$REFERENCE('SECRET', '<db>.PUBLIC.MENDIX_NATIVE_PG_SECRET', 'PERSISTENT', 'READ'));

CALL MENDIX_SPCS_APP.app_public.register_reference(
  'pg_eai', 'ADD',
  SYSTEM$REFERENCE('EXTERNAL_ACCESS_INTEGRATION', 'MENDIX_PG_EAI', 'PERSISTENT', 'USAGE'));
```

### Verify

```sql
SHOW SERVICES IN APPLICATION MENDIX_SPCS_APP;
-- controller + admin UI should reach RUNNING within ~2 minutes
```

Open the admin UI endpoint (Apps > `MENDIX_SPCS_APP` > Endpoints in Snowsight), register a test app, deploy a PAD, confirm the per-app service reaches RUNNING.

### Teardown

Drop only the application object — keep the package and version.

```sql
DROP APPLICATION MENDIX_SPCS_APP CASCADE;
-- CASCADE drops the app-owned compute pool, warehouse, and services
ALTER POSTGRES INSTANCE MENDIX_PG SUSPEND;   -- stop cost
```

---

## Setting the Release Directive

Once the version is validated, set it as the default so consumers get it. The CLI form is the verified path (`directive` is positional, `--version` and `--patch` required):

```powershell
snow app release-directive set default --version v1 --patch 0 --connection <conn>
```

The equivalent SQL form (both `VERSION` and `PATCH` are required per docs) — prefer the CLI command above:

```sql
ALTER APPLICATION PACKAGE MENDIX_SPCS_PKG
  SET DEFAULT RELEASE DIRECTIVE VERSION = v1 PATCH = 0;
```

For an EXTERNAL package this is gated: the release directive cannot be set until `review_status = APPROVED` (see [Distribution gate](#distribution-gate-external-listing)).

---

## Patching an Existing Version

For a code change within the same version (v1 patch 1, patch 2, ...):

```powershell
# 1. Push new images
cd native-app\scripts && .\build-and-push.ps1

# 2. Create a new patch (omit the version name; CLI increments the patch counter)
cd ..\\.build
snow app version create v1 --connection <conn>   # creates v1 patch N+1

# 3. Update the release directive
snow app release-directive set default --version v1 --patch <N+1> --connection <conn>
```

Consumers on the listing upgrade by running (`PATCH` is optional — omit to get the latest patch):

```sql
ALTER APPLICATION MENDIX_SPCS_APP UPGRADE USING VERSION v1 PATCH <N+1>;
```

or via Snowsight if auto-upgrade is enabled on the listing.

---

## Distribution Gate: External Listing

> **Do not proceed until the business is ready to publish.** Flipping to `EXTERNAL` starts Snowflake's NAAAPS automated security review. For container apps this also requires a one-time Snowflake Product Security provider approval. Both are slow, Siemens-level steps and cannot be undone.

When ready:

```sql
-- Accept provider ToS (shown once)
ALTER APPLICATION PACKAGE MENDIX_SPCS_PKG SET DISTRIBUTION = EXTERNAL;
```

Wait for `review_status = APPROVED` in `SHOW VERSIONS IN APPLICATION PACKAGE MENDIX_SPCS_PKG` **and** confirmation of Product Security provider approval from Snowflake.

Then create the private listing (targeted accounts, not a public Marketplace entry):

```sql
CREATE EXTERNAL LISTING MENDIX_SPCS_LISTING
  APPLICATION PACKAGE MENDIX_SPCS_PKG
  AS '<rendered listing-manifest.yml>'
  PUBLISH = FALSE REVIEW = TRUE;
```

The listing manifest is in `native-app/listing/listing-manifest.template.yml` (tokens resolved by `release.ps1`). The version comes from the release directive, not the manifest.

Verify: `SHOW LISTINGS;`

---

## Known Gaps

- **Cross-account consumer install not yet validated.** The dry-run above is same-account only. A true cross-account GET (listing → fresh consumer account) requires a second Snowflake account in the production target org, cross-account image access, and a complete from-scratch ACCOUNTADMIN prerequisites run (the same-account dry-run reuses an existing `MENDIX_PG` instance and EAI).
- **`review_status = REJECTED`**: if NAAAPS rejects, file a sev-4 support ticket with a remediation/appeal. Do not pre-remediate speculatively.
- **Private listing manifest open items (verify at listing-create time):** `listing_terms.type: CUSTOM` is set in the template (matched to the live sibling listing); confirm a private targeted listing accepts it. Also unconfirmed: whether `subtitle`/`profile` are mandatory for a private targeted listing, and whether a same-region manifest may omit `auto_fulfillment`.
