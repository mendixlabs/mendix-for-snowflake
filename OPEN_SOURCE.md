# Open Source Materials

Project source is licensed under the MIT License in [LICENSE.txt](LICENSE.txt),
copyright (c) 2026 Siemens AG. Third-party components keep their own licenses
and notices.

## Automated evidence

The `OSS evidence` GitHub Actions workflow runs for every pull request update,
every commit pushed to `main`, release tags matching `v*`, and manual dispatches.
It builds all three images from a clean checkout, verifies required license and
notice files, creates Syft and SPDX SBOMs, inventories licenses and copyleft
candidates, resolves source references, and uploads one combined review bundle.
Per-image artifacts are retained for 7 days; the combined bundle is retained for
90 days.

The release build also captures the exact Snowflake registry image digests so an
evidence bundle can be matched to the distributed images.

## Evidence contents

Each generated bundle contains exact image identities, Syft and SPDX SBOMs,
direct and transitive dependency inventories, extracted notices and copyright
files, source references, copyleft candidates, manual review items, build-input
hashes, and a SHA-256 manifest. The bundle records the Git commit and workflow
run that produced it.

## Runtime and module materials

The Python service images include the project license, CPython license, installed
package license files, Streamlit's upstream `NOTICES`, copied Debian copyright
files, common Debian license texts, and a machine-readable inventory under
`/licenses/`.

The Java image includes the project license, Temurin legal files under
`/opt/java/openjdk/legal/`, its OS package inventory, and Debian copyright files.
The SnowflakeSSO MPK now includes `LICENSE.txt` at its archive root. Its existing
12 entries were verified byte-for-byte unchanged when the license was added.

