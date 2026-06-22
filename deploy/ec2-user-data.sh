#!/usr/bin/env bash
#
# EC2 user-data (cloud-init) for a single-tenant nable box. Prepares the host:
# installs Docker + the compose plugin and clones the public repo into /opt/nable.
# It intentionally puts NO secret on the box, you copy the per-customer .env over
# SSH afterwards and run `docker compose --profile tls up -d`. See docs/PROVISIONING.md.
#
# Amazon Linux 2023 (dnf). For Ubuntu, swap dnf -> apt and adjust the docker pkg.
set -euxo pipefail

dnf install -y docker git
systemctl enable --now docker

# docker compose v2 as a CLI plugin
DOCKER_CONFIG=/usr/local/lib/docker
mkdir -p "${DOCKER_CONFIG}/cli-plugins"
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o "${DOCKER_CONFIG}/cli-plugins/docker-compose"
chmod +x "${DOCKER_CONFIG}/cli-plugins/docker-compose"

# Public repo, no credentials needed to clone.
git clone https://github.com/chaandannn/finopsmcp /opt/nable

echo "nable host ready. Next: scp the per-customer .env to /opt/nable/.env, then"
echo "  cd /opt/nable && docker compose --profile tls up -d --build"
