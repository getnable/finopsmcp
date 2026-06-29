# Fleet deploys: updating hosted nable boxes at scale

Single-tenant hosting means one box per customer. Updating them by hand (SSH in,
`git checkout`, `docker build`, `docker compose up`) is O(N) human effort and does
not survive past a couple of tenants. This is how it stays one command instead.

The model has three parts:

1. **The image is built once, in CI, not on each box.** Every release tag triggers
   [`.github/workflows/image.yml`](../.github/workflows/image.yml), which builds the
   container from source and pushes it to GHCR as
   `ghcr.io/chaandannn/finops:<version>` and `:latest`. Every tenant runs the
   identical, tested artifact, which also makes support and security review sane.

2. **Boxes pull, they do not build.** [`docker-compose.yml`](../docker-compose.yml)
   points the `nable` service at the GHCR image. A box update is
   `docker compose pull && docker compose up -d`: seconds, no compiler, no source
   checkout needed. Pin a version per box with `NABLE_VERSION` in its `.env`; blank
   means latest.

3. **The fleet updates with one command, no SSH.**
   [`deploy/fleet-update.sh`](../deploy/fleet-update.sh) uses AWS SSM Run Command to
   tell every tagged box to pull and restart, from your laptop or CI, with no SSH
   keys and no port 22 open.

## One-time setup

**Make the GHCR image pullable.** After the first CI push, set the GHCR package
visibility. The image holds no secrets (the code already ships publicly on PyPI),
so **public** is the simplest: the box pulls with no credentials. For a private
package instead, run `docker login ghcr.io` on each box with a read-only token.

**Per box, give it an SSM identity and a tag** (set both at launch via the launch
template / instance profile, or after the fact):

```bash
# 1. Attach an instance-profile IAM role that includes the managed policy
#    AmazonSSMManagedInstanceCore (the SSM agent ships on Amazon Linux 2023).
# 2. Tag the instance so the fleet script can target it.
aws ec2 create-tags --resources i-0123abc --tags Key=app,Value=nable
```

That is it. The box is now drivable from SSM and counted in the fleet.

## Updating the whole fleet

```bash
deploy/fleet-update.sh            # every app=nable box -> latest release
deploy/fleet-update.sh 0.8.100    # pin every box to 0.8.100
```

One box or fifty, same command. It pins `NABLE_VERSION` in each box's `.env`,
pulls the image, restarts, and prunes the old image. The script prints an SSM
CommandId and the one-liner to watch every box's status.

## Updating a single box

Either target one instance with SSM:

```bash
aws ssm send-command --region us-east-2 --targets Key=InstanceIds,Values=i-0123abc \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /opt/nable && docker compose --profile tls pull && docker compose --profile tls up -d"]'
```

Or, for a one-off with no AWS CLI, open the box in the AWS console
(EC2 -> Instances -> the box -> Connect -> EC2 Instance Connect, a browser
terminal already logged in) and run the same `pull && up -d`.

## The maturity ladder

- **Now (1 to ~dozens of boxes):** `fleet-update.sh` over SSM Run Command. SSM
  scales to hundreds of instances per call, so this carries you a long way.
- **Later (rolling, automatic):** the control plane in the hosting strategy, boxes
  register, a release rolls out on its own with health checks and staggering. You
  do not need it for a handful of tenants; SSM is the bridge until you do.

## Why this also matters for security

Deferred hardening (moving the vault master key off the data volume, adding the
Caddy security headers) needs a box redeploy to land. On this model that is the
same one command across every tenant, not N hand-rebuilds, so a security fix
reaches the whole fleet the moment it ships.
