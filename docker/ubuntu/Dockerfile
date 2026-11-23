FROM ghcr.io/jdx/mise:2026.11.12@sha256:991e2a550518ec5cae6f25adbc862ec0253ff714ee6e5e6bf328058a7324a041 AS mise

FROM ubuntu:26.10@sha256:c4ca31b6e93261c5dd773826f789738a49e414ca04fc094b1ccd0f91e07329a2

ARG DOTFILES_APT_CACHE=""
ARG TARGETARCH

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/home/dev \
    CLAUDE_CONFIG_DIR=/home/dev/.claude \
    MISE_DATA_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_ENV=container \
    MISE_YES=1 \
    MISE_TRUSTED_CONFIG_PATHS=/workspace:/home/dev/.config/mise \
    PATH=/home/dev/.local/bin:/home/dev/.opencode/bin:/opt/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

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
    mise --version \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && export HOME=/tmp/phase1-home \
      XDG_DATA_HOME=/tmp/phase1-home/data \
      XDG_STATE_HOME=/tmp/phase1-home/state \
      XDG_CACHE_HOME=/tmp/phase1-home/cache \
      MISE_DATA_DIR=/tmp/phase1-mise \
      MISE_CACHE_DIR=/tmp/phase1-mise/cache \
      MISE_STATE_DIR=/tmp/phase1-mise/state \
      MISE_CONFIG_DIR=/tmp/phase1 \
      MISE_TRUSTED_CONFIG_PATHS=/tmp/phase1 \
    && mise -E container-prune-ubuntu bootstrap --only accounts --yes \
    && envs="container,linux" \
    && if [ -n "${DOTFILES_APT_CACHE}" ]; then envs="${envs},ubuntu-cache"; fi \
    && mise -E "${envs}" bootstrap --only accounts,packages,files --yes \
    && rm -rf /etc/apt/apt.conf.d/01proxy /tmp/phase1 /tmp/phase1-home /tmp/phase1-mise

USER 1000:1000

COPY --chown=1000:1000 unix/home/.config/mise/ /tmp/phase1b/

RUN --mount=type=cache,id=mise-downloads-${TARGETARCH},target=/opt/mise/cache,uid=1000,gid=1000 \
    chmod 0644 /tmp/phase1b/mise.lock \
    && chmod +x /tmp/phase1b/tasks/bootstrap/* \
    && export MISE_CONFIG_DIR=/tmp/phase1b MISE_TRUSTED_CONFIG_PATHS=/tmp/phase1b \
    && mise -E container,linux install --locked \
    && mise -E container,linux run bootstrap \
    && ln -s /workspace/unix/home/.config/mise /home/dev/.config/mise \
    && rm -rf /tmp/phase1b

WORKDIR /workspace
CMD ["tailscaled", "--tun=userspace-networking", "--statedir=/home/dev/.local/state/tailscale", "--socket=/var/run/tailscale/tailscaled.sock", "--socks5-server=localhost:1055", "--outbound-http-proxy-listen=localhost:1055"]
