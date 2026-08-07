# Base pinned to an immutable digest for reproducible, scannable builds (NAAAPS).
# JRE, not JDK: the Mendix Runtime only needs a JRE to run a deployed app (JDK is a
# Studio Pro development-time concern) - JRE drops the compiler/jshell/jarsigner etc.
# Refresh: docker pull eclipse-temurin:21-jre && docker inspect --format '{{index .RepoDigests 0}}' eclipse-temurin:21-jre
FROM eclipse-temurin:24-jre@sha256:8cb2387a28af84cf0db0948d9c67d4480192f4e567027a3963f145d218e8b4f2
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client unzip && rm -rf /var/lib/apt/lists/* \
    # temurin bundles Canonical's pebble service manager (not dpkg-owned); this
    # image never uses it and its Go deps carry CVEs, so remove it outright.
    && rm -f /usr/bin/pebble
WORKDIR /mendix
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
    && useradd --system --no-create-home --home-dir /mendix mendixuser \
    && chown -R mendixuser:mendixuser /mendix
USER mendixuser
# --home-dir /mendix (not just $HOME below) matters: the JDK reads user.home from
# getpwuid()'s passwd entry, not from $HOME - without it, java.util.prefs.
# FileSystemPreferences keeps resolving to /home/mendixuser (unwritable, since
# --no-create-home never made it), and every ~30s sync retries mkdir/lock there and
# logs a BackingStoreException. $HOME is still set for shell tools that do read it.
ENV HOME=/mendix
EXPOSE 8080 8090
CMD ["./entrypoint.sh"]
