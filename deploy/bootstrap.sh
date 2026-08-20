#!/usr/bin/env bash
#
# Session 15 — one-time provisioning of the EC2 instance.
#
# Everything a PaaS did for you and now you own: install a container runtime,
# create a place for the app to live, close the ports you are not using, give
# the box a memory cushion, keep the logs from eating the disk, and make the
# stack come back after a reboot.
#
# Idempotent: safe to run twice.
#
#     ssh ubuntu@<host> 'bash -s' < deploy/bootstrap.sh
#
# Target: Ubuntu 24.04 LTS on t3.medium (4 GB RAM, >= 30 GB EBS).
#
# Why >= 30 GB: the images alone are ~7.4 GB (the AI service is ~4.9 GB — torch
# arrives via sentence-transformers), plus the pgvector volume, plus Docker's
# build cache. The default 8 GB root volume fills during the first `pull`.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/estimator}"
APP_USER="${APP_USER:-$(id -un)}"

log() { printf '\n\033[1m[bootstrap]\033[0m %s\n' "$1"; }

# --- 1. System ---------------------------------------------------------------
log "Updating the system"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq

log "Installing base packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl gnupg postgresql-client ufw unattended-upgrades

# --- 2. Docker ---------------------------------------------------------------
# From Docker's own repository, not Ubuntu's: the distro package lags and ships
# no `docker compose` v2 plugin, which every command here assumes.
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine + compose plugin"
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    log "Docker already installed — skipping"
fi

log "Adding ${APP_USER} to the docker group"
sudo usermod -aG docker "${APP_USER}"

# --- 3. Log rotation ---------------------------------------------------------
# Without this a long-lived box with `restart: unless-stopped` and a healthcheck
# every 30 s fills the disk with JSON logs, and the first symptom is Postgres
# refusing to write. This is the single most common way a small VM dies quietly.
log "Capping container log size"
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl restart docker

# --- 4. Swap -----------------------------------------------------------------
# 4 GB of RAM is enough to RUN the stack but tight while pgvector builds its
# HNSW indexes on first migration. Swap turns a hard OOM kill into a slow
# minute. It is a cushion, not a substitute for memory.
if ! sudo swapon --show | grep -q '/swapfile'; then
    log "Creating a 2G swapfile"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
else
    log "Swap already present — skipping"
fi

# --- 5. Firewall -------------------------------------------------------------
# The SECOND of the two layers that keep the AI service private. The first is
# the AWS security group; this one survives a security-group mistake, and a
# security group survives a ufw mistake. Neither alone is the boundary.
#
# Note what is NOT opened: 8000 (AI service), 3000 (Rails), 5432 (Postgres),
# 6379 (Redis). They are reachable only from inside the Docker network.
log "Configuring the firewall"
sudo ufw --force reset > /dev/null
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    comment 'SSH'
sudo ufw allow 80/tcp    comment 'HTTP - Caddy, ACME challenge'
sudo ufw allow 443/tcp   comment 'HTTPS - Caddy'
sudo ufw --force enable

# --- 6. Unattended security upgrades ----------------------------------------
log "Enabling unattended security upgrades"
echo 'Unattended-Upgrade::Automatic-Reboot "false";' \
    | sudo tee /etc/apt/apt.conf.d/51-no-auto-reboot > /dev/null
sudo systemctl enable --now unattended-upgrades

# --- 7. Application directory ------------------------------------------------
log "Preparing ${APP_DIR}"
sudo mkdir -p "${APP_DIR}"
sudo chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

# --- 8. systemd --------------------------------------------------------------
# The replacement for "the PaaS restarts it for you". Without this, a reboot —
# scheduled, or an AWS host retirement — leaves the whole system down until a
# human notices.
if [ -f "${APP_DIR}/deploy/estimator.service" ]; then
    log "Installing the systemd unit"
    sudo cp "${APP_DIR}/deploy/estimator.service" /etc/systemd/system/estimator.service
    sudo systemctl daemon-reload
    sudo systemctl enable estimator.service
else
    log "Unit file not found yet — copy the repo to ${APP_DIR}, then:"
    echo "    sudo cp ${APP_DIR}/deploy/estimator.service /etc/systemd/system/"
    echo "    sudo systemctl daemon-reload && sudo systemctl enable estimator"
fi

cat <<EOF

──────────────────────────────────────────────────────────────────────────────
 Bootstrap complete.

 Next, and in this order:
   1. Log out and back in   (so the docker group applies to your shell)
   2. Copy the repo to      ${APP_DIR}
   3. Create the secrets:   ${APP_DIR}/.env    ->  chmod 600
   4. Restore the corpus:   scripts/restore_corpus.sh
   5. Start it:             sudo systemctl start estimator

 The .env NEVER comes from git. Copy it with scp and lock it down:
   scp .env.prod ${APP_USER}@<host>:${APP_DIR}/.env && ssh … chmod 600 ${APP_DIR}/.env
──────────────────────────────────────────────────────────────────────────────
EOF
