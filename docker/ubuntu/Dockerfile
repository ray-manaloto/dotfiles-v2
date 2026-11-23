FROM ubuntu:26.10@sha256:c4ca31b6e93261c5dd773826f789738a49e414ca04fc094b1ccd0f91e07329a2

ARG MISE_VER=2026.11.12
ARG MISE_SHA256_AMD64=4b029e19afc575875a4962dfe5d5e31cc93ab0317242768d0234adec219d3e14
ARG MISE_SHA256_ARM64=9a125763f8445ea28d2da0e943fe5ad7d81b397d78f173cc07c1cc18ee2bee47
ARG TAILSCALE_VER=1.104.7
ARG TAILSCALE_SHA256_AMD64=e56693ff74a64f2591b7e819c280635a100aae1ddfb0ef058fcd052b7cd8d16c
ARG TAILSCALE_SHA256_ARM64=d6bed2720310b328b861f943d7b4016c5358a9bad60575eb51e8143b79068484
ARG TARGETARCH

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/home/dev \
    CLAUDE_CONFIG_DIR=/home/dev/.claude \
    MISE_DATA_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_YES=1 \
    MISE_TRUSTED_CONFIG_PATHS=/workspace:/home/dev/.config/mise \
    PATH=/home/dev/.local/bin:/home/dev/.opencode/bin:/opt/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

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

RUN case "${TARGETARCH}" in \
      amd64) h="${TAILSCALE_SHA256_AMD64}" ;; \
      arm64) h="${TAILSCALE_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo /tmp/tailscale.tgz \
      "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VER}_${TARGETARCH}.tgz" \
    && echo "${h}  /tmp/tailscale.tgz" | sha256sum -c - \
    && tar -C /tmp -xzf /tmp/tailscale.tgz \
    && install -m 0755 "/tmp/tailscale_${TAILSCALE_VER}_${TARGETARCH}/tailscale" /usr/local/bin/tailscale \
    && install -m 0755 "/tmp/tailscale_${TAILSCALE_VER}_${TARGETARCH}/tailscaled" /usr/local/bin/tailscaled \
    && rm -rf "/tmp/tailscale_${TAILSCALE_VER}_${TARGETARCH}" /tmp/tailscale.tgz

RUN if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
    && if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu; fi \
    && useradd -m -u 1000 -s /bin/bash dev \
    && install -d -o dev -g dev /opt/mise /run/tailscale /workspace /home/dev/.claude /home/dev/.config /home/dev/.config/opencode /home/dev/.local /home/dev/.local/bin /home/dev/.local/share /home/dev/.local/share/opencode /home/dev/.local/state /home/dev/.local/state/tailscale /home/dev/.opencode /home/dev/.opencode/bin \
    && chmod 0700 /home/dev/.local/state/tailscale \
    && printf '\n. /etc/profile.d/dotfiles-v2-env.sh\n. /etc/profile.d/agent-secrets.sh\n' >> /etc/bash.bashrc

COPY --chmod=644 dotfiles-v2-env.sh /etc/profile.d/dotfiles-v2-env.sh
COPY --chmod=644 agent-secrets-profile.sh /etc/profile.d/agent-secrets.sh

USER 1000:1000

RUN export PATH=/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && /home/dev/.local/bin/claude --version

RUN export PATH=/home/dev/.opencode/bin:/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin \
    && curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path \
    && /home/dev/.opencode/bin/opencode --version

WORKDIR /workspace
CMD ["tailscaled", "--tun=userspace-networking", "--statedir=/home/dev/.local/state/tailscale", "--socket=/var/run/tailscale/tailscaled.sock", "--socks5-server=localhost:1055", "--outbound-http-proxy-listen=localhost:1055"]
