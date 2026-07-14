# Bases pinned to immutable digests for reproducible, scannable builds (NAAAPS).
# Refresh: docker pull python:3.12-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
# Refresh: docker pull gcr.io/distroless/cc-debian13:nonroot && docker inspect --format '{{index .RepoDigests 0}}' gcr.io/distroless/cc-debian13:nonroot
# Local-only debug variant: swap the final FROM to gcr.io/distroless/cc-debian13:debug-nonroot
# (adds a BusyBox shell). Never push the debug variant.

# --- builder ---------------------------------------------------------------
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
# Runtime-writable dirs for uid 65532 (nonroot): snowflake-connector OCSP cache.
# Created here because the distroless final stage has no shell/mkdir.
RUN mkdir -p /home/nonroot/.cache/snowflake \
    && chown -R 65532:65532 /home/nonroot /app
# System libraries CPython's stdlib extensions link against but distroless cc
# does not ship (verified by ldd over /usr/local: ctypes->libffi, bz2, lzma,
# sqlite3, uuid, crypt, dbm/gdbm, curses, readline). Staged here, copied into
# the runtime's /usr/lib/x86_64-linux-gnu below.
RUN mkdir -p /opt/runtime-libs \
    && cp -a /usr/lib/x86_64-linux-gnu/libffi.so.8* \
             /usr/lib/x86_64-linux-gnu/libbz2.so.1* \
             /usr/lib/x86_64-linux-gnu/liblzma.so.5* \
             /usr/lib/x86_64-linux-gnu/libsqlite3.so.0* \
             /usr/lib/x86_64-linux-gnu/libuuid.so.1* \
             /usr/lib/x86_64-linux-gnu/libcrypt.so.1* \
             /usr/lib/x86_64-linux-gnu/libgdbm.so.6* \
             /usr/lib/x86_64-linux-gnu/libdb-5.3.so \
             /usr/lib/x86_64-linux-gnu/libncursesw.so.6* \
             /usr/lib/x86_64-linux-gnu/libpanelw.so.6* \
             /usr/lib/x86_64-linux-gnu/libtinfo.so.6* \
             /usr/lib/x86_64-linux-gnu/libreadline.so.8* \
             /opt/runtime-libs/

# --- runtime ---------------------------------------------------------------
# distroless/cc-debian13 = glibc + libssl + libstdc++, no shell, no package
# manager. Same Debian 13 (trixie) userland as the python:3.12-slim builder
# (glibc 2.41), so native wheels (cryptography, pydantic-core, uvloop,
# httptools) are ABI-compatible. cc-debian12 does NOT work here: its glibc
# 2.36 predates the GLIBC_2.38 symbols libpython3.12.so needs.
# NOTE: no RUN is possible in this stage (no shell). Anything needing mkdir/
# chown at build time goes in the builder stage above.
FROM gcr.io/distroless/cc-debian13:nonroot@sha256:d3cda6e91129130d7229a1806b6a73d292ef245ab032da7851907798024cefba
# /usr/local carries the interpreter, stdlib C extensions (lib-dynload),
# libpython3.12.so, and all pip-installed packages in one consistent copy.
COPY --from=builder --chown=65532:65532 /usr/local /usr/local
# Stdlib-required shared libs absent from distroless cc (see builder stage).
COPY --from=builder /opt/runtime-libs/ /usr/lib/x86_64-linux-gnu/
COPY --from=builder --chown=65532:65532 /app /app
COPY --from=builder --chown=65532:65532 /home/nonroot /home/nonroot
# CA bundle for TLS to Snowflake, pinned explicitly rather than trusted from
# the base image.
COPY --from=builder --chown=65532:65532 /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV PATH="/usr/local/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/nonroot \
    SF_OCSP_RESPONSE_CACHE_DIR=/home/nonroot/.cache/snowflake
WORKDIR /app
# USER intentionally omitted: the :nonroot base already defaults to uid 65532.
EXPOSE 8080
CMD ["/usr/local/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
