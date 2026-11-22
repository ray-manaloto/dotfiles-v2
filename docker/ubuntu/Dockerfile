FROM ubuntu:26.10@sha256:c4ca31b6e93261c5dd773826f789738a49e414ca04fc094b1ccd0f91e07329a2

ARG MISE_VER=2026.11.12
ARG MISE_SHA256_AMD64=4b029e19afc575875a4962dfe5d5e31cc93ab0317242768d0234adec219d3e14
ARG MISE_SHA256_ARM64=9a125763f8445ea28d2da0e943fe5ad7d81b397d78f173cc07c1cc18ee2bee47
ARG TARGETARCH

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    MISE_DATA_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_YES=1 \
    MISE_TRUSTED_CONFIG_PATHS=/workspace \
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates=20260816ubuntu0.26.10.2 \
      curl=8.20.0-2ubuntu11.1 \
      git=1:2.55.0-1ubuntu1.1 \
    && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH}" in \
      amd64) a=x64 h="${MISE_SHA256_AMD64}" ;; \
      arm64) a=arm64 h="${MISE_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo /tmp/mise.tar.gz \
      "https://github.com/jdx/mise/releases/download/v${MISE_VER}/mise-v${MISE_VER}-linux-${a}.tar.gz" \
    && echo "${h}  /tmp/mise.tar.gz" | sha256sum -c - \
    && tar -C /tmp -xzf /tmp/mise.tar.gz \
    && install -m 0755 /tmp/mise/bin/mise /usr/local/bin/mise \
    && rm -rf /tmp/mise /tmp/mise.tar.gz

RUN if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
    && if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu; fi \
    && useradd -m -u 1000 -s /bin/bash dev \
    && install -d -o dev -g dev /opt/mise /workspace

USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
