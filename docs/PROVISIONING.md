# Host a single-tenant nable instance for a customer (AWS EC2)

This is the hand-cranked runbook for standing up a managed, single-tenant nable
instance for one customer, on an EC2 box you control. Use it for enterprises that
want the dashboard, scheduler, and alerts without anyone installing a CLI: health
systems, finance teams, anyone terminal-averse.

It is deliberately manual. One box per customer, provisioned by hand, a handful at
a time. Do not productize this into multi-tenant auto-provisioning, that is a
different and much later product. See the hosting-posture notes.

## The model

- **One EC2 instance per customer.** Single-tenant. Their cloud credentials live
  only on their box, in its encrypted vault. Nothing is ever pooled across
  customers.
- **The customer logs in from getnable.com.** Their account "Open my dashboard"
  mints a short-lived, single-use token scoped to their instance and redirects to
  it. No password to distribute. Password and SSO login also work as fallbacks.
- **You run it on AWS, on your credits.** A `t3.small` is plenty for one customer.

```
  getnable.com account ──mint token (instance-scoped)──▶ https://<customer>.getnable.com
        (CP_MASTER_SECRET)                                   EC2 box, single tenant
                                                             Caddy TLS ─▶ nable :8080
                                                             customer's read-only cloud creds
```

## Prerequisites

- An AWS account with credits (this is the host, not the customer's account).
- `CP_MASTER_SECRET` set in the getnable.com (Vercel) env. The same master is used
  to derive every instance's secret. Keep it out of the repo and out of EC2
  user-data.
- A subdomain per customer, e.g. `baptist.getnable.com`, that you can point with a
  DNS A record.
- An EC2 key pair for SSH, and `openssl` locally (for the secret derivation).

## 1. Generate the per-customer config

```bash
CP_MASTER_SECRET="$(your-secret-manager get cp_master_secret)" \
  ./deploy/provision-tenant.sh baptist baptist.getnable.com
```

This writes `tenants/baptist.env` (chmod 600, gitignored, never commit) with a
stable `FINOPS_INSTANCE_ID`, the derived `FINOPS_CONTROL_PLANE_SECRET`, the domain,
a strong fallback dashboard password, and commented slots for the customer's SSO
and read-only cloud creds. The secret is derived exactly as the account's mint
endpoint expects: `hmac_sha256_hex(CP_MASTER_SECRET, "nable-instance:" + id)`.

## 2. Launch the EC2 box

Security group: inbound 80 and 443 from anywhere (Let's Encrypt validates over
them and the dashboard serves over 443), and 22 from your IP only. Close 8080.

```bash
aws ec2 run-instances \
  --image-id <amazon-linux-2023-ami> \
  --instance-type t3.small \
  --key-name <your-keypair> \
  --security-group-ids <sg-with-80-443-and-your-ssh> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=nable-baptist},{Key=customer,Value=baptist}]' \
  --user-data file://deploy/ec2-user-data.sh
```

The user-data installs Docker and the compose plugin and clones the (public) repo,
so the box is ready to deploy when you SSH in. It does NOT put any secret on the
box, you copy the env yourself in the next step.

`deploy/ec2-user-data.sh`:

```bash
#!/usr/bin/env bash
set -euxo pipefail
dnf install -y docker git
systemctl enable --now docker
DOCKER_CONFIG=/usr/local/lib/docker
mkdir -p "$DOCKER_CONFIG/cli-plugins"
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o "$DOCKER_CONFIG/cli-plugins/docker-compose"
chmod +x "$DOCKER_CONFIG/cli-plugins/docker-compose"
git clone https://github.com/chaandannn/finopsmcp /opt/nable
```

## 3. Point DNS

Add an A record for the customer's subdomain to the instance's public IP (an
Elastic IP is worth it so the address survives a stop/start):

```
baptist.getnable.com.  A  <elastic-ip>
```

Wait for it to resolve before the next step, Caddy needs it to get a certificate.

## 4. Deploy

Copy the env up as `.env` (over SSH, never via user-data), then start with the TLS
profile:

```bash
scp tenants/baptist.env ec2-user@<ip>:/opt/nable/.env
ssh ec2-user@<ip>
cd /opt/nable
sudo docker compose --profile tls up -d --build
sudo docker compose logs -f nable   # confirm the scheduler + dashboard came up
```

Caddy fetches the Let's Encrypt cert on first boot. The dashboard is then live at
`https://baptist.getnable.com` with session cookies marked Secure.

## 5. Give the instance read-only cloud access

The customer's credentials, not yours. Easiest path: hand them the one-click
read-only CloudFormation key (`finops setup aws` generates the quick-create link,
or send them the template URL). They run it in their AWS account and send you the
two output values. Paste them into `.env` and restart:

```bash
# in /opt/nable/.env
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```
```bash
sudo docker compose --profile tls up -d
```

Azure and GCP slot in the same way (`AZURE_*`, `GOOGLE_APPLICATION_CREDENTIALS`).
These creds live only on this box. They are never sent to getnable.com.

## 6. Register the instance and hand off login

For the account's "Open my dashboard" to route to this box, set two fields on the
customer's **Stripe customer metadata**, this is exactly where
`web/api/account/dashboard-login.js` reads them: `instance_id` and
`instance_domain` (the two values the provision script printed). Set them on that
customer in the Stripe dashboard, or via the account hosting endpoint
(`web/api/account/hosting.js`, which already writes hosting choice to the same
metadata). Then:

1. The customer signs in at `https://getnable.com/account` (email OTP).
2. They click "Open my dashboard", the account mints an instance-scoped token and
   redirects to `https://baptist.getnable.com`, which verifies it with its
   `FINOPS_CONTROL_PLANE_SECRET` and starts a session.

If the customer has SSO, fill the `FINOPS_SSO_*` block in `.env` instead and add
`https://baptist.getnable.com/sso/callback` to their IdP. Then login goes straight
through Okta / Entra / Google with no nable password at all.

## Updating an instance

```bash
ssh ec2-user@<ip>
cd /opt/nable && sudo git pull && sudo docker compose --profile tls up -d --build
```

## Offboarding

Terminate the instance and remove the DNS record. Because the box was
single-tenant, that one action destroys every credential and all derived data for
that customer. Delete `tenants/<slug>.env` locally and clear the instance fields on
their account record.

```bash
aws ec2 terminate-instances --instance-ids <id>
```

## Why this stays within the posture

Each customer is physically isolated on their own box. Raw cloud credentials and
bills never leave it and are never pooled. The only thing getnable.com holds is the
derived data the customer chooses to surface plus the routing record (instance id +
domain), never their cloud keys. That is the single-tenant guarantee, kept by
construction rather than by policy.
