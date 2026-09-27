# Packs: signing and code

Most packs are data: policies, guard rules, playbooks, price books, report
templates and skills. nable validates them and nothing in them runs. Three
kinds carry code: **connectors** (a cost source, returning FOCUS rows),
**org-context adapters** (proposing owners and environments) and **action
sinks** (delivering a ticket or a PR proposal). nable never imports that code.
It runs out of process, behind a broker in the core, and gets only the data,
secrets and network hosts its manifest declares.

This page covers signing and the broker. `nable pack --help` covers the rest.

## Signing

A signature is a detached Ed25519 signature over the pack's **content digest**,
the `digest` that `nable pack validate` prints: one sha256 over every file's
path and sha256, with the signature file left out. It lives in
`nable-pack.sig`, inside the pack or next to a tarball (`<tarball>.sig`).

```
nable pack keygen --out ~/secure/org-packs.pem --name acme-platform
nable pack sign ./my-pack --key ~/secure/org-packs.pem
nable pack sign ./my-pack-1.0.0.tar.gz --key ~/secure/org-packs.pem
```

`keygen` writes the private key with mode 0600 (never over an existing file;
`--encrypt` asks for a passphrase) and prints the public key as a snippet for
the org policy. `sign` validates the pack first, and never prints or logs the
key.

nable trusts two kinds of key, and nothing else:

| Key | Where it comes from | What it lets a pack do |
|---|---|---|
| nable's first-party key | built into nable | claim `support = "first-party"` or `"verified"` |
| an org key | `packs.trusted_keys` in the org policy file | pass `packs.require_signed`; run code |

- A `first-party` or `verified` claim is honoured only when nable's first-party
  key signed the pack. Otherwise the install is refused, with or without an
  org policy.
- A signature that does not hold (a file changed after signing, or the
  signature does not verify) is refused everywhere.
- Under `packs.require_signed: true`, a pack must be signed by the first-party
  key or an org-trusted key. `--yes` stays refused under it.
- Audit, the runtime and the broker verify again from the installed files
  against today's keys, so removing a key from `trusted_keys` stops its packs
  loading.

The manifest's `[integrity].attestation` (a PEP 740 or Sigstore bundle
reference) is parsed and shown, and **not verified yet**. Nothing relies on it.

## The org policy

In `nable.policy.yaml` (in nable's data directory or `FINOPS_POLICY_FILE`, never
the working directory):

```yaml
packs:
  require_signed: true
  trusted_keys:
    - name: acme-platform
      key: <base64 Ed25519 public key from `nable pack keygen`>
  allow_unsigned_code: [io.github.acme/internal-connector]
```

A malformed key or pack id fails closed: every install is refused until it is
fixed.

## Running code

```
nable pack run com.example/example-csv-connector csv-costs --start 2026-09-01 --end 2026-10-01
```

Code runs only from a pack signed by the first-party key or an org-trusted
key, or one the org allowlists by id in `packs.allow_unsigned_code`. For each
call the broker:

1. re-checks the installed pack: files match what was approved, the org policy
   allows it, the signature holds;
2. starts `python -I -B` running `finops.packs.host` in a new process group, in
   a throwaway HOME, with only `PATH`, `HOME`, `LANG` and the declared secrets
   in its environment (read from nable's vault, else from nable's own
   environment). No `FINOPS_*`, `NABLE_*` or cloud credentials cross;
3. speaks JSON-RPC 2.0 over stdin and stdout with a per-call timeout and an
   output cap; stderr goes, truncated and with secret values redacted, to
   `<data dir>/packs/logs/<namespace>/<name>.log`;
4. answers `data.read` only for declared `read_data` scopes (`focus.cost`,
   `org.owners` and `org.environments` today; the other scopes say they are not
   available yet);
5. checks what comes back: FOCUS rows against nable's schema (invalid rows are
   dropped and reported), org facts as proposals whose source starts with the
   pack id, sink deliveries against the declared `act` kinds and
   `max_autonomy` (checked before the process starts).

Code must live in the pack directory, where its signature covers it. An entry
point that resolves to an installed Python distribution instead runs only for
a pack the org allowlists.

`read_cloud` is shown and approved at install but not brokered: the broker
hands a pack no cloud credentials. A connector that needs an API key declares
it in `secrets`.

## What the laptop sandbox does not do

This is a seatbelt, not a security boundary.

- **Network.** On Linux, a pack that declares no network runs in its own empty
  network namespace when the kernel allows unprivileged user namespaces, and
  then reaches nothing. Otherwise, and for any pack that declares hosts, egress
  is audited, not isolated: an audit hook inside the pack's process refuses
  connections and name lookups for undeclared hosts, refuses unix sockets,
  starting programs and loading native libraries through ctypes, and reports
  every connection it sees to the log. A hook that runs inside the process it
  watches can be bypassed by a determined pack (through the garbage collector,
  or a C extension that calls `connect()` directly). That is why unsigned code
  does not run.
- **Files.** Not sandboxed. A pack runs as your user and can read what you can
  read, including, on Linux, other processes' environments under `/proc` that
  your user may read. The scrubbed environment keeps secrets out of the pack's
  own environment, not out of reach of a pack that goes looking.
- **Resources.** A call is stopped at its timeout and at its output cap. CPU
  and memory are not otherwise limited.

Where egress has to be enforced, run nable in the hosted or self-hosted
runner, where container network policy does it.

## Writing a code pack

See `examples/packs/example-csv-connector` and `finops/packs/sdk.py`. An entry
point is one function:

```python
def fetch(ctx, start, end):          # connector: FOCUS rows
def propose(ctx, context):           # adapter: org facts, always proposals
def deliver(ctx, payload):           # sink: a receipt dict
```

`ctx.secret(name)` and `ctx.read_data(scope, query)` refuse anything the
manifest does not declare. Print freely: stdout is not the protocol channel.
`sdk.Context.for_testing(...)` lets a pack's own tests call its entry points
without the broker.
