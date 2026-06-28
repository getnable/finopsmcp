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

# docker compose v2 as a CLI plugin. Pin the version (not "latest") so boxes
# provisioned weeks apart get the same binary. On a credential-bearing host you
# should also verify the published sha256 from the docker/compose release before
# the chmod +x below.
DOCKER_CONFIG=/usr/local/lib/docker
COMPOSE_VERSION="v2.29.7"
mkdir -p "${DOCKER_CONFIG}/cli-plugins"
curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o "${DOCKER_CONFIG}/cli-plugins/docker-compose"
chmod +x "${DOCKER_CONFIG}/cli-plugins/docker-compose"

# docker buildx, required by compose v2 to build images (`up --build`). Without
# it the build fails with "compose build requires buildx 0.17.0 or later".
BUILDX_VERSION="v0.18.0"
curl -SL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
  -o "${DOCKER_CONFIG}/cli-plugins/docker-buildx"
chmod +x "${DOCKER_CONFIG}/cli-plugins/docker-buildx"

# Public repo, no credentials needed to clone. Check out the latest RELEASE TAG,
# not arbitrary main HEAD, so the box runs a tested release.
git clone https://github.com/chaandannn/finopsmcp /opt/nable
git -C /opt/nable checkout "$(git -C /opt/nable describe --tags --abbrev=0)"

# The clone runs as root (cloud-init); hand /opt/nable to ec2-user so the
# operator can scp the per-customer .env in and edit it without sudo.
chown -R ec2-user:ec2-user /opt/nable

echo "nable host ready. Next: scp the per-customer .env to /opt/nable/.env, then"
echo "  cd /opt/nable && docker compose --profile tls up -d --build"
