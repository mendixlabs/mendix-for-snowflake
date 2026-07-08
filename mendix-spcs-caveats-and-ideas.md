# Mendix on SPCS: Caveats and Future Ideas

## Caveats

### No custom domain for SPCS endpoints

SPCS assigns a URL of the form `<hash>-<org>-<account>.snowflakecomputing.app` at service creation time. There is no support for custom domains, CNAME mapping, or vanity URLs.

**Impact:** If the service is dropped and recreated, the URL changes. Bookmarks, integrations, and any hardcoded references break.

**Mitigation:** Always use `ALTER SERVICE ... FROM SPECIFICATION` to update the service in-place. Only `DROP SERVICE` + `CREATE SERVICE` generates a new URL.

**Workaround for production:** Place a reverse proxy (Cloudflare, nginx, AWS ALB) in front of the SPCS endpoint with a CNAME on your own domain.

---

### Stage volume performance not yet benchmarked

The Mendix file storage is backed by a Snowflake stage volume. Stage volumes are optimized for large sequential reads/writes, not random I/O.

**Impact:** Large file uploads or high-frequency small file operations may be slower than local disk or S3.

**Action needed:** Benchmark with realistic file sizes and concurrency. If performance is insufficient, add a `block` volume for temp/random-IO workloads alongside the stage volume for documents.

---

### Trial license time limit

Without a Mendix license set, the runtime runs trial-licensed: 6 concurrent users, unlimited
named users, and it stops after a randomly chosen 2-4 hours (SPCS restarts it, losing sessions
and in-memory state).

**Mitigation:** set a license (License ID + License Key, requested from Mendix Support per app
node) via the Admin UI's License section on the Apps page, or at registration. Validation is
local and offline - the key is a signed blob the runtime checks once at startup. No egress is
required or added for this; the security-questionnaire answer stays "Postgres only".

**Native App status (updated 2026-07-02):** implemented. `PUT`/`DELETE /apps/{name}/license`
write the key to a per-app secret (`MXAPP_<NAME>.MX_LICENSE_KEY`) and the id as a plain
`RUNTIME_LICENSE_ID` env var in the service spec, the same pattern already used for constants.
Only the static License ID + License Key model is supported - the documented model for Docker,
Kubernetes DIY, Cloud Foundry, and VM deployments. The "Subscription Secret" model (SAP BTP,
Mendix on Kubernetes in Connected mode) is coupled to the Mendix Operator with no documented
runtime-level setting for a standalone Portable Runtime container, and remains unsupported. See
`PLAN-mendix-licensing.md` section 6 for the upgrade path if Mendix ever exposes one.

---

### SPCS egress IP ranges have an expiry date

The egress IPs used to whitelist SPCS on the Snowflake Postgres network policy (`SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()`) have an `expires` field. Current expiry: **2026-09-07** (roughly 3 months away).

**Impact:** If IPs rotate and the network rule isn't updated, the Mendix app loses database connectivity.

**Action needed:** Before 2026-09-07, re-run `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` and update the `SPCS_TO_PG_INGRESS` network rule with the new CIDRs.

---

### Caller's rights token expiry UX

When a caller token expires mid-session (user idle for >30 minutes without the refresh snippet triggering, or browser tab backgrounded), Snowflake queries throw a `RuntimeException`. The user sees an error page and must log out/in to restore the token.

**Current status:** The `Snippet_TriggerSFTokenRefresh` keepalive mitigates this for active users. Background tabs may still lose their token if the browser suspends JS timers.

**Future improvement:** Catch the auth exception in the query microflow and redirect to `/headersso/` for silent re-authentication instead of showing an error.

---

### JDBC connection pooling per user not implemented

Each call to `GetCompoundToken` + `ExecuteQuery` creates a new JDBC connection (via HikariCP). There is no connection reuse across requests for the same user.

**Impact:** Acceptable for low-concurrency POC/demo use. May become a bottleneck with many concurrent users.

**Future improvement:** Investigate user-keyed connection pooling or session-scoped connection caching in the External Database Connector.

---

### Deploy and constants update pick up whatever `:latest` resolves to

`ALTER SERVICE FROM SPECIFICATION` re-resolves the image tag at call time, so a `:latest`
reference can silently pull a new `mendix-base` version pushed since the last deploy.

**Status:** mitigated in the Native App packaging — the manifest pins all three images by
`@sha256` digest, and the controller spawns per-app services from the pinned
`MENDIX_BASE_IMAGE` value in its spec. The `:latest` fallback only applies when that env var is
unset (local dev outside the app).

---

## Future Ideas

### Multi-instance deployment for HA

**What:** Run `MIN_INSTANCES = 2` for high availability and load balancing.

**Prerequisite:** Already using Snowflake Postgres (shared DB across instances). Done.

