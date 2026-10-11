# Build with scripts/build-session-image.py. BASE_IMAGE must be digest-pinned
# and BASE_FAMILY must match one of the committed snapshot/package manifests:
# Debian Bookworm or Ubuntu Noble.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_FAMILY

# Snapshot apt inputs are copied into the isolated build context by the helper.
COPY apt/ /opt/doxa-apt/
RUN set -eu; \
    . /etc/os-release; \
    case "$ID:$VERSION_CODENAME:$BASE_FAMILY" in \
      debian:bookworm:debian-bookworm) family=debian-bookworm ;; \
      ubuntu:noble:ubuntu-noble) family=ubuntu-noble ;; \
      *) echo "unsupported base family or OS release" >&2; exit 1 ;; \
    esac; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64|arm64) ;; *) echo "unsupported base architecture" >&2; exit 1 ;; esac; \
    rm -f /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; \
    cp "/opt/doxa-apt/$family.sources" /etc/apt/sources.list; \
    printf '%s\n' 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99doxa-snapshot; \
    apt-get update; \
    xargs -r apt-get install -y --no-install-recommends < "/opt/doxa-apt/$family.packages"; \
    rm -f /etc/apt/apt.conf.d/99doxa-snapshot; \
    rm -rf /opt/doxa-apt; \
    rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 worker /usr/local/bin/doxa-isolation-worker
COPY --chmod=0755 providers/ /usr/local/bin/
COPY --chmod=0700 codex-provider/ /opt/doxa/codex-provider/
RUN if [ -f /opt/doxa/codex-provider/receipt.json ]; then \
      chmod 0700 /opt/doxa/codex-provider; \
      chmod 0600 /opt/doxa/codex-provider/receipt.json; \
      ln -s /opt/doxa/codex-provider/codex /usr/local/bin/codex; \
    fi

ENV HOME=/home/doxa TMPDIR=/work-cache/tmp
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/doxa-isolation-worker"]
CMD ["hold"]
