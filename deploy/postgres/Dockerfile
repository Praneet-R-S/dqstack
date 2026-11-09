# PostgreSQL 16 for DQ-Stack, with WAL-G for continuous archiving and point-in-time
# recovery (deploy/README.md section 5). docker-compose.yml builds it for three
# services: db (archives every finished WAL segment), pitr (nightly base backups and
# retention) and backup (the independent nightly pg_dump).
#
# Debian, not Alpine: WAL-G's release binaries are built against glibc. An existing
# data volume made by the old postgres:16-alpine image must be moved with a dump and
# restore, not reused (text sort order differs between musl and glibc, which would
# leave indexes on text columns out of order). See deploy/README.md section 5.
#
# Versions are pinned exactly. The base image by digest; WAL-G by release and by the
# SHA-256 GitHub publishes for each release file, checked before the binary is used.
FROM postgres:16.15-bookworm@sha256:5264710be20f409750ac6dc4e5706dcef929c87e241605463f0f9aefa932a8d9

ARG WALG_VERSION=v3.0.9
ARG WALG_SHA256_AMD64=d6795c663894d836ba20840bffe8970aed81bbe25d3151bd4951f6f9c362def9
ARG WALG_SHA256_ARM64=072eb4a4a119f817e1fa888c64a776b01e3453b2e0d967a67d149adb18fde652
ARG TARGETARCH

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl jq libsodium23; \
    case "${TARGETARCH:-amd64}" in \
      amd64) asset=wal-g-pg-22.04-amd64;   sum="$WALG_SHA256_AMD64" ;; \
      arm64) asset=wal-g-pg-22.04-aarch64; sum="$WALG_SHA256_ARM64" ;; \
      *) echo "WAL-G publishes no build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 -o /usr/local/bin/wal-g \
      "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${asset}"; \
    echo "${sum}  /usr/local/bin/wal-g" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/wal-g; \
    if ldd /usr/local/bin/wal-g | grep 'not found'; then exit 1; fi; \
    wal-g --version; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /walg; \
    chown postgres:postgres /walg; \
    chmod 0700 /walg

COPY dq-pitr.sh /usr/local/bin/dq-pitr
RUN chmod 0755 /usr/local/bin/dq-pitr