**Mendix consideration:** Set `com.mendix.core.isClusterSlave = true` on non-leader instances, or use Mendix's built-in cluster leader election (requires shared DB).

---

### External Access Integration for Mendix egress

**What:** Allow the Mendix container to reach external services beyond the Postgres database.

**Potential needs:**
- `licensing.mendix.com:443` for license validation
- External REST/SOAP APIs consumed by the Mendix app
- SSO/SAML identity providers
- Email (SMTP) servers

**How:** Create network rules for each host:port, bundle into an EAI, attach with `ALTER SERVICE ... SET EXTERNAL_ACCESS_INTEGRATIONS = (...)`.

---

### Private Link for Snowflake Postgres (Business Critical upgrade)

**What:** Replace the current IP-whitelist approach with Private Link for the Postgres connection. Traffic stays on Snowflake's internal backbone instead of traversing the public internet.

**When:** If/when the account is upgraded to Business Critical edition.

**How:** `ALTER POSTGRES INSTANCE ENABLE PRIVATELINK`, provision SPCS endpoint via `SYSTEM$PROVISION_PRIVATELINK_ENDPOINT`, use `PRIVATE_HOST_PORT` network rule type.

---

### Service identity JDBC connection

**What:** Use the SPCS service token (auto-injected at `/snowflake/session/token`) to query Snowflake tables from within Mendix via JDBC. Simpler than caller's rights; runs as the service owner role.

**Use case:** Background jobs, scheduled data syncs, or admin dashboards that don't need per-user access control.

**Status:** Not yet implemented. The caller's rights approach (compound token) is the primary path. Service identity could be added as a simpler alternative for scenarios where per-user access isn't needed.

**How:** Read `/snowflake/session/token`, connect via JDBC with `host=$SNOWFLAKE_HOST`, `authenticator=oauth`, `token=<service-token>`. No caller token needed; no user context required.

---

### Event-table log export for full log history

**What:** SPCS can ship each service's stdout/stderr to a Snowflake event table via a `logExporters` block in the service spec (`eventTableConfig.logLevel: INFO|ERROR|NONE`) instead of relying solely on `SYSTEM$GET_SERVICE_LOGS`. Once flowing, logs are ordinary rows - full SQL (`WHERE`, `ORDER BY`, arbitrary time ranges), no 1000-line/100KB cap, and retention set by the table rather than a best-effort platform buffer. This would be the real fix for O12 in `PLAN-native-app-packaging.md`; today's "Download Logs" background job only dodges the ingress timeout on the same capped tail, it can't retrieve more history than `SYSTEM$GET_SERVICE_LOGS` ever could.

**Constraint / registry impact:** the destination event table is designated account- or database-wide via `ALTER ACCOUNT SET EVENT_TABLE = ...`, which needs ACCOUNTADMIN - an app object can't do this itself, the same shape as the Postgres instance/EAI/secret already carved out as one-time consumer setup (see PLAN §4). Adopting this would add, not just a spec tweak: a new bound `reference` in `manifest.yml` plus its `register_reference`/`get_reference_config` entries in `setup_script.sql` (the app needs `SELECT` on the event table to query it - restricted caller's rights doesn't apply to platform logs), a Setup/Verify page check confirming the table exists and is bound, `logExporters` added to every service spec (per-app, controller, admin UI), and a genuinely new Controller query endpoint, since `/logs` and `/logs/download` both just wrap `SYSTEM$GET_SERVICE_LOGS` and none of that carries over to "search the last 3 days of logs for app X."

**Make it optional, with an Admin UI upgrade path:** the `log_event_table` reference doesn't have to gate `maybe_start_services()` the way `pg_secret`/`pg_eai` do - the controller and admin UI don't need it to start - so the base install path is untouched whether or not a consumer ever binds it. That leaves two independent toggle points once it is bound. **Per-app:** a new `AppRecord.log_export_enabled` boolean, mirroring the existing `use_caller_rights` field exactly (same `UpdateSpecRequest` PUT, same "Service spec" expander on the Apps page, same restart-on-change flow), threaded into `_build_spec` to add/omit `logExporters` for that app's service only. **Platform-wide:** one flag covering the controller + admin-UI system services and the default for newly-created apps, which needs a new home for that setting since no settings table exists today - either a small new registry row or an env var the operator sets on the Setup page that the controller reads at startup. Both toggles would stay disabled in the Admin UI until a Setup/Verify check confirms the reference is actually bound (same readiness-gating pattern already used for `pg_secret`/`pg_eai`), with a pointer to the one-time ACCOUNTADMIN SQL when it isn't. Not started - this is a deliberate scope decision for later, not a drive-by addition to the Download Logs fix (O12 in `PLAN-native-app-packaging.md`).
