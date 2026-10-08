# Build with scripts/build-session-image.py. BASE_IMAGE must be digest-pinned.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Toolchains may be supplied by a reviewed Debian/Ubuntu-based image. No
# repository Dockerfile, host configuration or credentials enter this context.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git bash python3 \
 && rm -rf /var/lib/apt/lists/*

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
