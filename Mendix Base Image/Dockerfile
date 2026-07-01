# Base pinned to an immutable digest for reproducible, scannable builds (NAAAPS).
# JRE, not JDK: the Mendix Runtime only needs a JRE to run a deployed app (JDK is a
# Studio Pro development-time concern) - JRE drops the compiler/jshell/jarsigner etc.
# Refresh: docker pull eclipse-temurin:21-jre && docker inspect --format '{{index .RepoDigests 0}}' eclipse-temurin:21-jre
FROM eclipse-temurin:21-jre@sha256:8ec353b20d3aab0758572236b81b967c7077c40c4d0819ce97f9a1329d684603
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client unzip && rm -rf /var/lib/apt/lists/* \
    # temurin bundles Canonical's pebble service manager (not dpkg-owned); this
    # image never uses it and its Go deps carry CVEs, so remove it outright.
    && rm -f /usr/bin/pebble
WORKDIR /mendix
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
    && useradd --system --no-create-home mendixuser \
    && chown -R mendixuser:mendixuser /mendix
USER mendixuser
EXPOSE 8080 8090
CMD ["./entrypoint.sh"]
