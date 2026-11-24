FROM ghcr.io/jdx/mise:2026.11.14@sha256:7178a0bc7eb30d5090f81237fc0a7830f57e52126b9d8df656e24547437a1e53 AS mise

FROM ubuntu:26.10@sha256:c4ca31b6e93261c5dd773826f789738a49e414ca04fc094b1ccd0f91e07329a2

ARG DOTFILES_APT_CACHE=""
ARG DOTFILES_V2_ROOT=/workspace
ARG TARGETARCH

# ZCODE_CREDENTIAL_SECRET_FILE is a path inside a runtime volume, not a secret value.
# hadolint ignore=DL3064
ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/home/dev \
    CLAUDE_CONFIG_DIR=/home/dev/.claude \
    ZCODE_CREDENTIAL_SECRET_FILE=/home/dev/.zcode/v2/credential-secret \
    DOTFILES_V2_ROOT=${DOTFILES_V2_ROOT} \
    MISE_DATA_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_ENV=container \
    MISE_YES=1 \
    MISE_TRUSTED_CONFIG_PATHS=${DOTFILES_V2_ROOT}:/workspace:/home/dev/.config/mise \
    PATH=/opt/mise/shims:/home/dev/.opencode/bin:/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
COPY --from=mise /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

COPY unix/home/.config/mise/config.container-prune-ubuntu.toml \
    unix/home/.config/mise/config.container.toml \
    unix/home/.config/mise/config.linux.toml \
    unix/home/.config/mise/config.ubuntu-cache.toml \
    /tmp/phase1/
COPY unix/home/.config/mise/assets/container/ /tmp/phase1/assets/container/

RUN --mount=type=cache,id=apt-cache-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=apt-lib-${TARGETARCH},target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && export HOME=/tmp/phase1-home \
      XDG_DATA_HOME=/tmp/phase1-home/data \
      XDG_STATE_HOME=/tmp/phase1-home/state \
      XDG_CACHE_HOME=/tmp/phase1-home/cache \
      MISE_DATA_DIR=/tmp/phase1-mise \
      MISE_CACHE_DIR=/tmp/phase1-mise/cache \
      MISE_STATE_DIR=/tmp/phase1-mise/state \
      MISE_CONFIG_DIR=/tmp/phase1 \
      MISE_TRUSTED_CONFIG_PATHS=/tmp/phase1 \
    && mise --version \
    && mise -E container-prune-ubuntu bootstrap --only accounts --yes \
    && envs="container,linux" \
    && if [ -n "${DOTFILES_APT_CACHE}" ]; then envs="${envs},ubuntu-cache"; fi \
    && mise -E "${envs}" bootstrap --only accounts,packages,files --yes \
    && rm -rf /etc/apt/apt.conf.d/01proxy /tmp/phase1 /tmp/phase1-home /tmp/phase1-mise

USER 1000:1000

COPY --chown=1000:1000 unix/home/.config/mise/ /tmp/phase1b/

RUN --mount=type=cache,id=mise-downloads-${TARGETARCH},target=/opt/mise/cache,uid=1000,gid=1000 \
    chmod 0644 /tmp/phase1b/mise*.lock \
    && chmod +x /tmp/phase1b/tasks/bootstrap/* \
    && export MISE_CONFIG_DIR=/tmp/phase1b MISE_TRUSTED_CONFIG_PATHS=/tmp/phase1b \
    && mise -E container,linux install --locked \
    && mise -E container,linux run bootstrap \
    && ln -s "${DOTFILES_V2_ROOT}/unix/home/.config/mise" /home/dev/.config/mise \
    && rm -rf /tmp/phase1b

WORKDIR /workspace
CMD ["tailscaled", "--tun=userspace-networking", "--statedir=/home/dev/.local/state/tailscale", "--socket=/var/run/tailscale/tailscaled.sock", "--socks5-server=localhost:1055", "--outbound-http-proxy-listen=localhost:1055"]
