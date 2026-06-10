# Mendix on SPCS: Caveats and Future Ideas

## Caveats

### No custom domain for SPCS endpoints

SPCS assigns a URL of the form `<hash>-<org>-<account>.snowflakecomputing.app` at service creation time. There is no support for custom domains, CNAME mapping, or vanity URLs.

**Impact:** If the service is dropped and recreated, the URL changes. Bookmarks, integrations, and any hardcoded references break.

**Mitigation:** Always use `ALTER SERVICE ... FROM SPECIFICATION` to update the service in-place. Only `DROP SERVICE` + `CREATE SERVICE` generates a new URL.

**Workaround for production:** Place a reverse proxy (Cloudflare, nginx, AWS ALB) in front of the SPCS endpoint with a CNAME on your own domain.

---

### Snowflake authentication layer on public endpoints

All public SPCS endpoints require Snowflake authentication. Browser access redirects through Snowflake's OAuth flow; programmatic access requires a PAT in the Authorization header.

**Impact:** End users must have Snowflake accounts and be granted access to the service endpoint. This prevents anonymous/public-facing apps.

**Open question:** Does Mendix's session management (cookies, XASSESSIONID) work correctly behind Snowflake's auth proxy? Initial testing shows the React client works, but more validation is needed for SSO modules, deep links, and API consumers.

---

### Stage volume performance not yet benchmarked

The Mendix file storage is backed by a Snowflake stage volume. Stage volumes are optimized for large sequential reads/writes, not random I/O.

**Impact:** Large file uploads or high-frequency small file operations may be slower than local disk or S3.

**Action needed:** Benchmark with realistic file sizes and concurrency. If performance is insufficient, add a `block` volume for temp/random-IO workloads alongside the stage volume for documents.

---

### Trial license time limit

Without a production license, the Mendix runtime terminates after ~2 hours. SPCS auto-restarts it, but sessions and in-memory state are lost.

**Mitigation:** Add license env vars (`RUNTIME_LICENSE_ID`, `RUNTIME_LICENSE_KEY`). License validation may require egress to `licensing.mendix.com:443` via an EAI.

---

### SPCS egress IP ranges have an expiry date

The egress IPs used to whitelist SPCS on the Snowflake Postgres network policy (`SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()`) have an `expires` field. Current expiry: **2026-09-07**.

**Impact:** If IPs rotate and the network rule isn't updated, the Mendix app loses database connectivity.

**Action needed:** Monitor before expiry. Re-run `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` and update the `SPCS_TO_PG_INGRESS` network rule with new CIDRs.

---

## Future Ideas

### Query Snowflake data as the end user (caller's rights)

**What:** Execute Snowflake SQL from the Mendix app using the authenticated end user's identity and role, enabling per-user data access and row-level security.

**How:** Enable `executeAsCaller: true` in the service spec. SPCS injects `Sf-Context-Current-User-Token` per request. Use this token with the Snowflake JDBC driver to open a session as the calling user.

**Two modes of Snowflake access:**

| Mode | Runs as | Use case |
|------|---------|----------|
| Service identity (`/snowflake/session/token`) | Service owner role | Shared queries, background jobs |
| Caller's rights (`Sf-Context-Current-User-Token`) | End user's role | User-scoped dashboards, RLS |

**Open questions:**
- Can the Mendix Database Connector module use a dynamic token per request?
- Performance: new JDBC connection per user vs. pooling by user
- Token validity (default 2 min, max 7 days via `SERVICE_CALLER_TOKEN_VALIDITY_SECS`)

---

### Connecting Mendix to Snowflake data (service identity)

**What:** Use the SPCS service token (auto-injected at `/snowflake/session/token`) to query Snowflake tables from within Mendix via JDBC. Simpler than caller's rights; runs as the service owner.

**Use case:** Mendix apps that visualize or manipulate Snowflake data without ETL. The app runs next to the data.

**Open questions:**
- Can Mendix's Database Connector module connect to Snowflake via JDBC inside an SPCS container?
- Does the auto-refreshed OAuth token work with the standard Snowflake JDBC driver?
- Performance implications of mixing Snowflake queries with the app's transactional PG database?

---

### Auto-suspend/resume for cost management

**What:** SPCS services with public endpoints do not auto-suspend based on HTTP inactivity. The compute pool charges credits as long as the service is running.

**Workaround ideas:**
- Scheduled task to suspend at night: `CREATE TASK ... AS ALTER SERVICE ... SUSPEND;`
- Service auto-resumes when a user hits the endpoint (if `AUTO_RESUME = TRUE` on pool and service)
- Users see a brief error page for 30-60 seconds while the service wakes up

---

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
