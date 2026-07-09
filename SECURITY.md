# Security Policy

## Reporting a vulnerability

Report vulnerabilities through Siemens ProductCERT's vulnerability handling and disclosure
process: https://www.siemens.com/global/en/products/services/cert/vulnerability-process.html.
Alternatively, email security@mendix.com.

To reach the maintainer of this repository directly and fastest, open a GitHub issue:
https://github.com/mendixlabs/mendix-for-snowflake/issues. For a sensitive
finding you would rather not disclose in the open, use GitHub's private vulnerability
reporting on the repository's Security tab instead, which reaches the maintainer just as
quickly without a public issue.

## Automated security scanning

Every push and pull request that touches the Controller, Admin UI, or Mendix Base Image code
runs `.github/workflows/security-scan.yml`, a seven-job pipeline:

- **pip-audit** scans the Controller's and Admin UI's pinned Python dependencies for known
  CVEs and fails the build on any finding.
- **Trivy image scan** builds all three shipped images (Controller, Admin UI, base image)
  and scans them. The build fails on any CRITICAL or HIGH severity CVE with a fix available
  (`ignore-unfixed`); MEDIUM and LOW findings are reported as SARIF to the repository's
  Security tab.
- **Bandit** runs static analysis over the Controller and Admin UI source, gated on MEDIUM
  and HIGH severity findings, with the full report uploaded as SARIF.
- **Trivy secret scan** checks the repository for committed secrets; any detected secret
  fails the build.
- **ClamAV** scans the exported root filesystem of each shipped image for malware; any hit
  fails the build.
- **shellcheck** lints the base image's `entrypoint.sh`.
- **PSScriptAnalyzer** lints the repo's PowerShell scripts (build/release/repackage tooling).

The workflow also runs on a weekly schedule (Mondays, 06:00 UTC) so unchanged, digest-pinned
images are re-checked against freshly updated vulnerability databases, and it can be
triggered manually via `workflow_dispatch`.

CodeQL (`.github/workflows/codeql.yml`) adds semantic static analysis for Python and for the
Java SnowflakeSSO module (App Components) on every push and pull request to `main` and on the
same weekly cadence. Dependabot (`.github/dependabot.yml`) opens weekly pull requests for
outdated pip, Docker base image, and GitHub Actions dependencies, complementing pip-audit's
point-in-time scans. `.github/workflows/tests.yml` runs an offline pytest suite (roughly 490
tests) covering the Controller and Admin UI on every push and pull request, gated on a minimum
coverage percentage per service so coverage cannot silently regress to zero on an untested file.

In summary: CodeQL and Bandit cover Python static analysis, PSScriptAnalyzer covers the
PowerShell tooling, pip-audit and Dependabot cover dependencies, Trivy covers container images
and committed secrets, ClamAV covers malware, shellcheck covers shell scripts, and the pytest
suite covers functional regressions.

## Vulnerability remediation

The primary control is a fail-closed CI gate: a build containing a known CRITICAL or HIGH
severity finding that has a fix available does not ship, because CI blocks the merge before
the image can be released. This gate runs on every push and pull request and on the weekly
schedule, so it applies independently of maintainer availability.

This application is maintained by a single developer, so fixes and mitigations are merged on
a best-effort basis rather than against fixed calendar deadlines. Severity drives
prioritization: CRITICAL and HIGH findings are addressed ahead of MEDIUM and LOW.

Released app versions additionally reference images by immutable sha256 digest, so a
released artifact is exactly the build that passed these gates.

## Data isolation between Mendix apps

Each Mendix app deployed by this controller gets its own Postgres role and
password, scoped to only that app's own database. The controller provisions
the per-app role and database when the app is registered; the shared
`application` bootstrap credential used to create those per-app roles is held
only by the trusted controller and is never mounted into an app container.

## SSO identity trust and caller tokens

The SnowflakeSSO module's `HeaderSSOHandler` trusts the `Sf-Context-Current-User` header
identity presented by SPCS ingress. Whenever the request also carries a caller OAuth token
(`Sf-Context-Current-User-Token`, present when `use_caller_rights` is enabled), the handler
opens a real caller-rights Snowflake session and checks `CURRENT_USER()` against the claimed
header identity before creating a Mendix session for it, rejecting a confirmed mismatch. This
is defense-in-depth for any path where the SPCS ingress boundary is not the only way to reach
the handler (e.g. a co-resident container in the same compute pool); it does not block login on
a Snowflake connectivity failure, since an inconclusive check is not the same as a confirmed
forged identity.

The live caller token itself is never persisted to the database: it is cached in memory only,
for the container process's own lifetime, and expires after a short TTL well under the token's
real validity window. A container restart or cache expiry simply requires the user to log back
in - it does not degrade to a stored, at-rest credential the way an earlier version of this
module did.

## Organizational context

This application is developed by a Siemens business (Mendix). For broader context, Siemens (our
parent company) holds a TÜV SÜD IEC 62443-4-1 certification covering the secure development
lifecycle of certain of its product lines, and Mendix publishes a platform-level secure
development lifecycle that includes mandatory peer review, Snyk software composition analysis,
Veracode SAST, SonarQube quality gates, and monthly external penetration tests:
https://www.mendix.com/evaluation-guide/security/secure-development-lifecycle/.

Those are organizational and platform-level controls provided for context. The Siemens IEC
62443-4-1 certification is scoped to specific Siemens product lines and does not itself cover
this repository, and the Mendix platform controls govern the Mendix product platform, not this
repository. This repository has a single maintainer and does not itself go through the
platform's peer review, Snyk, Veracode, SonarQube, or penetration testing. What governs this
repository is the automated CI pipeline and best-effort remediation described above.
